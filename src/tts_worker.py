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
WATCHER_STATE = ROOT / "data" / "watcher" / "watcher_state.json"
CONFIG_PATH = DATA_ROOT / "config.json"
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
    def __init__(self, username: str):
        self.username = username
        raw = read_json(CONFIG_PATH, {})
        self.settings = TTSSettings.from_mapping(raw)
        self.processor = ChatProcessor(self.settings)
        self.queue: asyncio.Queue[tuple[PreparedChat, float]] = asyncio.Queue(
            maxsize=self.settings.queue_size
        )
        self.backend = EdgeTTSBackend()
        self.player = PygameAudioPlayer()
        self.session_path: Path | None = None
        self.file_handle = None
        self.pending_bytes = b""
        self.first_session = True
        self.metrics = {
            "chat_events_seen": 0,
            "spoken": 0,
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
            "spoken": self.metrics["spoken"],
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

    def _process_line(self, line: bytes) -> None:
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.metrics["skipped"]["invalid_event_line"] += 1
            return
        if not isinstance(event, dict) or event.get("type") != "chat":
            return

        self.metrics["chat_events_seen"] += 1
        self.metrics["last_chat_at_local"] = event.get("received_at_local")
        prepared, reason = self.processor.prepare(event)
        if prepared is None:
            self.metrics["skipped"][reason or "filtered"] += 1
            return
        try:
            self.queue.put_nowait((prepared, time.monotonic()))
        except asyncio.QueueFull:
            self.metrics["skipped"]["queue_full"] += 1

    def _read_new_events(self) -> None:
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
                self._process_line(row)

    async def _speak(self) -> None:
        while True:
            item, enqueued_at = await self.queue.get()
            message = item
            audio_path = None
            try:
                if time.monotonic() < self.backend_retry_at:
                    self.metrics["skipped"]["tts_backend_cooldown"] += 1
                    continue

                remaining = (
                    self.last_synthesis_request_at
                    + self.settings.min_request_interval_seconds
                    - time.monotonic()
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)

                self.metrics["is_speaking"] = True
                started = time.monotonic()
                if message.received_at_ms is not None:
                    self.metrics["latencies_ms"].append(
                        max(0, int(time.time() * 1000) - message.received_at_ms)
                    )
                    self.metrics["latencies_ms"] = self.metrics["latencies_ms"][-100:]
                audio_path = await self.backend.synthesize(
                    message.text, message.voice, self.settings.rate_percent
                )
                self.last_synthesis_request_at = started
                await self.player.play(audio_path, self.settings.volume)
                self.backend_failures = 0
                self.backend_retry_at = 0.0
                self.metrics["spoken"] += 1
                self.metrics["last_spoken_at_local"] = now_local()
                self.metrics["last_error"] = None
                self.metrics["tts_retry_after_local"] = None
                queue_wait_ms = int((started - enqueued_at) * 1000)
                if queue_wait_ms > 0:
                    print(f"[tts] played message after {queue_wait_ms}ms queue wait", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Keep remote error payloads and chat text out of local logs.
                self.metrics["synthesis_or_playback_errors"] += 1
                self.metrics["last_error"] = type(error).__name__
                self.backend_failures += 1
                backoff = min(60, 5 * (2 ** min(self.backend_failures - 1, 4)))
                self.backend_retry_at = time.monotonic() + backoff
                self.metrics["tts_retry_after_local"] = (
                    datetime.now(TAIPEI_TZ) + timedelta(seconds=backoff)
                ).isoformat(timespec="seconds")
                print(f"[tts-error] {type(error).__name__}", flush=True)
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
                    self._read_new_events()
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
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    username = args.username.strip().lstrip("@")
    if not username or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_." for char in username):
        print("[tts] invalid streamer username", file=sys.stderr, flush=True)
        return 2
    worker = TTSWorker(username)
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
