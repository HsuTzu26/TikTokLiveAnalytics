"""Bounded, atomic state for live dashboards; NDJSON remains the history source."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        for attempt, delay in enumerate((0.02, 0.04, 0.08, 0.16)):
            try:
                os.replace(temp_path, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(delay)
    finally:
        temp_path.unlink(missing_ok=True)


class LiveStateWriter:
    """Maintain a small rolling view from already deduplicated events."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        username: str,
        flush_interval_seconds: float = 1.0,
        recent_limit: int = 100,
        viewer_sample_limit: int = 600,
        activity_limit: int = 300,
    ):
        self.path = path
        self.flush_interval_seconds = max(0.1, float(flush_interval_seconds))
        self.recent_limit = max(1, int(recent_limit))
        self.viewer_sample_limit = max(1, int(viewer_sample_limit))
        self.activity_limit = max(1, int(activity_limit))
        self.last_flush_monotonic = 0.0
        self.state = {
            "schema_version": 1,
            "session_id": session_id,
            "username": username,
            "status": "running",
            "updated_at_utc": None,
            "updated_at_local": None,
            "last_event_at_utc": None,
            "last_event_at_local": None,
            "last_event_at_ms": None,
            "connection_state": "starting",
            "gap_count": 0,
            "current_gap_started_at_ms": None,
            "last_gap_seconds": None,
            "total_gap_seconds": 0.0,
            "viewer_count": None,
            "viewer_samples": [],
            "likes_total": None,
            "likes_total_is_platform": False,
            "likes_observed": 0,
            "diamonds": 0,
            "chat_count": 0,
            "gift_count": 0,
            "member_count": 0,
            "follow_count": 0,
            "share_count": 0,
            "subscribe_count": 0,
            "event_counts": {},
            "recent_chat": [],
            "recent_gifts": [],
            "recent_activity": [],
        }

    @staticmethod
    def _append_bounded(rows: list, value: dict, limit: int) -> None:
        rows.append(value)
        if len(rows) > limit:
            del rows[: len(rows) - limit]

    def record(self, event: dict, *, force: bool = False) -> None:
        event_type = event.get("type") or "unknown"
        counts = self.state["event_counts"]
        counts[event_type] = int(counts.get(event_type, 0)) + 1
        received_ms = event.get("received_at_ms")
        received_at = event.get("received_at_utc") or event.get("received_at_local")

        if received_ms is not None:
            self.state["last_event_at_ms"] = received_ms
        if event.get("received_at_utc"):
            self.state["last_event_at_utc"] = event["received_at_utc"]
        if event.get("received_at_local"):
            self.state["last_event_at_local"] = event["received_at_local"]

        if event_type not in {"system", "room", "control"}:
            self._append_bounded(
                self.state["recent_activity"],
                {
                    "time": received_at,
                    "type": event_type,
                    "user": event.get("unique_id") or event.get("nickname") or "",
                    "detail": self._activity_detail(event),
                },
                self.activity_limit,
            )

        if event_type == "system":
            system_event = event.get("system_event")
            if system_event in {"connected", "reconnected"}:
                if system_event == "reconnected":
                    self._close_gap(received_ms)
                self.state["connection_state"] = "connected"
            elif system_event == "disconnected":
                if self.state["current_gap_started_at_ms"] is None:
                    self.state["gap_count"] += 1
                    self.state["current_gap_started_at_ms"] = (
                        received_ms if isinstance(received_ms, (int, float))
                        else int(time.time() * 1000)
                    )
                self.state["connection_state"] = "disconnected"
            elif system_event == "live_end":
                self.state["connection_state"] = "offline"
        elif event_type == "viewer":
            viewer_count = event.get("viewer_count")
            if isinstance(viewer_count, (int, float)):
                self.state["viewer_count"] = viewer_count
                self._append_bounded(
                    self.state["viewer_samples"],
                    {"time": received_at, "received_at_ms": received_ms, "viewer_count": viewer_count},
                    self.viewer_sample_limit,
                )
        elif event_type == "like":
            like_count = event.get("like_count") or 0
            if isinstance(like_count, (int, float)):
                self.state["likes_observed"] += int(like_count)
            total_likes = event.get("total_likes")
            if isinstance(total_likes, (int, float)):
                current = self.state["likes_total"]
                self.state["likes_total"] = max(int(current or 0), int(total_likes))
                self.state["likes_total_is_platform"] = True
            elif not self.state["likes_total_is_platform"]:
                self.state["likes_total"] = self.state["likes_observed"]
        elif event_type == "gift" and event.get("counted"):
            self.state["gift_count"] += 1
            diamonds = event.get("diamond_total") or 0
            if isinstance(diamonds, (int, float)):
                self.state["diamonds"] += diamonds
            self._append_bounded(
                self.state["recent_gifts"],
                {
                    "time": received_at,
                    "user_id": event.get("user_id"),
                    "unique_id": event.get("unique_id") or event.get("nickname") or "unknown",
                    "gift": event.get("gift_name") or "gift",
                    "repeat_count": event.get("repeat_count") or 1,
                    "diamonds": diamonds,
                },
                self.recent_limit,
            )
        elif event_type == "chat":
            self.state["chat_count"] += 1
            self._append_bounded(
                self.state["recent_chat"],
                {
                    "time": received_at,
                    "user_id": event.get("user_id"),
                    "unique_id": event.get("unique_id") or event.get("nickname") or "",
                    "comment": event.get("comment") or "",
                    "message_kind": event.get("message_kind"),
                },
                self.recent_limit,
            )
        elif event_type == "member":
            self.state["member_count"] += 1
        elif event_type == "social":
            action = event.get("social_action")
            if action == "follow":
                self.state["follow_count"] += 1
            elif action == "share":
                self.state["share_count"] += 1
        elif event_type == "subscribe":
            self.state["subscribe_count"] += 1

        force = force or (
            event_type == "system"
            and event.get("system_event")
            in {"connected", "reconnected", "disconnected", "live_end"}
        )
        self.flush(force=force)

    @staticmethod
    def _activity_detail(event: dict) -> str:
        event_type = event.get("type")
        if event_type == "chat":
            return str(event.get("comment") or "[emote]")
        if event_type == "gift":
            return f"{event.get('gift_name') or 'gift'} x{event.get('repeat_count') or 1}"
        if event_type == "viewer":
            return f"viewers={event.get('viewer_count')}"
        if event_type == "like":
            return f"+{event.get('like_count') or 0}"
        if event_type == "social":
            return str(event.get("social_action") or "social")
        return str(event_type or "event")

    def set_status(self, status: str) -> None:
        self.state["status"] = status
        if status != "running":
            self._close_gap(int(time.time() * 1000))
        self.flush(force=True)

    def _close_gap(self, ended_at_ms: int | float | None) -> None:
        started_at_ms = self.state["current_gap_started_at_ms"]
        if started_at_ms is None:
            return
        ended_at_ms = ended_at_ms if isinstance(ended_at_ms, (int, float)) else int(time.time() * 1000)
        duration = max(0.0, (ended_at_ms - started_at_ms) / 1000)
        self.state["last_gap_seconds"] = round(duration, 3)
        self.state["total_gap_seconds"] += duration
        self.state["current_gap_started_at_ms"] = None

    def flush(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self.last_flush_monotonic < self.flush_interval_seconds:
            return False
        from datetime import datetime, timezone

        updated = datetime.now(timezone.utc)
        self.state["updated_at_utc"] = updated.isoformat()
        self.state["updated_at_local"] = updated.astimezone(TAIPEI_TZ).isoformat()
        write_json_atomic(self.path, self.state)
        self.last_flush_monotonic = now
        return True
