"""Write a readable per-session LIVE and TTS summary next to captured events."""
from __future__ import annotations

import importlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.tts import gift_catalog

# Streamlit keeps imported modules alive across reruns. If an older catalog
# module is still cached without the current lookup API, refresh it before
# binding the function used by this report module.
if not callable(getattr(gift_catalog, "gift_metadata_for", None)):
    gift_catalog = importlib.reload(gift_catalog)
gift_metadata_for = gift_catalog.gift_metadata_for


ROOT = Path(__file__).resolve().parents[1]
TTS_STATE_PATH = ROOT / "data" / "tts" / "state.json"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TERMINAL_GIFT_FAILURES = {"synthesis_failed", "playback_failed", "worker_stopped"}
TERMINAL_GIFT_SUCCESSES = {"playback_completed"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _duration_stats(values: list[float]) -> dict[str, int | None]:
    ordered = sorted(max(0.0, value) for value in values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None}
    return {
        "count": len(ordered),
        "p50": round(ordered[(len(ordered) - 1) // 2]),
        "p95": round(ordered[max(0, int(len(ordered) * 0.95 + 0.999999) - 1)]),
    }


def _local_time(value: object) -> str | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
        return timestamp.astimezone(TAIPEI_TZ).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


def _session_matches(state: dict[str, Any], session_dir: Path, meta: dict[str, Any]) -> bool:
    source = state.get("source_session_dir")
    if source:
        return os.path.normcase(os.path.abspath(str(source))) == os.path.normcase(
            os.path.abspath(str(session_dir))
        )
    return (
        str(state.get("username") or "").casefold() == str(meta.get("username") or "").casefold()
        and state.get("active_session") == (meta.get("session_id") or session_dir.name)
    )


def _gift_rows(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    grouped: dict[str, dict[str, Any]] = {}
    skipped_streak_updates = 0
    counted_records = 0
    for event in events:
        if event.get("type") != "gift":
            continue
        if event.get("counted", True) is False:
            skipped_streak_updates += 1
            continue
        counted_records += 1
        gift_id = str(event.get("gift_id") or "-")
        raw_name = str(event.get("gift_name_original") or event.get("gift_name") or "unknown")
        row = grouped.setdefault(
            gift_id,
            {
                "gift_id": gift_id,
                "raw_name": raw_name,
                "raw_names": set(),
                "quantity": 0,
                "diamonds": 0,
            },
        )
        row["raw_names"].add(raw_name)
        quantity = max(1, _int(event.get("repeat_count"), 1))
        row["quantity"] += quantity
        if event.get("diamond_total") is not None:
            row["diamonds"] += _int(event.get("diamond_total"))
        else:
            row["diamonds"] += _int(event.get("diamond_count")) * quantity

    rows = list(grouped.values())
    for row in rows:
        metadata = gift_metadata_for(row["gift_id"], row["raw_name"])
        row["raw_names"] = sorted(row["raw_names"])
        chinese_name = metadata.get("name_zh_display")
        english_name = metadata.get("name_en")
        if chinese_name and english_name and str(chinese_name).casefold() != str(english_name).casefold():
            display_name = f"{chinese_name}（{english_name}）"
        elif chinese_name:
            display_name = str(chinese_name)
        else:
            display_name = row["raw_name"]
        if not english_name:
            display_name += "（英文名稱待補）"
        if not chinese_name:
            display_name += "（中文名稱待補）"
        row.update({
            "name_en": english_name,
            "name_zh_display": chinese_name,
            "name_zh_tts": metadata.get("name_zh_tts"),
            "catalog_status": metadata.get("mapping_status") or "needs_review",
            "pending_fields": list(metadata.get("pending_fields") or []),
            "display_name": display_name,
        })
    return rows, counted_records, skipped_streak_updates


def _terminal_gift_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    without_event_id = []
    for row in rows:
        stage = row.get("stage")
        if stage not in TERMINAL_GIFT_FAILURES | TERMINAL_GIFT_SUCCESSES:
            continue
        event_id = str(row.get("event_id") or "").strip()
        if event_id:
            latest[event_id] = row
        elif stage in TERMINAL_GIFT_FAILURES:
            without_event_id.append(row)
    failed = [row for row in latest.values() if row.get("stage") in TERMINAL_GIFT_FAILURES]
    failed.extend(without_event_id)
    return sorted(failed, key=lambda row: (str(row.get("timestamp_local") or ""), str(row.get("event_id") or "")))


def _event_timestamp_ms(event: dict[str, Any]) -> int | None:
    value = event.get("timestamp_ms") or event.get("received_at_ms")
    return int(value) if isinstance(value, (int, float)) else None


def _timeline_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    timed = [(stamp, event) for event in events if (stamp := _event_timestamp_ms(event)) is not None]
    if not timed:
        return {"first_event_at": None, "last_event_at": None, "duration_seconds": None, "phases": []}
    timed.sort(key=lambda row: row[0])
    start, end = timed[0][0], timed[-1][0]
    span = max(0, end - start)
    phases = []
    for index, label in enumerate(("前段", "中段", "後段")):
        phase_start = start + span * index / 3
        phase_end = start + span * (index + 1) / 3
        selected = [
            event for stamp, event in timed
            if phase_start <= stamp < phase_end or (index == 2 and stamp == end)
        ]
        viewers = [
            _int(event.get("viewer_count"))
            for event in selected
            if event.get("type") == "viewer" and isinstance(event.get("viewer_count"), (int, float))
        ]
        counted_gifts = [
            event for event in selected
            if event.get("type") == "gift" and event.get("counted", True) is not False
        ]
        diamonds = sum(
            _int(event.get("diamond_total"))
            if event.get("diamond_total") is not None
            else _int(event.get("diamond_count")) * max(1, _int(event.get("repeat_count"), 1))
            for event in counted_gifts
        )
        likes_received = sum(
            _int(event.get("like_count"))
            for event in selected
            if event.get("type") == "like"
        )
        selected_like_totals = [
            _int(event.get("total_likes"))
            for event in selected
            if event.get("type") == "like" and isinstance(event.get("total_likes"), (int, float))
        ]
        phases.append({
            "label": label,
            "from": _local_time(datetime.fromtimestamp(phase_start / 1000, tz=ZoneInfo("UTC")).isoformat()),
            "to": _local_time(datetime.fromtimestamp(phase_end / 1000, tz=ZoneInfo("UTC")).isoformat()),
            "chat": sum(event.get("type") == "chat" for event in selected),
            "members": sum(event.get("type") == "member" for event in selected),
            "viewer_average": round(sum(viewers) / len(viewers), 1) if viewers else None,
            "viewer_peak": max(viewers) if viewers else None,
            "gift_quantity": sum(max(1, _int(event.get("repeat_count"), 1)) for event in counted_gifts),
            "gift_diamonds": diamonds,
            "likes_received": likes_received,
            "like_total_end": selected_like_totals[-1] if selected_like_totals else None,
        })
    viewer_rows = [(stamp, event) for stamp, event in timed if event.get("type") == "viewer" and isinstance(event.get("viewer_count"), (int, float))]
    gaps = [max(0, right[0] - left[0]) / 1000 for left, right in zip(viewer_rows, viewer_rows[1:])]
    viewer_values = [int(event["viewer_count"]) for _, event in viewer_rows]
    return {
        "first_event_at": _local_time(datetime.fromtimestamp(start / 1000, tz=ZoneInfo("UTC")).isoformat()),
        "last_event_at": _local_time(datetime.fromtimestamp(end / 1000, tz=ZoneInfo("UTC")).isoformat()),
        "duration_seconds": round(span / 1000),
        "viewer_min": min(viewer_values) if viewer_values else None,
        "viewer_average": round(sum(viewer_values) / len(viewer_values), 1) if viewer_values else None,
        "viewer_median": sorted(viewer_values)[(len(viewer_values) - 1) // 2] if viewer_values else None,
        "viewer_gap_p50_seconds": round(sorted(gaps)[(len(gaps) - 1) // 2], 2) if gaps else None,
        "viewer_gap_max_seconds": round(max(gaps), 2) if gaps else None,
        "phases": phases,
    }


def _user_activity_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    unique_users: dict[str, set[str]] = defaultdict(set)
    profile_counts: dict[str, Counter] = defaultdict(Counter)
    gifters: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        user_key = str(event.get("unique_id") or event.get("user_id") or event.get("nickname") or "").strip()
        if event_type in {"chat", "gift", "member"} and user_key:
            unique_users[event_type].add(user_key)
        if event_type in {"chat", "gift", "member"}:
            profile_counts[event_type]["events"] += 1
            if event.get("user_grade_level") is not None:
                profile_counts[event_type]["user_grade"] += 1
            if event.get("fan_club_level") is not None:
                profile_counts[event_type]["fan_club"] += 1
        if event_type != "gift" or event.get("counted", True) is False:
            continue
        key = user_key or "unknown"
        row = gifters.setdefault(key, {
            "user": key,
            "nickname": event.get("nickname"),
            "gift_events": 0,
            "quantity": 0,
            "diamonds": 0,
            "gift_ids": set(),
            "gifts_by_id": {},
        })
        quantity = max(1, _int(event.get("repeat_count"), 1))
        row["gift_events"] += 1
        row["quantity"] += quantity
        row["diamonds"] += (
            _int(event.get("diamond_total"))
            if event.get("diamond_total") is not None
            else _int(event.get("diamond_count")) * quantity
        )
        gift_id = str(event.get("gift_id") or "-")
        row["gift_ids"].add(gift_id)
        gift_row = row["gifts_by_id"].setdefault(gift_id, {
            "gift_id": gift_id,
            "raw_names": set(),
            "quantity": 0,
            "diamonds": 0,
        })
        gift_row["raw_names"].add(str(event.get("gift_name_original") or event.get("gift_name") or "unknown"))
        gift_row["quantity"] += quantity
        gift_row["diamonds"] += (
            _int(event.get("diamond_total"))
            if event.get("diamond_total") is not None
            else _int(event.get("diamond_count")) * quantity
        )
        if event.get("nickname"):
            row["nickname"] = event["nickname"]
    all_gifters = []
    for row in sorted(gifters.values(), key=lambda item: (-item["diamonds"], -item["quantity"], item["user"])):
        itemized_gifts = []
        for gift_row in sorted(row["gifts_by_id"].values(), key=lambda item: (-item["diamonds"], item["gift_id"])):
            raw_names = sorted(gift_row["raw_names"])
            metadata = gift_metadata_for(gift_row["gift_id"], raw_names[0] if raw_names else None)
            chinese_name = metadata.get("name_zh_display")
            english_name = metadata.get("name_en")
            if chinese_name and english_name and str(chinese_name).casefold() != str(english_name).casefold():
                display_name = f"{chinese_name}（{english_name}）"
            else:
                display_name = str(chinese_name or english_name or (raw_names[0] if raw_names else "unknown"))
            itemized_gifts.append({
                **gift_row,
                "raw_names": raw_names,
                "display_name": display_name,
            })
        all_gifters.append({
            "user": row["user"],
            "nickname": row["nickname"],
            "gift_events": row["gift_events"],
            "quantity": row["quantity"],
            "diamonds": row["diamonds"],
            "gift_ids": len(row["gift_ids"]),
            "gifts": itemized_gifts,
        })
    top_gifters = all_gifters[:10]
    return {
        "unique_chatters": len(unique_users["chat"]),
        "unique_gifters": len(unique_users["gift"]),
        "unique_member_users": len(unique_users["member"]),
        "member_user_grade_events": profile_counts["member"]["user_grade"],
        "member_fan_club_events": profile_counts["member"]["fan_club"],
        "profile_field_counts": {
            event_type: dict(profile_counts[event_type])
            for event_type in ("chat", "gift", "member")
        },
        "gifters": all_gifters,
        "top_gifters": top_gifters,
    }


def _latest_rank_snapshot(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for event in reversed(events):
        ranks = event.get("ranks")
        if event.get("type") != "viewer" or not isinstance(ranks, list):
            continue
        result = []
        for item in ranks[:10]:
            if not isinstance(item, dict):
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            result.append({
                "rank": item.get("rank"),
                "user": user.get("display_id") or user.get("unique_id") or user.get("nickname") or "unknown",
                "nickname": user.get("nickname"),
                "score": item.get("score"),
            })
        if result:
            return result
    return []


def build_live_summary(session_dir: Path) -> dict[str, Any]:
    session_dir = Path(session_dir).resolve()
    meta = _read_json(session_dir / "session.json")
    events = _read_ndjson(session_dir / "events.ndjson")
    event_counts = Counter(str(event.get("type") or "unknown") for event in events)
    timeline = _timeline_summary(events)
    users = _user_activity_summary(events)
    gifts, counted_gift_records, skipped_streak_updates = _gift_rows(events)
    viewer_values = [
        _int(event.get("viewer_count"))
        for event in events
        if event.get("type") == "viewer" and isinstance(event.get("viewer_count"), (int, float))
    ]
    like_events = [event for event in events if event.get("type") == "like"]
    like_totals = [
        _int(event.get("total_likes"))
        for event in like_events
        if isinstance(event.get("total_likes"), (int, float))
    ]
    likes_received = sum(_int(event.get("like_count")) for event in like_events)
    likes_baseline_estimate = (
        max(0, like_totals[0] - _int(like_events[0].get("like_count")))
        if like_totals
        else None
    )
    gift_delivery_rows = _read_ndjson(session_dir / "tts_delivery.ndjson")
    event_timing_values: dict[str, list[float]] = {}
    for event in events:
        timings = event.get("processing_timings_ms")
        if not isinstance(timings, dict):
            continue
        for name, value in timings.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                event_timing_values.setdefault(str(name), []).append(float(value))
    stage_timing_values: dict[str, list[float]] = {}
    for row in gift_delivery_rows:
        stage = row.get("stage")
        if stage == "speech_prepared":
            stage_values = row.get("stage_timings_ms") or {}
            if isinstance(stage_values, dict):
                for key in ("tts_name_resolution", "speech_preparation"):
                    value = stage_values.get(key)
                    if isinstance(value, (int, float)):
                        stage_timing_values.setdefault(key, []).append(float(value))
        timing_name = {
            "synthesis_completed": "synthesis",
            "synthesis_segment_completed": "synthesis_segment",
            "playback_completed": "playback",
            "offline_fallback_synthesized": "offline_fallback",
        }.get(str(stage))
        value = row.get("duration_ms")
        if timing_name and isinstance(value, (int, float)):
            stage_timing_values.setdefault(timing_name, []).append(float(value))
    processing_timings = {
        name: _duration_stats(values) for name, values in event_timing_values.items()
    }
    gift_stage_timings = {
        name: _duration_stats(values) for name, values in stage_timing_values.items()
    }
    pending_gifts = [
        {
            "gift_id": row["gift_id"],
            "original_name": row["raw_name"],
            "pending_fields": row["pending_fields"],
        }
        for row in gifts
        if row["catalog_status"] != "mapped"
    ]
    pending_keys = {(row["gift_id"], row["original_name"]) for row in pending_gifts}
    for row in _read_ndjson(session_dir / "gift_catalog_pending.ndjson"):
        key = (str(row.get("gift_id") or "-"), str(row.get("raw_name") or "unknown"))
        if key in pending_keys:
            continue
        pending_keys.add(key)
        pending_gifts.append({
            "gift_id": key[0],
            "original_name": key[1],
            "pending_fields": ["name_zh_display", "name_zh_tts"],
        })
    tts_state = _read_json(TTS_STATE_PATH)
    session_tts_state = _read_json(session_dir / "tts_summary.json")
    snapshot_matches = (
        session_tts_state.get("session_id") == (meta.get("session_id") or session_dir.name)
        and str(session_tts_state.get("username") or "").casefold()
        == str(meta.get("username") or meta.get("streamer_username") or "").casefold()
    )
    state_matches = _session_matches(tts_state, session_dir, meta)
    tts_metrics = (
        session_tts_state.get("metrics", {})
        if snapshot_matches
        else (tts_state.get("metrics", {}) if state_matches else {})
    )
    tts_status = (
        session_tts_state.get("status")
        if snapshot_matches
        else (tts_state.get("status") if state_matches else None)
    )
    live_end = meta.get("live_end_detection") or {}
    delivery_log_path = session_dir / "tts_delivery.ndjson"
    live_end_at = _local_time(live_end.get("timestamp_utc"))
    post_end_seconds = None
    if live_end.get("timestamp_utc") and meta.get("ended_at"):
        try:
            post_end_seconds = max(0, int((datetime.fromisoformat(str(meta["ended_at"]).replace("Z", "+00:00")) - datetime.fromisoformat(str(live_end["timestamp_utc"]).replace("Z", "+00:00"))).total_seconds()))
        except ValueError:
            pass
    return {
        "session_id": meta.get("session_id") or session_dir.name,
        "username": meta.get("username") or meta.get("streamer_username") or "",
        "room_id": meta.get("room_id"),
        "started_at": _local_time(meta.get("started_at")),
        "ended_at": _local_time(meta.get("ended_at")),
        "status": meta.get("status"),
        "live_end_detection": live_end,
        "live_end_at": live_end_at,
        "post_end_capture_seconds": post_end_seconds,
        "timeline": timeline,
        "users": users,
        "latest_rank_snapshot": _latest_rank_snapshot(events),
        "events": {
            "counts": dict(event_counts),
            "chat_with_text": sum(
                bool(event.get("comment")) for event in events if event.get("type") == "chat"
            ),
            "counted_gift_records": counted_gift_records,
            "skipped_gift_streak_updates": skipped_streak_updates,
            "gift_item_quantity": sum(row["quantity"] for row in gifts),
            "gift_diamonds": sum(row["diamonds"] for row in gifts),
            "peak_viewers": max(viewer_values) if viewer_values else None,
            "last_viewers": viewer_values[-1] if viewer_values else None,
            "last_room_user_count": next(
                (
                    _int(event.get("total_user_count"))
                    for event in reversed(events)
                    if event.get("type") == "viewer"
                    and isinstance(event.get("total_user_count"), (int, float))
                ),
                None,
            ),
            "last_total_likes": like_totals[-1] if like_totals else None,
            "likes_received": likes_received,
            "like_total_first_observed": like_totals[0] if like_totals else None,
            "like_baseline_estimate": likes_baseline_estimate,
        },
        "gifts": gifts,
        "gift_catalog_pending": pending_gifts,
        "gift_catalog_pending_log": (
            "gift_catalog_pending.ndjson"
            if (session_dir / "gift_catalog_pending.ndjson").is_file()
            else None
        ),
        "processing_timings_ms": processing_timings,
        "tts": {
            "status": tts_status if tts_metrics else None,
            "metrics": tts_metrics,
            "gift_delivery_log": "tts_delivery.ndjson" if delivery_log_path.is_file() else None,
            "gift_delivery_outcomes": _terminal_gift_outcomes(gift_delivery_rows),
            "gift_delivery_log_records": len(gift_delivery_rows),
            "gift_stage_timings_ms": gift_stage_timings,
            "session_snapshot": "tts_summary.json" if snapshot_matches else None,
        },
    }


def render_live_summary(summary: dict[str, Any]) -> str:
    """Render an end-of-LIVE report with distinct count semantics."""
    events = summary.get("events") or {}
    counts = events.get("counts") or {}
    tts = summary.get("tts") or {}
    metrics = tts.get("metrics") or {}
    timeline = summary.get("timeline") or {}
    users = summary.get("users") or {}
    skipped = metrics.get("skipped") or {}

    def show(value: object, suffix: str = "") -> str:
        if value is None:
            return "\u2014"
        if isinstance(value, float):
            value = f"{value:,.1f}"
        elif isinstance(value, int):
            value = f"{value:,}"
        return f"{value}{suffix}"

    def cell(value: object) -> str:
        return str(value if value is not None else "\u2014").replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        f"# LIVE \u5834\u6b21\u7d71\u6574\uff1a@{cell(summary.get('username') or 'unknown')}",
        "",
        f"- Session\uff1a{cell(summary.get('session_id'))}",
        f"- Room ID\uff1a{cell(summary.get('room_id'))}",
        f"- \u958b\u59cb\uff1a{cell(summary.get('started_at'))}",
        f"- \u7d50\u675f\uff1a{cell(summary.get('ended_at'))}",
        f"- \u7d50\u675f\u5075\u6e2c\uff1a{cell((summary.get('live_end_detection') or {}).get('status') or 'NOT_OBSERVED')}",
        f"- \u7d50\u675f\u8a0a\u865f\u6642\u9593\uff1a{cell(summary.get('live_end_at'))}",
        f"- \u4e8b\u4ef6\u6642\u9593\u7bc4\u570d\uff1a{cell(timeline.get('first_event_at'))} \u81f3 {cell(timeline.get('last_event_at'))}",
        "",
        "## \u5834\u6b21\u7e3d\u89bd",
        "",
        "| \u9805\u76ee | \u6578\u503c | \u8aaa\u660e |",
        "|---|---:|---|",
        f"| \u804a\u5929\u8a0a\u606f | {show(counts.get('chat'))} | \u6709\u5167\u5bb9 {show(events.get('chat_with_text'))} \u5247 |",
        f"| \u804a\u5929\u4eba\u6578 | {show(users.get('unique_chatters'))} | \u4ee5 user ID \u53bb\u91cd |",
        f"| \u79ae\u7269\u4e8b\u4ef6 | {show(events.get('counted_gift_records'))} | \u5df2\u8a08\u5165\u5b8c\u6210\u9023\u9001\u7684\u4e8b\u4ef6 |",
        f"| \u79ae\u7269\u6578\u91cf | {show(events.get('gift_item_quantity'))} | \u6309\u9023\u9001\u6578\u91cf\u52a0\u7e3d |",
        f"| \u79ae\u7269\u947d\u77f3 | {show(events.get('gift_diamonds'))} | \u4f9d\u4e8b\u4ef6 diamond_total \u52a0\u7e3d |",
        f"| \u9001\u79ae\u4eba\u6578 | {show(users.get('unique_gifters'))} | \u4ee5 user ID \u53bb\u91cd |",
        f"| \u9023\u9001\u4e2d\u9593\u66f4\u65b0 | {show(events.get('skipped_gift_streak_updates'))} | \u4e0d\u91cd\u8907\u8a08\u7b97\u672a\u5b8c\u6210\u9023\u9001 |",
        f"| \u89c0\u773e\u6578\u63a1\u6a23 | {show(counts.get('viewer'))} | \u7576\u4e0b\u89c0\u773e\u4eba\u6578\u6a23\u672c |",
        f"| \u89c0\u773e\u5cf0\u503c | {show(events.get('peak_viewers'))} | \u4f9d\u6536\u5230\u7684 viewer_count \u6a23\u672c |",
        f"| \u89c0\u773e\u5e73\u5747\uff0f\u4e2d\u4f4d\uff0f\u6700\u4f4e | {show(timeline.get('viewer_average'))} / {show(timeline.get('viewer_median'))} / {show(timeline.get('viewer_min'))} | \u4f9d\u6536\u5230\u7684 viewer_count \u6a23\u672c |",
        f"| \u7d2f\u8a08\u623f\u9593\u4eba\u6578 | {show(events.get('last_room_user_count'))} | TikTok total_user_count\uff0c\u8207\u7576\u4e0b\u89c0\u773e\u6578\u5206\u958b |",
        f"| Like \u4e8b\u4ef6 | {show(counts.get('like'))} | TikTok \u50b3\u4f86\u7684 Like \u66f4\u65b0\u5c01\u5305\u6578 |",
        f"| Like \u89c0\u5bdf\u589e\u91cf | {show(events.get('likes_received'))} | \u52a0\u7e3d like_count |",
        f"| Like \u6700\u521d\uff0f\u6700\u5f8c\u7d2f\u8a08\u503c | {show(events.get('like_total_first_observed'))} / {show(events.get('last_total_likes'))} | \u9996\u6b21\u6536\u5230\u7684\u7d2f\u8a08\u503c\u53ef\u80fd\u975e\u96f6 |",
        f"| Like \u958b\u5834\u524d\u4f30\u503c | {show(events.get('like_baseline_estimate'))} | \u6700\u521d\u7d2f\u8a08\u503c\u6263\u9664\u8a72\u5c01\u5305\u589e\u91cf\uff0c\u50c5\u4f30\u7b97 |",
        f"| \u65b0\u9032\u623f\u89c0\u773e\uff08Member\uff09 | {show(counts.get('member'))} | \u4ee5\u6536\u5230\u7684 member \u4e8b\u4ef6\u8a08 |",
        "",
        "## \u4eba\u6578\u3001\u79ae\u7269\u8207 Like \u8da8\u52e2",
        "",
        "| \u6642\u6bb5 | \u804a\u5929 | \u89c0\u773e\u5e73\u5747 | \u89c0\u773e\u5cf0\u503c | \u79ae\u7269\u6578\u91cf | \u947d\u77f3 | Like \u589e\u91cf | \u671f\u672b Like \u7d2f\u8a08 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for phase in timeline.get("phases") or []:
        lines.append(
            f"| {cell(phase.get('label'))} ({cell(phase.get('from'))} \u81f3 {cell(phase.get('to'))}) | "
            f"{show(phase.get('chat'))} | {show(phase.get('viewer_average'))} | {show(phase.get('viewer_peak'))} | "
            f"{show(phase.get('gift_quantity'))} | {show(phase.get('gift_diamonds'))} | "
            f"{show(phase.get('likes_received'))} | {show(phase.get('like_total_end'))} |"
        )

    lines.extend([
        "",
        "## \u79ae\u7269\u660e\u7d30",
        "",
        "\u6578\u91cf\u6309\u9023\u9001\u6578\u52a0\u7e3d\uff0c\u947d\u77f3\u4f9d\u4e8b\u4ef6\u50b3\u4f86\u7684 diamond_total \u52a0\u7e3d\u3002",
        "",
        "| Gift ID | \u4e2d\u82f1\u6587\u540d\u7a31 | \u539f\u59cb\u540d\u7a31 | \u6578\u91cf | \u947d\u77f3 |",
        "|---:|---|---|---:|---:|",
    ])
    gifts = summary.get("gifts") or []
    for row in sorted(gifts, key=lambda item: (-_int(item.get("diamonds")), str(item.get("gift_id")))):
        raw_names = row.get("raw_names") or [row.get("raw_name") or ""]
        lines.append(
            f"| {cell(row.get('gift_id'))} | {cell(row.get('display_name'))} | "
            f"{cell(', '.join(raw_names))} | {show(row.get('quantity'))} | {show(row.get('diamonds'))} |"
        )
    lines.append(
        f"| **\u5408\u8a08** | **{len(gifts)} \u7a2e** |  | **{show(events.get('gift_item_quantity'))}** | **{show(events.get('gift_diamonds'))}** |"
    )

    top_gifters = users.get("top_gifters") or []
    if top_gifters:
        lines.extend([
            "",
            "## \u9001\u79ae\u6392\u540d\uff08\u524d 10 \u540d\uff09",
            "",
            "| \u5e33\u865f | \u986f\u793a\u540d | \u79ae\u7269\u4e8b\u4ef6 | \u79ae\u7269\u6578\u91cf | \u7a2e\u985e | \u947d\u77f3 |",
            "|---|---|---:|---:|---:|---:|",
        ])
        for row in top_gifters:
            lines.append(
                f"| @{cell(row.get('user'))} | {cell(row.get('nickname'))} | {show(row.get('gift_events'))} | "
                f"{show(row.get('quantity'))} | {show(row.get('gift_ids'))} | {show(row.get('diamonds'))} |"
            )

    all_gifters = users.get("gifters") or []
    if all_gifters:
        lines.extend([
            "",
            "## \u6bcf\u4f4d\u9001\u79ae\u8005\u7684\u79ae\u7269\u660e\u7d30",
            "",
            "| \u5e33\u865f | \u986f\u793a\u540d | Gift ID | \u79ae\u7269\u540d\u7a31 | \u6578\u91cf | \u947d\u77f3 |",
            "|---|---|---:|---|---:|---:|",
        ])
        for user in all_gifters:
            for gift in user.get("gifts") or []:
                lines.append(
                    f"| @{cell(user.get('user'))} | {cell(user.get('nickname'))} | {cell(gift.get('gift_id'))} | "
                    f"{cell(gift.get('display_name'))} | {show(gift.get('quantity'))} | {show(gift.get('diamonds'))} |"
                )

    rank_rows = summary.get("latest_rank_snapshot") or []
    if rank_rows:
        lines.extend([
            "",
            "## \u6700\u5f8c\u6536\u5230\u7684\u6392\u540d\u8cc7\u6599",
            "",
            "| \u6392\u540d | \u5e33\u865f | \u986f\u793a\u540d | Score |",
            "|---:|---|---|---:|",
        ])
        for row in rank_rows:
            lines.append(
                f"| {show(row.get('rank'))} | @{cell(row.get('user'))} | {cell(row.get('nickname'))} | {show(row.get('score'))} |"
            )

    pending_gifts = summary.get("gift_catalog_pending") or []
    if pending_gifts:
        lines.extend([
            "",
            "## \u5f85\u88dc\u4e2d\u82f1\u6587\u5c0d\u7167\u7684 Gift ID",
            "",
            "| Gift ID | TikTok \u539f\u540d | \u5f85\u88dc\u6b04\u4f4d |",
            "|---:|---|---|",
        ])
        for row in pending_gifts:
            lines.append(
                f"| {cell(row.get('gift_id'))} | {cell(row.get('original_name'))} | "
                f"{cell(', '.join(row.get('pending_fields') or []))} |"
            )
        if summary.get("gift_catalog_pending_log"):
            lines.append(f"\n\u6b64\u5834\u6b21\u65b0\u767c\u73fe\u6e05\u55ae\uff1a{summary['gift_catalog_pending_log']}")

    lines.extend(["", "## TTS \u9001\u9054\u72c0\u614b", ""])
    if not metrics:
        lines.append("\u6b64\u5834\u6b21\u6c92\u6709\u5c0d\u61c9\u7684 TTS worker \u72c0\u614b\u6216\u9010\u7b46\u9001\u9054\u8a18\u9304\u3002")
    else:
        lines.extend([
            f"- Worker\uff1a{cell(tts.get('status') or 'unknown')}",
            f"- \u804a\u5929\uff1a\u6536\u5230 {show(metrics.get('chat_events_seen'))}\uff0c\u4f47\u5217 {show(metrics.get('chat_queued'))}\uff0c\u5df2\u64ad {show(metrics.get('chat_spoken'))}\uff0c\u904e\u671f\u7565\u904e {show(skipped.get('expired_chat', 0))}",
            f"- Gift\uff1a\u6536\u5230 {show(metrics.get('gift_events_seen'))}\uff0c\u4f47\u5217 {show(metrics.get('gift_queued'))}\uff0c\u5df2\u64ad {show(metrics.get('gift_spoken'))}\uff0c\u6700\u7d42\u5931\u6557 {show(metrics.get('gift_delivery_failures'))}",
            f"- \u96e2\u7dda\u8a9e\u97f3\u5099\u63f4\uff1a\u5617\u8a66 {show(metrics.get('gift_offline_fallback_attempts'))}\uff0c\u5408\u6210\u6210\u529f {show(metrics.get('gift_offline_fallback_succeeded'))}\uff0c\u5931\u6557 {show(metrics.get('gift_offline_fallback_failures'))}",
            f"- \u4f47\u5217\u5c16\u5cf0\uff1a{show(metrics.get('max_queue_depth'))}/{show(metrics.get('queue_capacity'))}\uff0c\u4f47\u5217\u6eff\u6642\u7b49\u5f85 {show(metrics.get('queue_full_waits'))} \u6b21\uff0c\u6700\u9577\u4f47\u5217\u7b49\u5f85 {show(metrics.get('max_queue_wait_ms'), ' ms')}",
            f"- \u4e8b\u4ef6\u5230\u64ad\u653e\uff1aP50 {show(metrics.get('p50_event_to_playback_ms'), ' ms')}\uff0cP95 {show(metrics.get('p95_event_to_playback_ms'), ' ms')}\uff0c\u6700\u9577 {show(metrics.get('max_event_to_playback_ms'), ' ms')}\uff08\u6700\u8fd1\u4fdd\u7559\u6a23\u672c\uff09",
            f"- \u4f47\u5217\u7b49\u5f85 P95\uff1a{show(metrics.get('p95_queue_wait_ms'), ' ms')}\uff1b\u5408\u6210 P95\uff1a{show(metrics.get('p95_synthesis_ms'), ' ms')}\uff1b\u64ad\u653e P95\uff1a{show(metrics.get('p95_audio_playback_ms'), ' ms')}",
            f"- \u672a\u5b8c\u6210 Gift \u7b49\u5f85\u91cd\u64ad\uff1a{show(metrics.get('pending_gift_replay_count'))}\uff1b\u4e8b\u4ef6\u4f4d\u79fb\u5132\u5b58\u5931\u6557\uff1a{show(metrics.get('cursor_write_errors'))}",
        ])
        if tts.get("gift_delivery_log"):
            lines.append(f"- \u9010\u7b46\u65e5\u8a8c\uff1a{tts['gift_delivery_log']}")

    outcomes = tts.get("gift_delivery_outcomes") or []
    if outcomes:
        lines.extend([
            "",
            "### \u672a\u6210\u529f\u64ad\u51fa\u7684 Gift",
            "",
            "| \u6642\u9593 | Event ID | Gift ID | \u539f\u540d | \u6578\u91cf | \u947d\u77f3 | \u5931\u6557\u968e\u6bb5 |",
            "|---|---|---:|---|---:|---:|---|",
        ])
        for row in outcomes:
            lines.append(
                f"| {cell(row.get('timestamp_local'))} | {cell(row.get('event_id'))} | {cell(row.get('gift_id'))} | "
                f"{cell(row.get('gift_name_original') or row.get('gift_name'))} | {show(row.get('quantity'))} | "
                f"{show(row.get('diamond_total'))} | {cell(row.get('stage'))}: {cell(row.get('error'))} |"
            )
    elif metrics.get("gift_delivery_failures"):
        lines.append("\n\u6b64\u5834\u6b21\u6c92\u6709\u9010\u7b46\u5931\u6557\u65e5\u8a8c\uff1b\u53ea\u80fd\u77e5\u9053\u64ad\u5831\u5931\u6557\u6b21\u6578\uff0c\u7121\u6cd5\u56de\u6eaf\u5c0d\u61c9 Gift ID\u3002")

    timings = tts.get("gift_stage_timings_ms") or {}
    if timings:
        lines.extend([
            "",
            "### Gift TTS \u968e\u6bb5\u8017\u6642",
            "",
            "| \u968e\u6bb5 | P50 ms | P95 ms | \u7b46\u6578 |",
            "|---|---:|---:|---:|",
        ])
        for key, label in (
            ("tts_name_resolution", "\u79ae\u7269\u540d\u7a31\u9078\u64c7"),
            ("speech_preparation", "\u6587\u5b57\u6e05\u7406"),
            ("synthesis_segment", "\u55ae\u6bb5\u5408\u6210"),
            ("synthesis", "\u6574\u7b46\u5408\u6210"),
            ("offline_fallback", "\u96e2\u7dda\u5099\u63f4\u5408\u6210"),
            ("playback", "\u97f3\u8a0a\u64ad\u653e"),
        ):
            row = timings.get(key)
            if row:
                lines.append(
                    f"| {label} | {show(row.get('p50'))} | {show(row.get('p95'))} | {show(row.get('count'))} |"
                )

    lines.extend([
        "",
        "---",
        "",
        "\u89c0\u773e\u6578\u8207 Like \u4f9d\u672c\u5834\u5be6\u969b\u6536\u5230\u7684 TikTok \u4e8b\u4ef6\u5efa\u7acb\u8da8\u52e2\uff1b\u672a\u50b3\u9001\u7684\u6b04\u4f4d\u4e0d\u6703\u88dc\u70ba 0\u3002",
        "",
    ])
    return "\n".join(lines)

def write_live_summary(session_dir: Path) -> Path:
    session_dir = Path(session_dir).resolve()
    summary = build_live_summary(session_dir)
    output = session_dir / "live_summary.md"
    temporary = output.with_suffix(".md.tmp")
    temporary.write_text(render_live_summary(summary), encoding="utf-8")
    temporary.replace(output)
    return output


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = write_live_summary(args.session_dir)
    print(f"[REPORT] {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
