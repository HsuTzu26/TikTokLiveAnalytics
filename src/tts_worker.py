"""Optional TTS action worker that tails collector NDJSON without a second LIVE connection."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.tts.audio import EdgeTTSBackend, PygameAudioPlayer
from src.tts.pipeline import ChatProcessor, PreparedChat, TTSSettings


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "tts"
RAW_ROOT = ROOT / "data" / "raw"
BENCHMARK_ROOT = ROOT / "data" / "v2_provider_benchmark"
WATCHER_STATE = ROOT / "data" / "watcher" / "watcher_state.json"
CONFIG_PATH = DATA_ROOT / "config.json"
PREVIEW_PATH = DATA_ROOT / "preview_request.json"
STATE_PATH = DATA_ROOT / "state.json"
STOP_PATH = DATA_ROOT / "stop.json"
LOCK_PATH = DATA_ROOT / "worker.lock"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


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
    def __init__(self, username: str, session_dir: Path | None = None):
        self.username = username
        self.benchmark_session_dir = None
        if session_dir is not None:
            resolved = session_dir.resolve()
            if not resolved.is_relative_to(BENCHMARK_ROOT.resolve()):
                raise ValueError("benchmark session must be under data/v2_provider_benchmark")
            if not resolved.name.casefold().endswith(f"_{username.casefold()}"):
                raise ValueError("benchmark session username does not match the worker username")
            if not (resolved / "events.ndjson").is_file():
                raise FileNotFoundError("benchmark session has no events.ndjson")
            self.benchmark_session_dir = resolved
        raw = read_json(CONFIG_PATH, {})
        self.settings = TTSSettings.from_mapping(raw)
        self.processor = ChatProcessor(self.settings)
        self.queue: asyncio.PriorityQueue[
            tuple[int, int, PreparedChat, float, str]
        ] = asyncio.PriorityQueue(
            maxsize=self.settings.queue_size
        )
        self.queue_sequence = 0
        self.last_preview_id = None
        self.backend = EdgeTTSBackend()
        self.player = PygameAudioPlayer()
        self.session_path: Path | None = None
        self.file_handle = None
        self.pending_bytes = b""
        self.first_session = True
        self.metrics = {
            "chat_events_seen": 0,
            "gift_events_seen": 0,
            "gift_streak_events_skipped": 0,
            "chat_queued": 0,
            "gift_queued": 0,
            "spoken": 0,
            "chat_spoken": 0,
            "gift_spoken": 0,
            "preview_queued": 0,
            "preview_spoken": 0,
            "synthesis_or_playback_errors": 0,
            "skipped": Counter(),
            "latencies_ms": [],
            "is_speaking": False,
            "last_chat_at_local": None,
            "last_spoken_at_local": None,
            "last_error": None,
            "tts_retry_after_local": None,
        }
        self.last_synthesis_request_at = 0.0
        self.backend_failures = 0
        self.backend_retry_at = 0.0
        self.state = {
            "pid": os.getpid(),
            "username": username,
            "source_session_dir": str(self.benchmark_session_dir) if self.benchmark_session_dir else None,
            "status": "starting",
            "started_at_local": now_local(),
            "updated_at_local": now_local(),
            "active_session": None,
            "metrics": {},
        }

    def update_state(self, status: str | None = None, **values) -> None:
        if status is not None:
            self.state["status"] = status
        self.state.update(values)
        latency = self.metrics["latencies_ms"]
        self.state["metrics"] = {
            "chat_events_seen": self.metrics["chat_events_seen"],
            "gift_events_seen": self.metrics["gift_events_seen"],
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
            "skipped": dict(self.metrics["skipped"]),
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.settings.queue_size,
            "is_speaking": self.metrics["is_speaking"],
            "average_event_to_playback_ms": (
                round(sum(latency) / len(latency)) if latency else None
            ),
            "last_chat_at_local": self.metrics["last_chat_at_local"],
            "last_spoken_at_local": self.metrics["last_spoken_at_local"],
            "last_error": self.metrics["last_error"],
            "tts_retry_after_local": self.metrics["tts_retry_after_local"],
        }
        self.state["updated_at_local"] = now_local()
        write_json(STATE_PATH, self.state)

    def _close_tail(self) -> None:
        if self.file_handle is not None:
            self.file_handle.close()
            self.file_handle = None
        self.session_path = None
        self.pending_bytes = b""

    def _attach(self, path: Path) -> None:
        self._close_tail()
        event_path = path / "events.ndjson"
        self.file_handle = event_path.open("rb")
        # Starting midway through an existing LIVE must never replay old chat.
        # If we were already running before a new collector session appeared,
        # read that new session from its beginning instead.
        if self.first_session:
            self.file_handle.seek(0, os.SEEK_END)
            self.first_session = False
        self.session_path = path
        print(f"[tts] following active collector session {path.name}", flush=True)

    async def _process_line(self, line: bytes) -> None:
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.metrics["skipped"]["invalid_event_line"] += 1
            return
        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        if event_type == "chat":
            self.metrics["chat_events_seen"] += 1
            self.metrics["last_chat_at_local"] = event.get("received_at_local")
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
            sender = str(
                event.get("nickname")
                or (f"@{event['unique_id']}" if event.get("unique_id") else "\u6709\u89c0\u773e")
            ).strip()
            gift_name = str(event.get("gift_name") or "\u79ae\u7269").strip()
            try:
                quantity = max(1, int(event.get("repeat_count") or 1))
            except (TypeError, ValueError):
                quantity = 1
            event["comment"] = (
                f"{sender} \u9001\u51fa {gift_name}\uff0c\u6578\u91cf {quantity} \u500b"
            )
        else:
            return

        prepared, reason = self.processor.prepare(event)
        if prepared is None:
            self.metrics["skipped"][reason or "filtered"] += 1
            return
        # Backpressure the file reader instead of dropping accepted events.
        # The append-only event file remains the durable source of truth.
        event_time = time.monotonic()
        if prepared.received_at_ms is not None:
            age_at_enqueue = max(
                0.0,
                (time.time() * 1000 - prepared.received_at_ms) / 1000,
            )
            event_time -= age_at_enqueue
        await self.queue.put(
            (1, self._next_queue_sequence(), prepared, event_time, event_type)
        )
        self.metrics[f"{event_type}_queued"] += 1

    async def _read_new_events(self) -> None:
        if self.file_handle is None:
            return
        try:
            current_size = self.file_handle.seek(0, os.SEEK_END)
            position = getattr(self, "_tail_position", None)
            if position is None:
                position = current_size
            if current_size < position:
                position = 0
                self.pending_bytes = b""
            self.file_handle.seek(position)
            chunk = self.file_handle.read()
            self._tail_position = self.file_handle.tell()
        except OSError:
            self._close_tail()
            self._tail_position = None
            return

        if not chunk:
            return
        rows = (self.pending_bytes + chunk).split(b"\n")
        self.pending_bytes = rows.pop()
        for row in rows:
            if row.strip():
                await self._process_line(row)

    def _next_queue_sequence(self) -> int:
        self.queue_sequence += 1
        return self.queue_sequence

    def _reload_runtime_settings(self) -> None:
        updated = TTSSettings.from_mapping(read_json(CONFIG_PATH, {}))
        self.settings = updated
        self.processor.settings = updated

    def _event_is_stale(
        self, event_type: str, enqueued_at: float, max_age: float
    ) -> bool:
        return (
            event_type != "preview"
            and max_age > 0
            and time.monotonic() - enqueued_at > max_age
        )

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
        await self.queue.put(
            (0, self._next_queue_sequence(), message, time.monotonic(), "preview")
        )
        self.last_preview_id = preview_id
        self.metrics["preview_queued"] += 1

    async def _speak(self) -> None:
        while True:
            _, _, message, enqueued_at, event_type = await self.queue.get()
            audio_path = None
            try:
                max_age = self.settings.max_queue_age_seconds
                expired = False
                if self._event_is_stale(event_type, enqueued_at, max_age):
                    self.metrics["skipped"][f"stale_{event_type}"] += 1
                    continue
                spoken = False
                for attempt in range(3):
                    if self._event_is_stale(event_type, enqueued_at, max_age):
                        self.metrics["skipped"][f"stale_{event_type}"] += 1
                        expired = True
                        break
                    cooldown = self.backend_retry_at - time.monotonic()
                    if cooldown > 0:
                        await asyncio.sleep(cooldown)
                    if self._event_is_stale(event_type, enqueued_at, max_age):
                        self.metrics["skipped"][f"stale_{event_type}"] += 1
                        expired = True
                        break

                    remaining = (
                        self.last_synthesis_request_at
                        + self.settings.min_request_interval_seconds
                        - time.monotonic()
                    )
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    if self._event_is_stale(event_type, enqueued_at, max_age):
                        self.metrics["skipped"][f"stale_{event_type}"] += 1
                        expired = True
                        break

                    self.metrics["is_speaking"] = True
                    started = time.monotonic()
                    self.last_synthesis_request_at = started
                    try:
                        audio_path = await self.backend.synthesize(
                            message.text, message.voice, self.settings.rate_percent
                        )
                        await self.player.play(audio_path, self.settings.volume)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        self.metrics["synthesis_or_playback_errors"] += 1
                        self.metrics["last_error"] = type(error).__name__
                        self.backend_failures += 1
                        backoff = min(60, 5 * (2 ** min(self.backend_failures - 1, 4)))
                        self.backend_retry_at = time.monotonic() + backoff
                        self.metrics["tts_retry_after_local"] = (
                            datetime.now(TAIPEI_TZ) + timedelta(seconds=backoff)
                        ).isoformat(timespec="seconds")
                        print(
                            f"[tts-error] {type(error).__name__}; retry {attempt + 1}/3",
                            flush=True,
                        )
                        if audio_path is not None:
                            audio_path.unlink(missing_ok=True)
                            audio_path = None
                        self.metrics["is_speaking"] = False
                        if (
                            event_type != "preview"
                            and max_age > 0
                            and time.monotonic() - enqueued_at > max_age
                        ):
                            self.metrics["skipped"][f"stale_{event_type}"] += 1
                            expired = True
                            break
                        continue

                    self.backend_failures = 0
                    self.backend_retry_at = 0.0
                    self.metrics["spoken"] += 1
                    if event_type in {"chat", "gift"}:
                        self.metrics[f"{event_type}_spoken"] += 1
                    elif event_type == "preview":
                        self.metrics["preview_spoken"] += 1
                    self.metrics["last_spoken_at_local"] = now_local()
                    self.metrics["last_error"] = None
                    self.metrics["tts_retry_after_local"] = None
                    if event_type != "preview" and message.received_at_ms is not None:
                        event_to_playback_ms = max(
                            0,
                            int(time.time() * 1000) - message.received_at_ms,
                        )
                        self.metrics["latencies_ms"].append(event_to_playback_ms)
                        self.metrics["latencies_ms"] = self.metrics["latencies_ms"][-100:]
                        print(
                            f"[tts] finished {event_type} after {event_to_playback_ms}ms from event arrival",
                            flush=True,
                        )
                    spoken = True
                    break

                if not spoken and not expired:
                    self.metrics["skipped"]["tts_retry_exhausted"] += 1
            except asyncio.CancelledError:
                raise
            finally:
                if audio_path is not None:
                    audio_path.unlink(missing_ok=True)
                self.metrics["is_speaking"] = False
                self.queue.task_done()

    async def run(self) -> int:
        lock = _acquire_lock(LOCK_PATH)
        if lock is None:
            print("[tts] another TTS worker already holds the lock", flush=True)
            return 2

        self.player.initialize()
        if self.benchmark_session_dir is not None:
            existing_session = self.benchmark_session_dir
        else:
            existing_session, _ = find_active_session(
                RAW_ROOT, WATCHER_STATE, self.username
            )
        # A worker launched before a collector exists should read its first
        # session from the beginning. When launched mid-session it seeks EOF.
        self.first_session = existing_session is not None
        playback_task = asyncio.create_task(self._speak())
        last_state_write = 0.0
        preferred = None
        try:
            print(f"[tts] started for @{self.username}; no additional LIVE connection", flush=True)
            while not STOP_PATH.exists():
                self._reload_runtime_settings()
                await self._enqueue_voice_preview()
                if self.benchmark_session_dir is not None:
                    active = (
                        self.benchmark_session_dir
                        if (self.benchmark_session_dir / "events.ndjson").is_file()
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
                        self._tail_position = None
                        preferred = None
                    current_status = status
                    state_values["active_session"] = None
                else:
                    if active != self.session_path:
                        self._attach(active)
                        self._tail_position = self.file_handle.tell()
                    preferred = active
                    await self._read_new_events()
                    current_status = (
                        "speaking" if self.metrics["is_speaking"] else "watching"
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
            self.player.stop()
            self.player.close()
            self._close_tail()
            STOP_PATH.unlink(missing_ok=True)
            self.state["pid"] = None
            self.update_state("stopped", stopped_at_local=now_local())
            lock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Tail an explicit benchmark session under data/v2_provider_benchmark.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    username = args.username.strip().lstrip("@")
    if not username or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_." for char in username):
        print("[tts] invalid streamer username", file=sys.stderr, flush=True)
        return 2
    worker = TTSWorker(username, session_dir=args.session_dir)
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
