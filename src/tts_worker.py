"""Optional TTS action worker that tails collector NDJSON without a second LIVE connection."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.live_summary import write_live_summary
from src.tts.audio import (
    EdgeTTSBackend,
    PygameAudioPlayer,
    WindowsSystemSpeechBackend,
)
from src.tts.pipeline import (
    ChatProcessor,
    PreparedChat,
    TTSSettings,
    dominant_language,
    member_entry_sound,
    should_speak_chat,
    spoken_gift_name,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "tts"
RAW_ROOT = ROOT / "data" / "raw"
BENCHMARK_ROOT = ROOT / "data" / "v2_provider_benchmark"
BROWSER_SESSION_ROOT = ROOT / "data" / "browser_ws_probe"
WATCHER_STATE = ROOT / "data" / "watcher" / "watcher_state.json"
CONFIG_PATH = DATA_ROOT / "config.json"
PREVIEW_PATH = DATA_ROOT / "preview_request.json"
STATE_PATH = DATA_ROOT / "state.json"
STOP_PATH = DATA_ROOT / "stop.json"
REPLAY_HANDOFF_PATH = DATA_ROOT / "replay_handoff.json"
UNMAPPED_GIFTS_PATH = DATA_ROOT / "unmapped_gifts.ndjson"
LOCK_PATH = DATA_ROOT / "worker.lock"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
GIFT_DELIVERY_RETRY_LIMIT = 1
GIFT_DELIVERY_RETRY_DELAY_SECONDS = 5
TTS_RETRY_BACKOFF_SECONDS = (1, 2, 4)


@dataclass
class PreparedAnnouncement:
    sequence: int
    message: PreparedChat
    event_arrived_at: float
    event_type: str
    audio_paths: list[Path]


def percentile_ms(values, percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return int(ordered[index])


def now_local() -> str:
    return datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    retry_delays = (0.02, 0.04, 0.08, 0.16, 0.32)
    for attempt, delay in enumerate(retry_delays):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == len(retry_delays) - 1:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            time.sleep(delay)


def _tts_error_label(error: Exception) -> str:
    return type(error).__name__


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _active_session_metadata(path: Path, username: str, watcher_started_at: str | None):
    try:
        metadata = read_json(path / "session.json", {})
        if str(metadata.get("username", "")).lstrip("@").casefold() != username.casefold():
            return None
        if metadata.get("status") != "running":
            return None
        if not (path / "events.ndjson").is_file():
            return None
        session_start = metadata.get("collector_started_at_utc")
        if watcher_started_at and session_start:
            try:
                watcher_dt = datetime.fromisoformat(watcher_started_at.replace("Z", "+00:00"))
                session_dt = datetime.fromisoformat(session_start.replace("Z", "+00:00"))
                if session_dt.timestamp() < watcher_dt.timestamp() - 10:
                    return None
            except (TypeError, ValueError):
                pass
        return metadata
    except OSError:
        return None


def find_active_session(
    raw_root: Path,
    watcher_state_path: Path,
    username: str,
    preferred: Path | None = None,
) -> tuple[Path | None, str]:
    watcher = read_json(watcher_state_path, {})
    streamers = watcher.get("streamers", {}) if isinstance(watcher, dict) else {}
    streamer = next(
        (
            value
            for key, value in streamers.items()
            if str(key).lstrip("@").casefold() == username.casefold()
        ),
        {},
    )
    if streamer.get("status") != "collecting":
        return None, "waiting_for_collector"

    watcher_started_at = streamer.get("collector_started_at_utc")
    if preferred and _active_session_metadata(preferred, username, watcher_started_at):
        return preferred, "collecting"

    candidates = []
    try:
        children = raw_root.iterdir()
        for path in children:
            if not path.is_dir():
                continue
            metadata = _active_session_metadata(path, username, watcher_started_at)
            if metadata:
                candidates.append((str(metadata.get("collector_started_at_utc") or ""), path))
    except OSError:
        return None, "waiting_for_session_file"

    if not candidates:
        return None, "waiting_for_session_file"
    return max(candidates, key=lambda item: item[0])[1], "collecting"


class TTSWorker:
    def __init__(
        self,
        username: str,
        session_dir: Path | None = None,
        browser_session_dir: Path | None = None,
    ):
        self.username = username
        self.benchmark_session_dir = None
        self.browser_session_dir = None
        if session_dir is not None and browser_session_dir is not None:
            raise ValueError("choose either --session-dir or --browser-session-dir")
        if session_dir is not None:
            resolved = session_dir.resolve()
            if not resolved.is_relative_to(BENCHMARK_ROOT.resolve()):
                raise ValueError("benchmark session must be under data/v2_provider_benchmark")
            if not resolved.name.casefold().endswith(f"_{username.casefold()}"):
                raise ValueError("benchmark session username does not match the worker username")
            if not (resolved / "events.ndjson").is_file():
                raise FileNotFoundError("benchmark session has no events.ndjson")
            self.benchmark_session_dir = resolved
        if browser_session_dir is not None:
            resolved = browser_session_dir.resolve()
            if not resolved.is_relative_to(BROWSER_SESSION_ROOT.resolve()):
                raise ValueError(
                    "browser session must be under data/browser_ws_probe"
                )
            if not resolved.name.casefold().endswith(f"_{username.casefold()}"):
                raise ValueError("browser session username does not match the worker username")
            if not (resolved / "events.ndjson").is_file():
                raise FileNotFoundError("browser session has no events.ndjson")
            self.browser_session_dir = resolved
        self.source_session_dir = (
            self.benchmark_session_dir or self.browser_session_dir
        )
        raw = read_json(CONFIG_PATH, {})
        self.settings = TTSSettings.from_mapping(raw)
        self.processor = ChatProcessor(self.settings)
        self.queue: asyncio.PriorityQueue[
            tuple[tuple[int, int], int, PreparedChat, float, float, str]
        ] = asyncio.PriorityQueue(
            maxsize=self.settings.queue_size
        )
        self.queue_sequence = 0
        self.pending_queue_times: dict[int, float] = {}
        self.pending_gift_replay_ids: set[str] = set()
        self.queued_gift_ids: set[str] = set()
        self.completed_gift_ids: set[str] = set()
        self.resume_existing_session = False
        self.tail_position = 0
        self.pending_catalog_keys: set[tuple[str, str]] | None = None
        self.last_preview_id = None
        self.backend = EdgeTTSBackend()
        self.offline_backend = WindowsSystemSpeechBackend()
        self.player = PygameAudioPlayer()
        self.session_path: Path | None = None
        self.last_attached_session_dir: Path | None = None
        self.file_handle = None
        self.pending_bytes = b""
        self.first_session = True
        self.metrics = {
            "chat_events_seen": 0,
            "chat_filtered_by_settings": 0,
            "gift_events_seen": 0,
            "member_events_seen": 0,
            "member_entry_sounds_queued": 0,
            "member_entry_sounds_played": 0,
            "member_entry_sound_errors": 0,
            "gift_streak_events_skipped": 0,
            "chat_queued": 0,
            "gift_queued": 0,
            "spoken": 0,
            "chat_spoken": 0,
            "gift_spoken": 0,
            "preview_queued": 0,
            "preview_spoken": 0,
            "synthesis_or_playback_errors": 0,
            "delivery_failures": 0,
            "gift_delivery_failures": 0,
            "gift_synthesis_failures": 0,
            "gift_playback_failures": 0,
            "gift_retry_queued": 0,
            "gift_offline_fallback_attempts": 0,
            "gift_offline_fallback_succeeded": 0,
            "gift_offline_fallback_failures": 0,
            "chat_offline_fallback_attempts": 0,
            "chat_offline_fallback_succeeded": 0,
            "chat_offline_fallback_failures": 0,
            "gift_failure_alerts_played": 0,
            "gift_failure_alert_errors": 0,
            "gift_replay_unavailable": 0,
            "gift_followup_sounds_played": 0,
            "gift_followup_sound_errors": 0,
            "skipped": Counter(),
            "latencies_ms": deque(maxlen=1000),
            "event_to_queue_latencies_ms": deque(maxlen=1000),
            "queue_wait_latencies_ms": deque(maxlen=1000),
            "synthesis_latencies_ms": deque(maxlen=1000),
            "audio_playback_latencies_ms": deque(maxlen=1000),
            "is_speaking": False,
            "is_synthesizing": False,
            "last_chat_at_local": None,
            "last_spoken_at_local": None,
            "last_error": None,
            "tts_retry_after_local": None,
            "max_queue_depth": 0,
            "queue_full_waits": 0,
            "max_queue_wait_ms": 0,
            "max_event_to_playback_ms": 0,
            "cursor_write_errors": 0,
        }
        self.last_synthesis_request_at = 0.0
        self.backend_failures = 0
        self.backend_retry_at = 0.0
        self.state = {
            "pid": os.getpid(),
            "username": username,
            "source_session_dir": str(self.source_session_dir) if self.source_session_dir else None,
            "status": "starting",
            "started_at_local": now_local(),
            "updated_at_local": now_local(),
            "active_session": None,
            "metrics": {},
        }
        self.last_session_snapshot_monotonic = 0.0

    def update_state(self, status: str | None = None, **values) -> None:
        if status is not None:
            self.state["status"] = status
        self.state.update(values)
        latency = self.metrics["latencies_ms"]
        queue_latency = self.metrics["event_to_queue_latencies_ms"]
        queue_wait = self.metrics["queue_wait_latencies_ms"]
        synthesis_latency = self.metrics["synthesis_latencies_ms"]
        playback_latency = self.metrics["audio_playback_latencies_ms"]
        self.state["metrics"] = {
            "chat_events_seen": self.metrics["chat_events_seen"],
            "chat_filtered_by_settings": self.metrics["chat_filtered_by_settings"],
            "gift_events_seen": self.metrics["gift_events_seen"],
            "member_events_seen": self.metrics["member_events_seen"],
            "member_entry_sounds_queued": self.metrics["member_entry_sounds_queued"],
            "member_entry_sounds_played": self.metrics["member_entry_sounds_played"],
            "member_entry_sound_errors": self.metrics["member_entry_sound_errors"],
            "gift_streak_events_skipped": self.metrics["gift_streak_events_skipped"],
            "chat_queued": self.metrics["chat_queued"],
            "gift_queued": self.metrics["gift_queued"],
            "spoken": self.metrics["spoken"],
            "chat_spoken": self.metrics["chat_spoken"],
            "gift_spoken": self.metrics["gift_spoken"],
            "preview_queued": self.metrics["preview_queued"],
            "preview_spoken": self.metrics["preview_spoken"],
            "synthesis_or_playback_errors": self.metrics[
                "synthesis_or_playback_errors"
            ],
            "delivery_failures": self.metrics["delivery_failures"],
            "gift_delivery_failures": self.metrics["gift_delivery_failures"],
            "gift_synthesis_failures": self.metrics["gift_synthesis_failures"],
            "gift_playback_failures": self.metrics["gift_playback_failures"],
            "gift_retry_queued": self.metrics["gift_retry_queued"],
            "gift_offline_fallback_attempts": self.metrics["gift_offline_fallback_attempts"],
            "gift_offline_fallback_succeeded": self.metrics["gift_offline_fallback_succeeded"],
            "gift_offline_fallback_failures": self.metrics["gift_offline_fallback_failures"],
            "chat_offline_fallback_attempts": self.metrics["chat_offline_fallback_attempts"],
            "chat_offline_fallback_succeeded": self.metrics["chat_offline_fallback_succeeded"],
            "chat_offline_fallback_failures": self.metrics["chat_offline_fallback_failures"],
            "cursor_write_errors": self.metrics["cursor_write_errors"],
            "gift_failure_alerts_played": self.metrics["gift_failure_alerts_played"],
            "gift_failure_alert_errors": self.metrics["gift_failure_alert_errors"],
            "gift_replay_unavailable": self.metrics["gift_replay_unavailable"],
            "pending_gift_replay_count": len(self.pending_gift_replay_ids),
            "gift_followup_sounds_played": self.metrics["gift_followup_sounds_played"],
            "gift_followup_sound_errors": self.metrics["gift_followup_sound_errors"],
            "skipped": dict(self.metrics["skipped"]),
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.settings.queue_size,
            "max_queue_depth": self.metrics["max_queue_depth"],
            "queue_full_waits": self.metrics["queue_full_waits"],
            "oldest_queue_age_seconds": self._oldest_queue_age_seconds(),
            "is_speaking": self.metrics["is_speaking"],
            "is_synthesizing": self.metrics["is_synthesizing"],
            "average_event_to_playback_ms": (
                round(sum(latency) / len(latency)) if latency else None
            ),
            "p50_event_to_playback_ms": percentile_ms(latency, 50),
            "p95_event_to_playback_ms": percentile_ms(latency, 95),
            "max_event_to_playback_ms": self.metrics["max_event_to_playback_ms"],
            "p50_event_to_queue_ms": percentile_ms(queue_latency, 50),
            "p95_event_to_queue_ms": percentile_ms(queue_latency, 95),
            "p50_queue_wait_ms": percentile_ms(queue_wait, 50),
            "p95_queue_wait_ms": percentile_ms(queue_wait, 95),
            "max_queue_wait_ms": self.metrics["max_queue_wait_ms"],
            "p50_synthesis_ms": percentile_ms(synthesis_latency, 50),
            "p95_synthesis_ms": percentile_ms(synthesis_latency, 95),
            "p50_audio_playback_ms": percentile_ms(playback_latency, 50),
            "p95_audio_playback_ms": percentile_ms(playback_latency, 95),
            "last_chat_at_local": self.metrics["last_chat_at_local"],
            "last_spoken_at_local": self.metrics["last_spoken_at_local"],
            "last_error": self.metrics["last_error"],
            "tts_retry_after_local": self.metrics["tts_retry_after_local"],
        }
        self.state["updated_at_local"] = now_local()
        write_json(STATE_PATH, self.state)
        self._write_session_tts_snapshot()

    def _write_session_tts_snapshot(self, *, force: bool = False) -> None:
        session_dir = (
            self.source_session_dir
            or self.session_path
            or self.last_attached_session_dir
        )
        if session_dir is None:
            return
        now = time.monotonic()
        if not force and now - self.last_session_snapshot_monotonic < 5.0:
            return
        payload = {
            "username": self.username,
            "session_id": session_dir.name,
            "status": self.state.get("status"),
            "started_at_local": self.state.get("started_at_local"),
            "updated_at_local": self.state.get("updated_at_local"),
            "metrics": self.state.get("metrics") or {},
        }
        try:
            write_json(session_dir / "tts_summary.json", payload)
            self.last_session_snapshot_monotonic = now
        except OSError as error:
            print(f"[tts-session-summary-error] {type(error).__name__}", flush=True)

    def _oldest_queue_age_seconds(self) -> float:
        if not self.pending_queue_times:
            return 0.0
        age = time.monotonic() - min(self.pending_queue_times.values())
        return round(max(0.0, age), 3)

    @staticmethod
    def _log_gift_stage(
        message: PreparedChat,
        stage: str,
        detail: str | None = None,
        *,
        attempt: int | None = None,
        error: Exception | str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        if not message.gift_id and not message.event_id:
            return
        safe_id = "".join(
            char
            for char in str(message.event_id or "")
            if char.isalnum() or char in "-_."
        )[:64] or "-"
        safe_gift_id = "".join(
            char
            for char in str(message.gift_id or "")
            if char.isalnum() or char in "-_."
        )[:64] or "-"
        safe_name = " ".join(
            str(message.spoken_gift_name or message.gift_name or "unknown").split()
        )[:100]
        quantity = message.gift_quantity or 1
        diamond_total = message.gift_diamond_total
        suffix = (
            f" gift_name={safe_name!r} quantity={quantity}"
            f" diamonds={diamond_total if diamond_total is not None else '-'}"
        )
        if attempt is not None:
            suffix += f" attempt={attempt}"
        if duration_ms is not None:
            suffix += f" duration_ms={max(0.0, float(duration_ms)):.3f}"
        if error is not None:
            suffix += f" error={_tts_error_label(error) if isinstance(error, Exception) else str(error)[:100]}"
        if detail:
            suffix += f" {detail}"
        print(
            f"[tts-gift] event_id={safe_id} gift_id={safe_gift_id} stage={stage}{suffix}",
            flush=True,
        )
        if not message.source_session_dir:
            return
        record = {
            "timestamp_local": now_local(),
            "session_id": message.session_id,
            "event_id": message.event_id,
            "gift_id": message.gift_id,
            "gift_name": message.gift_name,
            "gift_name_original": (
                message.gift_name_original
                if message.gift_name_original is not None
                else message.gift_name
            ),
            "gift_name_en": message.gift_name_en,
            "gift_name_zh_display": message.gift_name_zh_display,
            "gift_name_zh_tts": message.gift_name_zh_tts,
            "spoken_gift_name": message.spoken_gift_name,
            "gift_catalog_status": message.gift_catalog_status,
            "quantity": quantity,
            "diamond_total": diamond_total,
            "stage": stage,
            "attempt": attempt,
            "retry_attempt": message.retry_attempts,
            "duration_ms": (
                round(max(0.0, float(duration_ms)), 3)
                if duration_ms is not None
                else None
            ),
            "stage_timings_ms": {
                "protobuf_envelope": dict(message.processing_timings_ms).get("protobuf_envelope_ms"),
                "protobuf_event": dict(message.processing_timings_ms).get("protobuf_event_ms"),
                "event_normalization": dict(message.processing_timings_ms).get("event_normalization_ms"),
                "catalog_lookup": dict(message.processing_timings_ms).get("gift_name_resolution_ms"),
                "tts_name_resolution": message.gift_name_resolution_ms,
                "speech_preparation": message.speech_preparation_ms,
            },
            "speech_segment_count": len(message.segments),
            "speech_segmentation_fallback": message.speech_segmentation_fallback,
            "error": (
                _tts_error_label(error)
                if isinstance(error, Exception)
                else (str(error)[:100] if error is not None else None)
            ),
            "detail": detail,
        }
        try:
            log_path = Path(message.source_session_dir) / "tts_delivery.ndjson"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
        except OSError as log_error:
            print(
                f"[tts-gift-log-error] event_id={safe_id} error={type(log_error).__name__}",
                flush=True,
            )

    @staticmethod
    def _log_chat_stage(
        message: PreparedChat,
        stage: str,
        detail: str | None = None,
        *,
        attempt: int | None = None,
        error: Exception | str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        if not message.source_session_dir:
            return
        record = {
            "timestamp_local": now_local(),
            "session_id": message.session_id,
            "event_id": message.event_id,
            "user_key": message.user_key,
            "stage": stage,
            "attempt": attempt,
            "duration_ms": (
                round(max(0.0, float(duration_ms)), 3)
                if duration_ms is not None
                else None
            ),
            "error": (
                _tts_error_label(error)
                if isinstance(error, Exception)
                else (str(error)[:100] if error is not None else None)
            ),
            "detail": detail,
            "speech_segment_count": len(message.segments),
            "speech_segment_lengths": [len(text) for text, _voice in message.segments],
        }
        try:
            log_path = Path(message.source_session_dir) / "chat_tts_delivery.ndjson"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
        except OSError as log_error:
            print(
                f"[tts-chat-log-error] event_id={message.event_id or '-'} error={type(log_error).__name__}",
                flush=True,
            )

    def _enqueue_gift_retry(
        self, message: PreparedChat, event_arrived_at: float
    ) -> bool:
        if message.retry_attempts >= GIFT_DELIVERY_RETRY_LIMIT:
            return False
        sequence = self._next_queue_sequence()
        queued_at = time.monotonic()
        retry_message = replace(message, retry_attempts=message.retry_attempts + 1)
        order_at_ms = retry_message.received_at_ms or int(time.time() * 1000)
        try:
            self.queue.put_nowait(
                (
                    (int(order_at_ms), sequence),
                    sequence,
                    retry_message,
                    event_arrived_at,
                    queued_at,
                    "gift",
                )
            )
        except asyncio.QueueFull:
            return False
        self.pending_queue_times[sequence] = queued_at
        self.metrics["gift_retry_queued"] += 1
        retry_at = queued_at + GIFT_DELIVERY_RETRY_DELAY_SECONDS
        self.backend_retry_at = max(self.backend_retry_at, retry_at)
        self.metrics["tts_retry_after_local"] = (
            datetime.now(TAIPEI_TZ)
            + timedelta(seconds=GIFT_DELIVERY_RETRY_DELAY_SECONDS)
        ).isoformat(timespec="seconds")
        return True

    def _record_worker_stopped_queue(self) -> None:
        while True:
            try:
                _, sequence, message, _, _, event_type = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self.pending_queue_times.pop(sequence, None)
            self.metrics["skipped"]["worker_stopped"] += 1
            if event_type in {"chat", "gift"}:
                self.metrics["delivery_failures"] += 1
                if event_type == "gift":
                    self.metrics["gift_delivery_failures"] += 1
                    self._log_gift_stage(message, "worker_stopped")
                    self._remember_pending_gift_replay(message)
            self.queue.task_done()

    def _write_gift_replay_handoff(self) -> None:
        if not self.pending_gift_replay_ids:
            return
        session_dir = self.source_session_dir or self.session_path
        if session_dir is None:
            return
        payload = {
            "username": self.username,
            "session_dir": str(session_dir.resolve()),
            "pending_msg_ids": sorted(self.pending_gift_replay_ids),
            "created_at_local": now_local(),
        }
        try:
            write_json(REPLAY_HANDOFF_PATH, payload)
        except OSError as error:
            print(f"[tts] Gift replay handoff failed: {type(error).__name__}", flush=True)
            return
        print(
            f"[tts] saved pending Gift replay handoff={len(self.pending_gift_replay_ids)}",
            flush=True,
        )

    def _remember_pending_gift_replay(self, message: PreparedChat) -> None:
        if not message.event_id:
            self.metrics["gift_replay_unavailable"] += 1
            self._log_gift_stage(message, "replay_unavailable", "reason=missing_event_id")
            return
        self.pending_gift_replay_ids.add(message.event_id)
        # Persist as soon as delivery is known to have failed so a later crash
        # cannot discard the retry handoff that would otherwise be written at shutdown.
        self._write_gift_replay_handoff()

    def _mark_gift_delivered(self, message: PreparedChat) -> None:
        if not message.event_id:
            return
        self.completed_gift_ids.add(message.event_id)
        self.queued_gift_ids.discard(message.event_id)
        self.pending_gift_replay_ids.discard(message.event_id)
        handoff = read_json(REPLAY_HANDOFF_PATH, {})
        session_dir = self.source_session_dir or self.session_path
        if not isinstance(handoff, dict) or session_dir is None:
            return
        try:
            handoff_session = Path(str(handoff.get("session_dir", ""))).resolve()
        except OSError:
            return
        if (
            handoff_session != session_dir.resolve()
            or str(handoff.get("username", "")).casefold() != self.username.casefold()
        ):
            return
        pending_ids = {
            str(value)
            for value in handoff.get("pending_msg_ids", [])
            if str(value) and str(value) != message.event_id
        }
        if pending_ids:
            handoff["pending_msg_ids"] = sorted(pending_ids)
            try:
                write_json(REPLAY_HANDOFF_PATH, handoff)
            except OSError as error:
                print(f"[tts] Gift replay cleanup failed: {type(error).__name__}", flush=True)
        else:
            REPLAY_HANDOFF_PATH.unlink(missing_ok=True)

    def _record_unmapped_gift(self, event: dict) -> None:
        session_dir = self.session_path or self.source_session_dir
        if session_dir is None:
            return
        gift_id = str(event.get("gift_id") or "").strip()
        raw_name = event.get("gift_name_original")
        if not isinstance(raw_name, str) or not raw_name:
            raw_name = event.get("gift_name")
        if not gift_id or not isinstance(raw_name, str) or not raw_name:
            return
        if (
            event.get("gift_catalog_status") == "mapped"
            and event.get("gift_name_zh_display")
            and event.get("gift_name_zh_tts")
        ):
            return
        key = (gift_id, raw_name)
        if self.pending_catalog_keys is None:
            self.pending_catalog_keys = set()
            try:
                with UNMAPPED_GIFTS_PATH.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and row.get("gift_id") and row.get("raw_name"):
                            self.pending_catalog_keys.add(
                                (str(row["gift_id"]), str(row["raw_name"]))
                            )
            except OSError:
                pass
        if key in self.pending_catalog_keys:
            return
        self.pending_catalog_keys.add(key)
        record = {
            "first_seen_at_local": now_local(),
            "username": self.username,
            "session_id": event.get("session_id") or (
                self.session_path.name if self.session_path else None
            ),
            "gift_id": gift_id,
            "raw_name": raw_name,
            "diamond_count": event.get("diamond_count"),
        }
        try:
            session_pending_path = session_dir / "gift_catalog_pending.ndjson"
            session_pending_path.parent.mkdir(parents=True, exist_ok=True)
            with session_pending_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
            if (
                session_dir.resolve().is_relative_to((ROOT / "data" / "browser_ws_probe").resolve())
                or session_dir.resolve().is_relative_to((ROOT / "data" / "v2_provider_benchmark").resolve())
            ):
                UNMAPPED_GIFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with UNMAPPED_GIFTS_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
                    handle.flush()
        except OSError as error:
            print(f"[gift-catalog-pending-error] {type(error).__name__}", flush=True)

    async def _enqueue(
        self,
        message: PreparedChat,
        event_type: str,
        *,
        event_arrived_at: float,
        order_at_ms: int,
    ) -> None:
        sequence = self._next_queue_sequence()
        queued_at = time.monotonic()
        if self.queue.full():
            self.metrics["queue_full_waits"] += 1
        await self.queue.put(
            (
                (int(order_at_ms), sequence),
                sequence,
                message,
                event_arrived_at,
                queued_at,
                event_type,
            )
        )
        queued_at = time.monotonic()
        self.pending_queue_times[sequence] = queued_at
        self.metrics["max_queue_depth"] = max(
            self.metrics["max_queue_depth"], self.queue.qsize()
        )
        if event_type in {"chat", "gift"}:
            event_to_queue_ms = max(0, int((queued_at - event_arrived_at) * 1000))
            self.metrics["event_to_queue_latencies_ms"].append(event_to_queue_ms)

    def _close_tail(self) -> None:
        if self.file_handle is not None:
            self.file_handle.close()
            self.file_handle = None
        self.session_path = None
        self.pending_bytes = b""

    def _save_tail_cursor(self, offset: int) -> None:
        if self.session_path is None:
            return
        write_json(
            self.session_path / "tts_cursor.json",
            {
                "session_id": self.session_path.name,
                "offset": max(0, int(offset)),
                "updated_at_local": now_local(),
            },
        )

    def _load_completed_gift_ids(self, path: Path) -> None:
        self.completed_gift_ids.clear()
        for row in self._read_delivery_rows(path):
            if row.get("stage") == "playback_completed" and row.get("event_id"):
                self.completed_gift_ids.add(str(row["event_id"]))

    @staticmethod
    def _read_delivery_rows(path: Path) -> list[dict]:
        rows = []
        try:
            with (path / "tts_delivery.ndjson").open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except OSError:
            pass
        return rows

    def _attach(self, path: Path) -> None:
        self._close_tail()
        event_path = path / "events.ndjson"
        self.file_handle = event_path.open("rb")
        self.session_path = path
        self.last_attached_session_dir = path
        cursor = read_json(path / "tts_cursor.json", {})
        event_size = event_path.stat().st_size
        cursor_offset = cursor.get("offset") if cursor.get("session_id") == path.name else None
        if isinstance(cursor_offset, int) and 0 <= cursor_offset <= event_size:
            # Resume at the last fully processed NDJSON row after worker restart.
            self.tail_position = cursor_offset
            self.resume_existing_session = True
            self._load_completed_gift_ids(path)
        else:
            # Do not announce the backlog from before this worker first attached.
            # New collector sessions created while the worker is already running
            # are read from their beginning.
            self.tail_position = event_size if self.first_session else 0
            self.resume_existing_session = False
        self.file_handle.seek(self.tail_position)
        self.pending_bytes = b""
        self.first_session = False
        try:
            self._save_tail_cursor(self.tail_position)
        except OSError as error:
            self.metrics["cursor_write_errors"] += 1
            print(f"[tts-cursor-error] {type(error).__name__}", flush=True)
        print(f"[tts] following active collector session {path.name}", flush=True)

    async def _recover_inflight_gifts(self) -> int:
        """Restore queued counted Gifts after an unclean worker exit."""
        session_dir = self.session_path or self.source_session_dir
        if session_dir is None or not self.resume_existing_session:
            return 0
        delivery_rows = self._read_delivery_rows(session_dir)
        queued_ids = {
            str(row.get("event_id"))
            for row in delivery_rows
            if row.get("stage") == "queued" and row.get("event_id")
        }
        completed = {
            str(row.get("event_id"))
            for row in delivery_rows
            if row.get("stage") == "playback_completed" and row.get("event_id")
        }
        pending_ids = queued_ids - completed - self.queued_gift_ids
        if not pending_ids:
            return 0
        event_path = session_dir / "events.ndjson"
        restored = 0
        try:
            with event_path.open("rb") as handle:
                snapshot_size = handle.seek(0, os.SEEK_END)
                handle.seek(0)
                while handle.tell() < snapshot_size:
                    line = handle.readline(snapshot_size - handle.tell())
                    if not line:
                        break
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if not isinstance(event, dict) or event.get("type") != "gift":
                        continue
                    event_id = str(
                        event.get("msg_id")
                        or event.get("message_uuid")
                        or event.get("transaction_id")
                        or ""
                    )
                    if event_id not in pending_ids or event.get("counted", True) is False:
                        continue
                    await self._process_line(line)
                    self.queued_gift_ids.add(event_id)
                    restored += 1
        except OSError:
            print("[tts] could not scan pending Gift source after restart", flush=True)
            return 0
        if restored:
            self.state["recovered_inflight_gifts"] = restored
            print(f"[tts] recovered in-flight Gifts={restored}", flush=True)
        return restored

    async def _process_line(self, line: bytes) -> None:
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.metrics["skipped"]["invalid_event_line"] += 1
            return
        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        if event_type == "gift" and event.get("counted", True) is not False:
            event_id = str(
                event.get("msg_id")
                or event.get("message_uuid")
                or event.get("transaction_id")
                or ""
            )
            if event_id and event_id in self.completed_gift_ids:
                self.metrics["skipped"]["already_delivered_replay"] += 1
                return
            if event_id and event_id in self.queued_gift_ids:
                self.metrics["skipped"]["duplicate_gift_event"] += 1
                return
        followup_audio_path = None
        gift_name_resolution_ms = None
        speech_preparation_started = None
        gift_name_original = None
        gift_name_zh_tts = None
        if event_type == "chat":
            self.metrics["chat_events_seen"] += 1
            self.metrics["last_chat_at_local"] = event.get("received_at_local")
            if not should_speak_chat(event, self.settings):
                self.metrics["chat_filtered_by_settings"] += 1
                return
            sticker_message = (
                event.get("message_kind") == "emote" or bool(event.get("emotes"))
            )
            if sticker_message:
                event["comment"] = str(event.get("comment") or "").strip()
                if "\u50b3\u9001\u8868\u60c5\u8cbc" not in event["comment"]:
                    event["comment"] = (
                        f"{event['comment']}\uff0c\u50b3\u9001\u8868\u60c5\u8cbc"
                        if event["comment"]
                        else "\u50b3\u9001\u8868\u60c5\u8cbc"
                    )
        elif event_type == "gift":
            self.metrics["gift_events_seen"] += 1
            if not event.get("counted", True):
                self.metrics["gift_streak_events_skipped"] += 1
                return
            self._record_unmapped_gift(event)
            sender = str(
                event.get("nickname")
                or (f"@{event['unique_id']}" if event.get("unique_id") else "\u6709\u89c0\u773e")
            ).strip()
            gift_id = str(event.get("gift_id") or "").strip()
            gift_name_original = event.get("gift_name_original")
            if not isinstance(gift_name_original, str):
                gift_name_original = event.get("gift_name")
            gift_name_zh_tts = event.get("gift_name_zh_tts")
            name_resolution_started = time.perf_counter()
            try:
                gift_name = spoken_gift_name(
                    gift_id,
                    gift_name_original,
                    self.settings.gift_name_by_id,
                    tts_name=gift_name_zh_tts,
                )
            except Exception as error:
                gift_name = str(gift_name_original or "未知禮物")
                event_id = str(event.get("msg_id") or event.get("transaction_id") or "") or None
                diagnostic = PreparedChat(
                    user_key=None,
                    text="",
                    voice=self.settings.zh_voice,
                    received_at_ms=None,
                    event_id=event_id,
                    gift_id=gift_id or None,
                    source_session_dir=(
                        str(self.session_path.resolve()) if self.session_path else None
                    ),
                    session_id=str(event.get("session_id") or "") or None,
                    gift_name=str(gift_name_original or "") or None,
                    gift_name_original=str(gift_name_original or "") or None,
                    gift_name_zh_tts=(str(gift_name_zh_tts) if gift_name_zh_tts else None),
                    gift_catalog_status=str(event.get("gift_catalog_status") or "needs_review"),
                )
                self._log_gift_stage(
                    diagnostic,
                    "name_resolution_failed",
                    error=error,
                    duration_ms=(time.perf_counter() - name_resolution_started) * 1000,
                )
            gift_name_resolution_ms = (time.perf_counter() - name_resolution_started) * 1000
            followup_audio_path = dict(self.settings.gift_sound_by_id).get(
                gift_id, self.settings.gift_followup_audio_path
            )
            try:
                quantity = max(1, int(event.get("repeat_count") or 1))
            except (TypeError, ValueError):
                quantity = 1
            event["comment"] = f"{sender} \u9001\u51fa{gift_name} {quantity} \u500b"
        elif event_type == "member":
            self.metrics["member_events_seen"] += 1
            configured = member_entry_sound(event, self.settings)
            if not configured:
                return
            received_at_ms = event.get("received_at_ms")
            prepared = PreparedChat(
                user_key=None,
                text="",
                voice=self.settings.zh_voice,
                received_at_ms=received_at_ms if isinstance(received_at_ms, int) else None,
                followup_audio_path=configured,
            )
            await self._enqueue(
                prepared,
                "member_sound",
                event_arrived_at=time.monotonic(),
                order_at_ms=prepared.received_at_ms or int(time.time() * 1000),
            )
            self.metrics["member_entry_sounds_queued"] += 1
            return
        else:
            return

        speech_preparation_started = time.perf_counter()
        try:
            prepared, reason = self.processor.prepare(event)
        except Exception as error:
            if event_type not in {"chat", "gift"}:
                raise
            preparation_duration_ms = (
                time.perf_counter() - speech_preparation_started
            ) * 1000
            fallback_text = " ".join(str(event.get("comment") or "").split())
            fallback_text = fallback_text[: self.settings.max_text_length].rstrip()
            diagnostic = PreparedChat(
                user_key=None,
                text=fallback_text,
                voice=self.settings.zh_voice,
                received_at_ms=(
                    int(event["received_at_ms"])
                    if isinstance(event.get("received_at_ms"), (int, float))
                    else None
                ),
                event_id=str(event.get("msg_id") or event.get("transaction_id") or "") or None,
                gift_id=str(event.get("gift_id") or "") or None,
                source_session_dir=(
                    str(self.session_path.resolve()) if self.session_path else None
                ),
                session_id=str(event.get("session_id") or "") or None,
                gift_name=str(event.get("gift_name") or "") or None,
                gift_name_original=(
                    str(gift_name_original) if gift_name_original is not None else None
                ),
                spoken_gift_name=(str(gift_name or "") if event_type == "gift" else None),
                gift_name_zh_tts=(str(gift_name_zh_tts) if gift_name_zh_tts else None),
                gift_catalog_status=str(event.get("gift_catalog_status") or "needs_review"),
                gift_name_resolution_ms=gift_name_resolution_ms,
                speech_preparation_ms=preparation_duration_ms,
            )
            if event_type == "gift":
                self._log_gift_stage(
                    diagnostic,
                    "speech_preparation_fallback",
                    error=error,
                    duration_ms=preparation_duration_ms,
                )
            else:
                self.metrics["speech_preparation_fallbacks"] += 1
                print(
                    f"[tts-prepare-error] event_type=chat; fallback=single_segment error={type(error).__name__}",
                    flush=True,
                )
            if not fallback_text:
                self.metrics["skipped"]["empty_after_preparation_error"] += 1
                return
            fallback_voice = (
                self.settings.zh_voice
                if dominant_language(fallback_text) == "zh-TW"
                else self.settings.en_voice
            )
            prepared = PreparedChat(
                user_key=None,
                text=fallback_text,
                voice=fallback_voice,
                received_at_ms=diagnostic.received_at_ms,
                segments=((fallback_text, fallback_voice),),
                event_id=diagnostic.event_id,
                gift_id=diagnostic.gift_id,
                source_session_dir=diagnostic.source_session_dir,
                session_id=diagnostic.session_id,
                gift_name=diagnostic.gift_name,
                gift_name_original=diagnostic.gift_name_original,
                spoken_gift_name=diagnostic.spoken_gift_name,
                gift_name_en=(str(event.get("gift_name_en")) if event.get("gift_name_en") else None),
                gift_name_zh_display=(
                    str(event.get("gift_name_zh_display"))
                    if event.get("gift_name_zh_display")
                    else None
                ),
                gift_name_zh_tts=diagnostic.gift_name_zh_tts,
                gift_catalog_status=diagnostic.gift_catalog_status,
                gift_name_resolution_ms=gift_name_resolution_ms,
                gift_quantity=quantity if event_type == "gift" else None,
                gift_diamond_total=(
                    int(event["diamond_total"])
                    if event_type == "gift" and isinstance(event.get("diamond_total"), (int, float))
                    else None
                ),
                speech_segmentation_fallback=True,
                speech_preparation_ms=preparation_duration_ms,
            )
            reason = None
        if prepared is None:
            self.metrics["skipped"][reason or "filtered"] += 1
            if event_type == "gift":
                diagnostic = PreparedChat(
                    user_key=None,
                    text="",
                    voice=self.settings.zh_voice,
                    received_at_ms=None,
                    event_id=str(event.get("msg_id") or event.get("transaction_id") or "") or None,
                    gift_id=str(event.get("gift_id") or "") or None,
                    source_session_dir=(
                        str(self.session_path.resolve()) if self.session_path else None
                    ),
                    session_id=str(event.get("session_id") or "") or None,
                    gift_name=str(event.get("gift_name") or "") or None,
                    gift_name_original=str(gift_name_original or "") or None,
                    spoken_gift_name=gift_name if event_type == "gift" else None,
                    gift_name_zh_tts=str(gift_name_zh_tts or "") or None,
                    gift_catalog_status=str(event.get("gift_catalog_status") or "needs_review"),
                    gift_name_resolution_ms=gift_name_resolution_ms,
                    speech_preparation_ms=(time.perf_counter() - speech_preparation_started) * 1000,
                )
                self._log_gift_stage(
                    diagnostic,
                    "speech_preparation_skipped",
                    detail=f"reason={reason or 'filtered'}",
                    duration_ms=diagnostic.speech_preparation_ms,
                )
            return
        if event_type == "gift":
            processing_timings = event.get("processing_timings_ms")
            if not isinstance(processing_timings, dict):
                processing_timings = {}
            prepared = PreparedChat(
                user_key=prepared.user_key,
                text=prepared.text,
                voice=prepared.voice,
                received_at_ms=prepared.received_at_ms,
                segments=prepared.segments,
                followup_audio_path=followup_audio_path,
                event_id=str(
                    event.get("msg_id")
                    or event.get("message_uuid")
                    or event.get("transaction_id")
                    or ""
                ) or None,
                gift_id=gift_id or None,
                source_session_dir=(
                    str(self.session_path.resolve()) if self.session_path else None
                ),
                session_id=str(event.get("session_id") or "") or None,
                gift_name=str(event.get("gift_name") or "") or None,
                gift_name_original=(str(gift_name_original) if gift_name_original is not None else None),
                spoken_gift_name=gift_name,
                gift_name_en=(str(event.get("gift_name_en")) if event.get("gift_name_en") else None),
                gift_name_zh_display=(
                    str(event.get("gift_name_zh_display"))
                    if event.get("gift_name_zh_display")
                    else None
                ),
                gift_name_zh_tts=(str(gift_name_zh_tts) if gift_name_zh_tts else None),
                gift_catalog_status=str(event.get("gift_catalog_status") or "needs_review"),
                gift_name_resolution_ms=gift_name_resolution_ms,
                speech_preparation_ms=(
                    (time.perf_counter() - speech_preparation_started) * 1000
                ),
                speech_segmentation_fallback=prepared.speech_segmentation_fallback,
                processing_timings_ms=tuple(
                    (str(key), float(value))
                    for key, value in processing_timings.items()
                    if isinstance(value, (int, float))
                ),
                gift_quantity=quantity,
                gift_diamond_total=(
                    int(event["diamond_total"])
                    if isinstance(event.get("diamond_total"), (int, float))
                    else None
                ),
            )
        elif event_type == "chat":
            session_dir = self.session_path or self.source_session_dir
            prepared = replace(
                prepared,
                event_id=str(
                    event.get("msg_id")
                    or event.get("message_uuid")
                    or event.get("transaction_id")
                    or ""
                ) or None,
                source_session_dir=(
                    str(session_dir.resolve()) if session_dir is not None else None
                ),
                session_id=str(event.get("session_id") or "") or (
                    session_dir.name if session_dir is not None else None
                ),
            )
        # Backpressure the file reader instead of dropping accepted events.
        # The append-only event file remains the durable source of truth.
        order_at_ms = prepared.received_at_ms or int(time.time() * 1000)
        now_monotonic = time.monotonic()
        age_at_enqueue = max(0.0, (time.time() * 1000 - order_at_ms) / 1000)
        await self._enqueue(
            prepared,
            event_type,
            event_arrived_at=now_monotonic - age_at_enqueue,
            order_at_ms=order_at_ms,
        )
        self.metrics[f"{event_type}_queued"] += 1
        if event_type == "gift":
            if prepared.event_id:
                self.queued_gift_ids.add(prepared.event_id)
            self._log_gift_stage(
                prepared,
                "speech_prepared",
                detail=(
                    f"segments={len(prepared.segments)}"
                    + (" segmentation_fallback=dominant_voice" if prepared.speech_segmentation_fallback else "")
                ),
                duration_ms=prepared.speech_preparation_ms,
            )
            self._log_gift_stage(prepared, "queued")
        elif event_type == "chat":
            self._log_chat_stage(
                prepared,
                "speech_prepared",
                detail=f"segments={len(prepared.segments)}",
                duration_ms=prepared.speech_preparation_ms,
            )
            self._log_chat_stage(prepared, "queued")

    async def _read_new_events(self) -> None:
        if self.file_handle is None:
            return
        try:
            current_size = self.file_handle.seek(0, os.SEEK_END)
            position = self.tail_position
            if current_size < position:
                position = 0
                self.tail_position = 0
                self.completed_gift_ids.clear()
                if self.session_path is not None:
                    self._save_tail_cursor(0)
            self.file_handle.seek(position)
            chunk = self.file_handle.read(64 * 1024)
        except OSError:
            self._close_tail()
            self.tail_position = 0
            return

        if not chunk:
            return
        rows = chunk.split(b"\n")
        rows.pop()  # The trailing fragment remains after tail_position for next poll.
        processed_position = position
        for row in rows:
            if row.strip():
                await self._process_line(row)
            processed_position += len(row) + 1
            self.tail_position = processed_position
            if self.session_path is not None:
                try:
                    self._save_tail_cursor(processed_position)
                except OSError as error:
                    self.metrics["cursor_write_errors"] += 1
                    print(
                        f"[tts-cursor-error] {type(error).__name__}", flush=True
                    )

    async def _replay_handoff_gifts(self) -> int:
        """Restore counted Gifts left queued when the previous worker stopped."""
        handoff = read_json(REPLAY_HANDOFF_PATH, {})
        expected_session = self.source_session_dir or self.session_path
        if not isinstance(handoff, dict) or expected_session is None:
            return 0
        try:
            session_dir = Path(str(handoff.get("session_dir", ""))).resolve()
            pending_ids = {
                str(value) for value in handoff.get("pending_msg_ids", [])
                if str(value)
            }
            if pending_ids:
                since_ms = None
                terminal_gifts = 0
            else:
                since_ms = int(handoff.get("replay_since_ms"))
                terminal_gifts = max(0, int(handoff.get("terminal_gift_count", 0)))
        except (OSError, TypeError, ValueError):
            print("[tts] invalid Gift replay handoff; leaving it for review", flush=True)
            return 0
        if (
            session_dir != expected_session
            or str(handoff.get("username", "")).casefold() != self.username.casefold()
        ):
            return 0

        event_path = session_dir / "events.ndjson"
        gifts: list[tuple[int, int, bytes, str]] = []
        try:
            with event_path.open("rb") as handle:
                snapshot_size = handle.seek(0, os.SEEK_END)
                handle.seek(0)
                index = 0
                while handle.tell() < snapshot_size:
                    line = handle.readline(snapshot_size - handle.tell())
                    if not line:
                        break
                    index += 1
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if not isinstance(event, dict) or event.get("type") != "gift":
                        continue
                    if event.get("counted", True) is False:
                        continue
                    try:
                        received_at_ms = int(
                            event.get("received_at_ms") or event.get("timestamp_ms")
                        )
                    except (TypeError, ValueError):
                        continue
                    if since_ms is not None and received_at_ms < since_ms:
                        continue
                    event_id = str(
                        event.get("msg_id")
                        or event.get("message_uuid")
                        or event.get("transaction_id")
                        or ""
                    )
                    gifts.append((received_at_ms, index, line, event_id))
        except OSError:
            print("[tts] could not read Gift replay source; handoff retained", flush=True)
            return 0

        gifts.sort(key=lambda item: (item[0], item[1]))
        if pending_ids:
            found_ids = {event_id for _, _, _, event_id in gifts if event_id}
            missing_ids = pending_ids - found_ids
            if missing_ids:
                print(
                    f"[tts] Gift replay handoff references {len(missing_ids)} missing event IDs; handoff retained",
                    flush=True,
                )
                return 0
            completed_ids = {
                str(row.get("event_id"))
                for row in self._read_delivery_rows(session_dir)
                if row.get("stage") == "playback_completed" and row.get("event_id")
            }
            pending = [
                item for item in gifts
                if item[3] in pending_ids and item[3] not in completed_ids
            ]
        else:
            pending = gifts[terminal_gifts:]
        if len(pending) > 5000:
            print("[tts] Gift replay exceeds the 5000 event safety bound; handoff retained", flush=True)
            return 0
        if not pending and pending_ids:
            REPLAY_HANDOFF_PATH.unlink(missing_ok=True)
            self.pending_gift_replay_ids.difference_update(pending_ids)
            return 0
        for _, _, line, _ in pending:
            await self._process_line(line)
        self.state["replayed_pending_gifts"] = len(pending)
        REPLAY_HANDOFF_PATH.unlink(missing_ok=True)
        print(f"[tts] restored pending Gifts={len(pending)}", flush=True)
        return len(pending)

    def _next_queue_sequence(self) -> int:
        self.queue_sequence += 1
        return self.queue_sequence

    def _reload_runtime_settings(self) -> None:
        updated = TTSSettings.from_mapping(read_json(CONFIG_PATH, {}))
        self.settings = updated
        self.processor.settings = updated

    def _event_is_stale(
        self, event_type: str, event_arrived_at: float
    ) -> bool:
        return (
            event_type == "chat"
            and time.monotonic() - event_arrived_at > self.settings.chat_ttl_seconds
        )

    def _play_sound_effect(self, configured: str | None) -> bool:
        if not configured:
            return False
        audio_path = Path(configured).expanduser()
        if not audio_path.is_absolute():
            audio_path = ROOT / audio_path
        if not audio_path.is_file():
            return False
        try:
            return bool(self.player.play_effect(audio_path, self.settings.volume))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def _play_gift_failure_fallback(self, message: PreparedChat) -> None:
        """Play a mapped Gift sound or a local alert, while keeping the Gift pending."""
        configured = (
            message.followup_audio_path
            or self.settings.gift_followup_audio_path
        )
        if configured and self._play_sound_effect(configured):
            self.metrics["gift_failure_alerts_played"] += 1
            self._log_gift_stage(message, "failure_alert_played", "source=configured_sound")
            return
        try:
            if self.player.play_failure_alert(self.settings.volume):
                self.metrics["gift_failure_alerts_played"] += 1
                self._log_gift_stage(message, "failure_alert_played", "source=local_tone")
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.metrics["last_error"] = type(error).__name__
        self.metrics["gift_failure_alert_errors"] += 1
        self._log_gift_stage(message, "failure_alert_failed")

    async def _play_gift_followup_sound(
        self, message: PreparedChat, configured: str | None
    ) -> None:
        if not configured:
            return
        if self._play_sound_effect(configured):
            self.metrics["gift_followup_sounds_played"] += 1
            self._log_gift_stage(message, "followup_sound_played")
        else:
            # An optional sound cue must not turn a successfully spoken Gift
            # into a delivery failure or block the next TTS item.
            self.metrics["gift_followup_sound_errors"] += 1
            self._log_gift_stage(message, "followup_sound_failed")

    async def _play_member_entry_sound(self, configured: str | None) -> None:
        if not configured:
            return
        if self._play_sound_effect(configured):
            self.metrics["member_entry_sounds_played"] += 1
        else:
            self.metrics["member_entry_sound_errors"] += 1

    async def _enqueue_voice_preview(self) -> None:
        if not PREVIEW_PATH.exists():
            return
        processing_path = PREVIEW_PATH.with_name("preview_request.processing.json")
        try:
            PREVIEW_PATH.replace(processing_path)
        except OSError:
            return
        try:
            request = read_json(processing_path, {})
        finally:
            processing_path.unlink(missing_ok=True)
        if not isinstance(request, dict):
            return
        preview_id = str(request.get("id") or "")
        if not preview_id or preview_id == self.last_preview_id:
            return
        text = str(request.get("text") or "").strip()
        voice = str(request.get("voice") or "").strip()
        if not text or not voice:
            return
        message = PreparedChat(
            user_key=None,
            text=text,
            voice=voice,
            received_at_ms=int(time.time() * 1000),
        )
        now = time.monotonic()
        now_ms = int(time.time() * 1000)
        await self._enqueue(
            message,
            "preview",
            event_arrived_at=now,
            order_at_ms=now_ms,
        )
        self.last_preview_id = preview_id
        self.metrics["preview_queued"] += 1

    async def _prepare_next_announcement(self) -> PreparedAnnouncement | None:
        entry = await self.queue.get()
        _, sequence, message, event_arrived_at, enqueued_at, event_type = entry
        self.pending_queue_times.pop(sequence, None)
        dequeued_at = time.monotonic()
        queue_wait_ms = max(0, int((dequeued_at - enqueued_at) * 1000))
        self.metrics["queue_wait_latencies_ms"].append(
            queue_wait_ms
        )
        self.metrics["max_queue_wait_ms"] = max(
            self.metrics["max_queue_wait_ms"],
            queue_wait_ms,
        )
        return await self._prepare_announcement(
            sequence, message, event_arrived_at, event_type
        )

    async def _prepare_announcement(
        self,
        sequence: int,
        message: PreparedChat,
        event_arrived_at: float,
        event_type: str,
    ) -> PreparedAnnouncement | None:
        queue_finished = False
        audio_paths: list[Path] = []
        handed_off = False

        def finish_queue_item() -> None:
            nonlocal queue_finished
            if not queue_finished:
                self.pending_queue_times.pop(sequence, None)
                self.queue.task_done()
                queue_finished = True

        def expire_chat() -> None:
            self.metrics["skipped"]["expired_chat"] += 1
            if event_type == "chat":
                self._log_chat_stage(message, "expired_chat", detail="chat_ttl")
            finish_queue_item()

        async def offline_gift_audio() -> Path | None:
            self.metrics["gift_offline_fallback_attempts"] += 1
            started = time.monotonic()
            self._log_gift_stage(message, "offline_fallback_started")
            try:
                path = await self.offline_backend.synthesize(
                    message.text,
                    dominant_language(message.text),
                    self.settings.rate_percent,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.metrics["gift_offline_fallback_failures"] += 1
                self._log_gift_stage(
                    message,
                    "offline_fallback_failed",
                    f"backend=windows_system_speech reason={str(error).replace(chr(10), ' ')[:80]}",
                    error=error,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                return None
            self.metrics["gift_offline_fallback_succeeded"] += 1
            self._log_gift_stage(
                message,
                "offline_fallback_synthesized",
                "backend=windows_system_speech",
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return path

        async def use_offline_gift_audio() -> PreparedAnnouncement | None:
            nonlocal handed_off
            fallback_path = await offline_gift_audio()
            if fallback_path is None:
                return None
            audio_paths.append(fallback_path)
            self.metrics["synthesis_latencies_ms"].append(
                max(0, int((time.monotonic() - synthesis_started) * 1000))
            )
            self.backend_retry_at = 0.0
            self.metrics["tts_retry_after_local"] = None
            self._log_gift_stage(
                message,
                "synthesis_completed",
                "backend=windows_system_speech fallback=edge_tts_failed",
                duration_ms=(time.monotonic() - synthesis_started) * 1000,
            )
            handed_off = True
            return PreparedAnnouncement(
                sequence,
                message,
                event_arrived_at,
                event_type,
                audio_paths,
            )

        async def offline_chat_audio() -> list[Path] | None:
            self.metrics["chat_offline_fallback_attempts"] += 1
            started = time.monotonic()
            self._log_chat_stage(message, "offline_fallback_started")
            paths: list[Path] = []
            segments = message.segments or ((message.text, message.voice),)
            try:
                for text, voice in segments:
                    paths.append(
                        await self.offline_backend.synthesize(
                            text, voice, self.settings.rate_percent
                        )
                    )
            except asyncio.CancelledError:
                for path in paths:
                    path.unlink(missing_ok=True)
                raise
            except Exception as error:
                for path in paths:
                    path.unlink(missing_ok=True)
                self.metrics["chat_offline_fallback_failures"] += 1
                self._log_chat_stage(
                    message,
                    "offline_fallback_failed",
                    f"backend=windows_system_speech reason={str(error).replace(chr(10), ' ')[:80]}",
                    error=error,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                return None
            self.metrics["chat_offline_fallback_succeeded"] += 1
            self._log_chat_stage(
                message,
                "offline_fallback_synthesized",
                "backend=windows_system_speech",
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return paths

        if self._event_is_stale(event_type, event_arrived_at):
            expire_chat()
            return None
        if event_type == "member_sound":
            handed_off = True
            return PreparedAnnouncement(
                sequence, message, event_arrived_at, event_type, audio_paths
            )

        self.metrics["is_synthesizing"] = True
        synthesis_started = time.monotonic()
        expired = False
        last_synthesis_error: Exception | None = None
        try:
            for attempt in range(3):
                attempt_started = time.monotonic()
                if self._event_is_stale(event_type, event_arrived_at):
                    expired = True
                    break
                cooldown = self.backend_retry_at - time.monotonic()
                if cooldown > 0:
                    await asyncio.sleep(cooldown)
                if self._event_is_stale(event_type, event_arrived_at):
                    expired = True
                    break

                try:
                    segments = message.segments or ((message.text, message.voice),)
                    for segment_index, (text, voice) in enumerate(segments, start=1):
                        if self._event_is_stale(event_type, event_arrived_at):
                            expired = True
                            break
                        remaining = (
                            self.last_synthesis_request_at
                            + self.settings.min_request_interval_seconds
                            - time.monotonic()
                        )
                        if remaining > 0:
                            await asyncio.sleep(remaining)
                        if self._event_is_stale(event_type, event_arrived_at):
                            expired = True
                            break
                        self.last_synthesis_request_at = time.monotonic()
                        segment_started = time.monotonic()
                        try:
                            audio_path = await self.backend.synthesize(
                                text, voice, self.settings.rate_percent
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            if event_type == "gift":
                                self._log_gift_stage(
                                    message,
                                    "synthesis_segment_failed",
                                    detail=(
                                        f"segment={segment_index}/{len(segments)}"
                                        f" voice={voice} characters={len(text)}"
                                    ),
                                    error=error,
                                    duration_ms=(time.monotonic() - segment_started) * 1000,
                                )
                            elif event_type == "chat":
                                self._log_chat_stage(
                                    message,
                                    "synthesis_segment_failed",
                                    detail=(
                                        f"segment={segment_index}/{len(segments)}"
                                        f" voice={voice} characters={len(text)}"
                                    ),
                                    error=error,
                                    duration_ms=(time.monotonic() - segment_started) * 1000,
                                )
                            raise
                        audio_paths.append(audio_path)
                        if event_type == "gift":
                            self._log_gift_stage(
                                message,
                                "synthesis_segment_completed",
                                detail=(
                                    f"segment={segment_index}/{len(segments)}"
                                    f" voice={voice} characters={len(text)}"
                                ),
                                duration_ms=(time.monotonic() - segment_started) * 1000,
                            )
                        elif event_type == "chat":
                            self._log_chat_stage(
                                message,
                                "synthesis_segment_completed",
                                detail=(
                                    f"segment={segment_index}/{len(segments)}"
                                    f" voice={voice} characters={len(text)}"
                                ),
                                duration_ms=(time.monotonic() - segment_started) * 1000,
                            )
                    if expired:
                        break
                    if self._event_is_stale(event_type, event_arrived_at):
                        expired = True
                        break
                    self.metrics["synthesis_latencies_ms"].append(
                        max(0, int((time.monotonic() - synthesis_started) * 1000))
                    )
                    if event_type == "gift":
                        self._log_gift_stage(
                            message,
                            "synthesis_completed",
                            f"backend=edge_tts segments={len(segments)}",
                            duration_ms=(time.monotonic() - attempt_started) * 1000,
                        )
                    elif event_type == "chat":
                        self._log_chat_stage(
                            message,
                            "synthesis_completed",
                            f"backend=edge_tts segments={len(segments)}",
                            duration_ms=(time.monotonic() - attempt_started) * 1000,
                        )
                    handed_off = True
                    return PreparedAnnouncement(
                        sequence,
                        message,
                        event_arrived_at,
                        event_type,
                        audio_paths,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    last_synthesis_error = error
                    self.metrics["synthesis_or_playback_errors"] += 1
                    self.metrics["last_error"] = type(error).__name__
                    self.backend_failures += 1
                    backoff = TTS_RETRY_BACKOFF_SECONDS[
                        min(attempt, len(TTS_RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    self.backend_retry_at = time.monotonic() + backoff
                    self.metrics["tts_retry_after_local"] = (
                        datetime.now(TAIPEI_TZ) + timedelta(seconds=backoff)
                    ).isoformat(timespec="seconds")
                    print(
                        f"[tts-error] {type(error).__name__}; retry {attempt + 1}/3",
                        flush=True,
                    )
                    if event_type == "gift":
                        self._log_gift_stage(
                            message,
                            "synthesis_attempt_failed",
                            attempt=attempt + 1,
                            error=error,
                            duration_ms=(time.monotonic() - attempt_started) * 1000,
                        )
                    elif event_type == "chat":
                        self._log_chat_stage(
                            message,
                            "synthesis_attempt_failed",
                            attempt=attempt + 1,
                            error=error,
                            duration_ms=(time.monotonic() - attempt_started) * 1000,
                        )
                    for audio_path in audio_paths:
                        audio_path.unlink(missing_ok=True)
                    audio_paths.clear()

                    if event_type == "gift" and attempt == 0:
                        fallback_announcement = await use_offline_gift_audio()
                        if fallback_announcement is not None:
                            return fallback_announcement

                    if event_type == "chat" and attempt == 0:
                        fallback_paths = await offline_chat_audio()
                        if fallback_paths is not None:
                            audio_paths.extend(fallback_paths)
                            self.metrics["synthesis_latencies_ms"].append(
                                max(0, int((time.monotonic() - synthesis_started) * 1000))
                            )
                            self.backend_retry_at = 0.0
                            self.metrics["tts_retry_after_local"] = None
                            self._log_chat_stage(
                                message,
                                "synthesis_completed",
                                "backend=windows_system_speech fallback=edge_tts_failed",
                                duration_ms=(time.monotonic() - synthesis_started) * 1000,
                            )
                            handed_off = True
                            return PreparedAnnouncement(
                                sequence,
                                message,
                                event_arrived_at,
                                event_type,
                                audio_paths,
                            )
                    if self._event_is_stale(event_type, event_arrived_at):
                        expired = True
                        break

            if expired or self._event_is_stale(event_type, event_arrived_at):
                expire_chat()
                return None

            if event_type == "gift":
                if self._enqueue_gift_retry(message, event_arrived_at):
                    self._log_gift_stage(
                        message,
                        "synthesis_retry_queued",
                        f"attempt={message.retry_attempts + 1} delay={GIFT_DELIVERY_RETRY_DELAY_SECONDS}s error={_tts_error_label(last_synthesis_error) if last_synthesis_error else 'unknown'}",
                    )
                    finish_queue_item()
                    return None
            self.metrics["delivery_failures"] += 1
            self.metrics["skipped"]["backend_error"] += 1
            if event_type == "gift":
                self.metrics["gift_delivery_failures"] += 1
                self.metrics["gift_synthesis_failures"] += 1
                self._log_gift_stage(
                    message,
                    "synthesis_failed",
                    error=last_synthesis_error or "unknown",
                )
                self._remember_pending_gift_replay(message)
                self._play_gift_failure_fallback(message)
            elif event_type == "chat":
                self._log_chat_stage(
                    message,
                    "synthesis_failed",
                    error=last_synthesis_error or "unknown",
                )
            self.backend_retry_at = 0.0
            self.metrics["tts_retry_after_local"] = None
            finish_queue_item()
            return None
        except asyncio.CancelledError:
            self.metrics["skipped"]["worker_stopped"] += 1
            if event_type in {"chat", "gift"}:
                self.metrics["delivery_failures"] += 1
                if event_type == "gift":
                    self.metrics["gift_delivery_failures"] += 1
                    self._log_gift_stage(message, "worker_stopped")
                    self._remember_pending_gift_replay(message)
            finish_queue_item()
            raise
        finally:
            self.metrics["is_synthesizing"] = False
            if not handed_off:
                for audio_path in audio_paths:
                    audio_path.unlink(missing_ok=True)

    async def _deliver_prepared_announcement(
        self, announcement: PreparedAnnouncement
    ) -> None:
        sequence = announcement.sequence
        message = announcement.message
        event_type = announcement.event_type
        audio_paths = announcement.audio_paths
        speech_completed = False
        expired = False
        last_playback_error: Exception | None = None
        try:
            if event_type == "member_sound":
                await self._play_member_entry_sound(message.followup_audio_path)
                return
            if self._event_is_stale(event_type, announcement.event_arrived_at):
                self.metrics["skipped"]["expired_chat"] += 1
                if event_type == "chat":
                    self._log_chat_stage(message, "expired_before_playback", detail="chat_ttl")
                expired = True
                return

            if event_type in {"chat", "gift"} and message.received_at_ms is not None:
                event_to_playback_ms = max(
                    0, int(time.time() * 1000) - message.received_at_ms
                )
                self.metrics["latencies_ms"].append(event_to_playback_ms)
                self.metrics["max_event_to_playback_ms"] = max(
                    self.metrics["max_event_to_playback_ms"], event_to_playback_ms
                )
                print(
                    f"[tts] playback starting {event_type} after {event_to_playback_ms}ms from event arrival",
                    flush=True,
                )

            for attempt in range(3):
                try:
                    self.metrics["is_speaking"] = True
                    playback_started = time.monotonic()
                    for audio_path in audio_paths:
                        await self.player.play(audio_path, self.settings.volume)
                    self.metrics["audio_playback_latencies_ms"].append(
                        max(0, int((time.monotonic() - playback_started) * 1000))
                    )
                    speech_completed = True
                    if event_type == "gift":
                        self._log_gift_stage(
                            message,
                            "playback_completed",
                            duration_ms=(time.monotonic() - playback_started) * 1000,
                        )
                    elif event_type == "chat":
                        self._log_chat_stage(
                            message,
                            "playback_completed",
                            duration_ms=(time.monotonic() - playback_started) * 1000,
                        )
                    if event_type == "gift":
                        await self._play_gift_followup_sound(
                            message,
                            message.followup_audio_path
                            or self.settings.gift_followup_audio_path
                        )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    last_playback_error = error
                    self.metrics["synthesis_or_playback_errors"] += 1
                    self.metrics["last_error"] = type(error).__name__
                    self.backend_failures += 1
                    backoff = TTS_RETRY_BACKOFF_SECONDS[
                        min(attempt, len(TTS_RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    self.backend_retry_at = time.monotonic() + backoff
                    self.metrics["tts_retry_after_local"] = (
                        datetime.now(TAIPEI_TZ) + timedelta(seconds=backoff)
                    ).isoformat(timespec="seconds")
                    print(
                        f"[tts-error] {type(error).__name__}; playback retry {attempt + 1}/3",
                        flush=True,
                    )
                    if event_type == "gift":
                        self._log_gift_stage(
                            message,
                            "playback_attempt_failed",
                            attempt=attempt + 1,
                            error=error,
                            duration_ms=(time.monotonic() - playback_started) * 1000,
                        )
                    elif event_type == "chat":
                        self._log_chat_stage(
                            message,
                            "playback_attempt_failed",
                            attempt=attempt + 1,
                            error=error,
                            duration_ms=(time.monotonic() - playback_started) * 1000,
                        )
                    if self._event_is_stale(event_type, announcement.event_arrived_at):
                        self.metrics["skipped"]["expired_chat"] += 1
                        expired = True
                        break
                    if attempt < 2:
                        await asyncio.sleep(backoff)

            if (
                not speech_completed
                and not expired
                and event_type == "gift"
                and not any(path.suffix.casefold() == ".wav" for path in audio_paths)
            ):
                self.metrics["gift_offline_fallback_attempts"] += 1
                fallback_started = time.monotonic()
                self._log_gift_stage(message, "offline_fallback_started")
                fallback_path = None
                try:
                    fallback_path = await self.offline_backend.synthesize(
                        message.text,
                        dominant_language(message.text),
                        self.settings.rate_percent,
                    )
                    self.metrics["gift_offline_fallback_succeeded"] += 1
                    audio_paths.append(fallback_path)
                    self._log_gift_stage(
                        message,
                        "offline_fallback_synthesized",
                        "backend=windows_system_speech after_playback_failure",
                        duration_ms=(time.monotonic() - fallback_started) * 1000,
                    )
                    self.metrics["is_speaking"] = True
                    fallback_play_started = time.monotonic()
                    await self.player.play(fallback_path, self.settings.volume)
                    self.metrics["audio_playback_latencies_ms"].append(
                        max(0, int((time.monotonic() - fallback_play_started) * 1000))
                    )
                    speech_completed = True
                    self._log_gift_stage(
                        message,
                        "offline_fallback_playback_completed",
                        duration_ms=(time.monotonic() - fallback_play_started) * 1000,
                    )
                    self._log_gift_stage(
                        message,
                        "playback_completed",
                        "backend=windows_system_speech fallback=true",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.metrics["gift_offline_fallback_failures"] += 1
                    self._log_gift_stage(
                        message,
                        "offline_fallback_failed",
                        error=error,
                        duration_ms=(time.monotonic() - fallback_started) * 1000,
                    )

            if speech_completed:
                self.backend_failures = 0
                self.backend_retry_at = 0.0
                self.metrics["spoken"] += 1
                if event_type in {"chat", "gift"}:
                    self.metrics[f"{event_type}_spoken"] += 1
                if event_type == "gift":
                    self._mark_gift_delivered(message)
                elif event_type == "preview":
                    self.metrics["preview_spoken"] += 1
                self.metrics["last_spoken_at_local"] = now_local()
                self.metrics["last_error"] = None
                self.metrics["tts_retry_after_local"] = None
            elif not expired:
                retry_queued = (
                    event_type == "gift"
                    and self._enqueue_gift_retry(message, announcement.event_arrived_at)
                )
                if retry_queued:
                    self._log_gift_stage(
                        message,
                        "playback_retry_queued",
                        f"attempt={message.retry_attempts + 1} delay={GIFT_DELIVERY_RETRY_DELAY_SECONDS}s error={_tts_error_label(last_playback_error) if last_playback_error else 'unknown'}",
                    )
                else:
                    self.metrics["delivery_failures"] += 1
                    self.metrics["skipped"]["backend_error"] += 1
                    if event_type == "gift":
                        self.metrics["gift_delivery_failures"] += 1
                        self.metrics["gift_playback_failures"] += 1
                        self._log_gift_stage(
                            message,
                            "playback_failed",
                            error=last_playback_error or "unknown",
                        )
                        self._remember_pending_gift_replay(message)
                        self._play_gift_failure_fallback(message)
                    elif event_type == "chat":
                        self._log_chat_stage(
                            message,
                            "playback_failed",
                            error=last_playback_error or "unknown",
                        )
                    self.backend_retry_at = 0.0
                    self.metrics["tts_retry_after_local"] = None
        except asyncio.CancelledError:
            self.metrics["skipped"]["worker_stopped"] += 1
            if speech_completed and event_type in {"chat", "gift"}:
                self.metrics["spoken"] += 1
                self.metrics[f"{event_type}_spoken"] += 1
                self.metrics["last_spoken_at_local"] = now_local()
            elif event_type in {"chat", "gift"}:
                self.metrics["delivery_failures"] += 1
                if event_type == "gift":
                    self.metrics["gift_delivery_failures"] += 1
                    self.metrics["gift_playback_failures"] += 1
                    self._log_gift_stage(message, "worker_stopped")
                    self._remember_pending_gift_replay(message)
                else:
                    self._log_chat_stage(message, "worker_stopped")
            raise
        finally:
            for audio_path in audio_paths:
                audio_path.unlink(missing_ok=True)
            self.metrics["is_speaking"] = False
            self.pending_queue_times.pop(sequence, None)
            self.queue.task_done()

    async def _discard_prepared_announcement(
        self, announcement: PreparedAnnouncement
    ) -> None:
        self.metrics["skipped"]["worker_stopped"] += 1
        if announcement.event_type in {"chat", "gift"}:
            self.metrics["delivery_failures"] += 1
            if announcement.event_type == "gift":
                self.metrics["gift_delivery_failures"] += 1
                self.metrics["gift_playback_failures"] += 1
                self._log_gift_stage(announcement.message, "worker_stopped")
                self._remember_pending_gift_replay(announcement.message)
        for audio_path in announcement.audio_paths:
            audio_path.unlink(missing_ok=True)
        self.pending_queue_times.pop(announcement.sequence, None)
        self.queue.task_done()

    async def _speak(self) -> None:
        preparing = asyncio.create_task(self._prepare_next_announcement())
        try:
            while True:
                announcement = await preparing
                if announcement is None:
                    preparing = asyncio.create_task(
                        self._prepare_next_announcement()
                    )
                    continue
                # Keep one event ahead: synthesize it while this audio is playing,
                # then play it next to preserve the single chronological speaker.
                preparing = asyncio.create_task(self._prepare_next_announcement())
                await self._deliver_prepared_announcement(announcement)
        finally:
            if not preparing.done():
                preparing.cancel()
            result = await asyncio.gather(preparing, return_exceptions=True)
            if result and isinstance(result[0], PreparedAnnouncement):
                await self._discard_prepared_announcement(result[0])

    async def run(self) -> int:
        lock = _acquire_lock(LOCK_PATH)
        if lock is None:
            print("[tts] another TTS worker already holds the lock", flush=True)
            return 2

        self.player.initialize()
        if self.source_session_dir is not None:
            existing_session = self.source_session_dir
        else:
            existing_session, _ = find_active_session(
                RAW_ROOT, WATCHER_STATE, self.username
            )
        # A worker launched before a collector exists should read its first
        # session from the beginning. When launched mid-session it seeks EOF.
        self.first_session = existing_session is not None
        self.tail_position = 0
        playback_task = asyncio.create_task(self._speak())
        last_state_write = 0.0
        preferred = None
        try:
            print(f"[tts] started for @{self.username}; no additional LIVE connection", flush=True)
            await self._replay_handoff_gifts()
            while not STOP_PATH.exists():
                self._reload_runtime_settings()
                await self._enqueue_voice_preview()
                if self.source_session_dir is not None:
                    active = (
                        self.source_session_dir
                        if (self.source_session_dir / "events.ndjson").is_file()
                        else None
                    )
                    status = "collecting" if active is not None else "waiting_for_session_file"
                else:
                    active, status = find_active_session(
                        RAW_ROOT, WATCHER_STATE, self.username, preferred
                    )
                state_values = {}
                if active is None:
                    if self.file_handle is not None:
                        self._close_tail()
                        self.tail_position = 0
                        preferred = None
                    current_status = status
                    state_values["active_session"] = None
                else:
                    if active != self.session_path:
                        self._attach(active)
                        await self._replay_handoff_gifts()
                        await self._recover_inflight_gifts()
                    preferred = active
                    await self._read_new_events()
                    current_status = (
                        "speaking"
                        if self.metrics["is_speaking"]
                        else "synthesizing"
                        if self.metrics["is_synthesizing"]
                        else "watching"
                    )
                    state_values["active_session"] = active.name

                if time.monotonic() - last_state_write >= 1.0:
                    self.update_state(current_status, **state_values)
                    last_state_write = time.monotonic()
                await asyncio.sleep(0.5)
            return 0
        finally:
            playback_task.cancel()
            try:
                await playback_task
            except asyncio.CancelledError:
                pass
            self._record_worker_stopped_queue()
            self._write_gift_replay_handoff()
            self.player.stop()
            self.player.close()
            self._close_tail()
            STOP_PATH.unlink(missing_ok=True)
            self.state["pid"] = None
            self.update_state("stopped", stopped_at_local=now_local())
            self._write_session_tts_snapshot(force=True)
            if self.browser_session_dir is not None:
                try:
                    report_path = write_live_summary(self.browser_session_dir)
                    print(f"[REPORT] {report_path}", flush=True)
                except Exception as error:
                    print(f"[REPORT ERROR] {type(error).__name__}", flush=True)
            lock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Tail an explicit benchmark session under data/v2_provider_benchmark.",
    )
    parser.add_argument(
        "--browser-session-dir",
        type=Path,
        help="Explicitly tail a Browser Network Provider session under data/browser_ws_probe.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    username = args.username.strip().lstrip("@")
    if not username or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_." for char in username):
        print("[tts] invalid streamer username", file=sys.stderr, flush=True)
        return 2
    worker = TTSWorker(
        username,
        session_dir=args.session_dir,
        browser_session_dir=args.browser_session_dir,
    )
    try:
        return asyncio.run(worker.run())
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        worker.state["pid"] = None
        worker.state["last_error"] = type(error).__name__
        worker.update_state("error")
        print(f"[tts-fatal] {type(error).__name__}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
