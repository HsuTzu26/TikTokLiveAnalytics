from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from collections import Counter, defaultdict, deque
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import altair as alt
import pandas as pd
import streamlit as st
from app.i18n import render_language_selector, tr

# Streamlit reruns scripts in a long-lived process. Reload the local analytics
# module so newly added functions do not remain hidden behind Python's module cache.
import importlib
import src.analytics as analytics_module
import src.live_summary as live_summary_module

analytics_module = importlib.reload(analytics_module)
live_summary_module = importlib.reload(live_summary_module)
audience_metrics = analytics_module.audience_metrics
build_health_report = analytics_module.build_health_report
compare_sessions = analytics_module.compare_sessions
gift_activity_by_minute = analytics_module.gift_activity_by_minute
gift_concentration = analytics_module.gift_concentration
gift_detail = analytics_module.gift_detail
gift_leaderboard = analytics_module.gift_leaderboard
social_activity_by_minute = analytics_module.social_activity_by_minute
social_conversion_proxies = analytics_module.social_conversion_proxies
snapshot_history = analytics_module.snapshot_history
ranking_history = analytics_module.ranking_history
social_summary = analytics_module.social_summary
traffic_sources = analytics_module.traffic_sources


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
RAW_ROOT = DATA_ROOT / "raw"
BENCHMARK_ROOT = DATA_ROOT / "v2_provider_benchmark"
BROWSER_PROBE_ROOT = DATA_ROOT / "browser_ws_probe"
WATCHER_STATE = DATA_ROOT / "watcher" / "watcher_state.json"
WATCHER_LOG = DATA_ROOT / "watcher" / "watcher.log"
WATCHER_PID = DATA_ROOT / "watcher" / "watcher.pid"
WATCHER_STOP = DATA_ROOT / "watcher" / "watcher.stop"
CONFIG_PATH = ROOT / "watchlist.json"
TAIPEI_TZ = "Asia/Taipei"
LIVE_REFRESH_SECONDS = 2
EMOTE_IMAGE_HOSTS = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "ibytedtos.com",
    "byteoversea.com",
    "ibyteimg.com",
)


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return 0, 0
    return stat.st_mtime_ns, stat.st_size


@st.cache_data(max_entries=32)
def _cached_session_report(
    session_dir: str,
    events_mtime_ns: int,
    events_size: int,
    metadata_mtime_ns: int,
    tts_state_mtime_ns: int,
    gift_catalog_mtime_ns: int,
) -> dict:
    """Cache post-LIVE summaries until one of their source files changes."""
    del events_mtime_ns, events_size, metadata_mtime_ns, tts_state_mtime_ns, gift_catalog_mtime_ns
    return live_summary_module.build_live_summary(Path(session_dir))


def load_session_report(session_dir: Path) -> dict:
    events_mtime, events_size = _file_signature(session_dir / "events.ndjson")
    metadata_mtime, _ = _file_signature(session_dir / "session.json")
    tts_state_mtime, _ = _file_signature(DATA_ROOT / "tts" / "state.json")
    catalog_mtime, _ = _file_signature(ROOT / "src" / "tts" / "gift_catalog.json")
    return _cached_session_report(
        str(session_dir.resolve()),
        events_mtime,
        events_size,
        metadata_mtime,
        tts_state_mtime,
        catalog_mtime,
    )


st.set_page_config(
    page_title="TikTok LIVE Analytics",
    page_icon="LIVE",
    layout="wide",
)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_tail_lines(path: Path, limit: int = 10) -> list[str]:
    """Read the latest log lines without retaining the whole file."""
    limit = max(1, int(limit))
    with path.open("r", encoding="utf-8", errors="replace") as rows:
        return list(deque((line.rstrip("\r\n") for line in rows), maxlen=limit))


def trusted_emote_image_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold()
    except ValueError:
        return None
    if parsed.scheme != "https" or not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in EMOTE_IMAGE_HOSTS
    ):
        return None
    return value[:2048]


def pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def watcher_pid() -> int | None:
    try:
        return int(WATCHER_PID.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


@lru_cache(maxsize=1)
def watcher_python() -> Path | None:
    """Find an installed runtime for the TikTokLive-backed status probe."""
    venv_python = (
        ROOT / ".venv_tiktoklive" / "Scripts" / "python.exe"
        if os.name == "nt"
        else ROOT / ".venv_tiktoklive" / "bin" / "python"
    )
    candidates = [venv_python, Path(sys.executable)]
    checked = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in checked or not resolved.is_file():
            continue
        checked.add(resolved)
        try:
            result = subprocess.run(
                [str(resolved), "-c", "import TikTokLive"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return resolved
    return None


def start_watcher() -> str:
    pid = watcher_pid()
    if pid_is_running(pid):
        return tr(f"Watcher already running (PID {pid}).", f"Watcher 已在執行中（PID {pid}）。")

    python = watcher_python()
    if python is None:
        return tr(
            "Watcher needs a Python environment with TikTokLive installed.",
            "Watcher 需要安裝 TikTokLive 的 Python 環境。",
        )

    WATCHER_PID.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_PID.unlink(missing_ok=True)
    process = subprocess.Popen(
        [
            str(python),
            str(ROOT / "src" / "watcher.py"),
            "--config",
            str(CONFIG_PATH),
            "--probe-only",
        ],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        ),
    )
    # watcher.py owns watcher.pid. Do not write the dashboard launcher PID:
    # the venv launcher may spawn a different Python process and the watcher
    # would otherwise exit with "already running".
    for _ in range(30):
        time.sleep(0.1)
        pid = watcher_pid()
        if pid and pid_is_running(pid):
            return tr(f"Watcher started (PID {pid}).", f"Watcher 已啟動（PID {pid}）。")
        if process.poll() is not None:
            break
    if process.poll() is None:
        return tr(f"Watcher start requested (launcher PID {process.pid}); PID file not ready yet.", f"已要求啟動 Watcher（啟動器 PID {process.pid}）；PID 檔尚未就緒。")
    return tr(f"Watcher failed to start (exit code {process.returncode}); check {WATCHER_LOG}.", f"Watcher 啟動失敗（結束碼 {process.returncode}）；請查看 {WATCHER_LOG}。")


def stop_watcher() -> str:
    pid = watcher_pid()
    if not pid_is_running(pid):
        WATCHER_PID.unlink(missing_ok=True)
        return tr("Watcher is not running.", "Watcher 尚未執行。")

    WATCHER_STOP.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_STOP.write_text("requested", encoding="ascii")
    return tr(f"Graceful stop requested for Watcher (PID {pid}).", f"已要求平順停止 Watcher（PID {pid}）。")


def session_dirs(username: str | None = None) -> list[Path]:
    result = []
    for root in (RAW_ROOT, BROWSER_PROBE_ROOT):
        if not root.exists():
            continue
        try:
            for path in root.iterdir():
                if not path.is_dir():
                    continue
                metadata = session_metadata(path)
                if not isinstance(metadata, dict):
                    continue
                if not (path / "session.json").exists() and not _browser_capture_is_active(path):
                    continue
                session_username = str(metadata.get("username") or "")
                if username and session_username.casefold() != username.casefold():
                    continue
                result.append(path)
        except OSError:
            continue
    return sorted(result, key=lambda item: item.name, reverse=True)


def session_metadata(path: Path) -> dict:
    """Read metadata from Collector, Browser Network, or benchmark sessions."""
    metadata = read_json(path / "session.json", {})
    if metadata:
        if _is_browser_session(path):
            metadata.setdefault("provider", "browser_network")
        return metadata
    summary = read_json(path / "summary.json", {})
    if summary:
        return summary
    if _browser_capture_is_active(path):
        parts = path.name.split("_", 2)
        username = parts[2] if len(parts) == 3 else "unknown"
        return {
            "session_id": path.name,
            "username": username,
            "provider": "browser_network",
            "browser_mode": "cdp",
            "status": "running",
        }
    return {}


def _is_browser_session(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(BROWSER_PROBE_ROOT.resolve())
    except (OSError, ValueError):
        return False


def _browser_capture_is_active(path: Path) -> bool:
    if not _is_browser_session(path) or (path / "session.json").exists():
        return False
    events_path = path / "events.ndjson"
    try:
        return events_path.is_file() and time.time() - events_path.stat().st_mtime <= 120
    except OSError:
        return False


def active_browser_session_dirs() -> list[Path]:
    if not BROWSER_PROBE_ROOT.exists():
        return []
    active = []
    try:
        for path in BROWSER_PROBE_ROOT.iterdir():
            if not path.is_dir():
                continue
            metadata = read_json(path / "session.json", {})
            metadata_running = metadata.get("status") == "running"
            try:
                snapshot_fresh = (
                    (path / "session.json").is_file()
                    and time.time() - (path / "session.json").stat().st_mtime <= 30
                )
            except OSError:
                snapshot_fresh = False
            if (metadata_running and snapshot_fresh) or _browser_capture_is_active(path):
                if (path / "events.ndjson").is_file():
                    active.append(path)
    except OSError:
        return []
    return sorted(active, key=lambda item: item.name, reverse=True)


_BROWSER_LIVE_CACHE: dict[str, dict] = {}
_BROWSER_VIEWER_CORRECTION_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="browser-viewer-correction",
)
_BROWSER_VIEWER_CORRECTION_JOBS: dict[str, Future] = {}


@lru_cache(maxsize=1024)
def _decode_browser_viewer_frame(
    frame_path: str, modified_ns: int, size: int
) -> tuple[tuple[str, int], ...]:
    """Read the current viewer count from older raw frames when available."""
    del modified_ns, size
    try:
        from src.providers.browser_network import decode_webcast_frame

        events, _ = decode_webcast_frame(Path(frame_path).read_bytes())
    except Exception:
        return ()
    decoded = []
    for envelope in events:
        event = envelope.get("normalized") if isinstance(envelope, dict) else None
        if not isinstance(event, dict):
            event = envelope
        if not isinstance(event, dict) or event.get("type") != "viewer":
            continue
        value = event.get("viewer_count")
        if isinstance(value, (int, float)):
            msg_id = event.get("msg_id")
            if not msg_id and isinstance(envelope, dict):
                msg_id = envelope.get("msg_id")
            decoded.append((str(msg_id or ""), int(value)))
    return tuple(decoded)


def browser_viewer_count(session_dir: Path, event: dict) -> int | float | None:
    """Use the frame's current audience field to fix sessions captured by old code."""
    try:
        frame_seq = int(event.get("frame_seq"))
        if frame_seq < 1:
            return event.get("viewer_count")
        frame_path = session_dir / "frames" / f"frame_{frame_seq:06d}.bin"
        stat = frame_path.stat()
        counts = _decode_browser_viewer_frame(
            str(frame_path), stat.st_mtime_ns, stat.st_size
        )
    except (OSError, TypeError, ValueError):
        return event.get("viewer_count")
    msg_id = str(event.get("msg_id") or "")
    if msg_id:
        match = next((count for candidate, count in counts if candidate == msg_id), None)
        if match is not None:
            return match
    if len(counts) == 1:
        return counts[0][1]
    return event.get("viewer_count")


def _build_corrected_browser_viewer_rows(session_dir: str) -> list[dict]:
    """Rebuild archived viewer samples from raw frames without blocking the UI."""
    root = Path(session_dir)
    rows = []
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            kernel.SetThreadPriority(kernel.GetCurrentThread(), -1)
        except (AttributeError, OSError):
            pass
    try:
        with (root / "events.ndjson").open("r", encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") not in {"viewer", "roomUserSeq"}:
                    continue
                received_at_ms = event.get("received_at_ms") or event.get("timestamp_ms")
                event_time = (
                    event.get("received_at_local")
                    or event.get("timestamp_local")
                    or event.get("received_at_utc")
                    or event.get("timestamp_utc")
                )
                if not event_time and isinstance(received_at_ms, (int, float)):
                    event_time = pd.Timestamp(received_at_ms, unit="ms", tz="UTC").tz_convert(TAIPEI_TZ).isoformat()
                value = browser_viewer_count(root, event)
                if isinstance(value, (int, float)) and event_time:
                    rows.append({
                        "time": event_time,
                        "received_at_ms": received_at_ms if isinstance(received_at_ms, (int, float)) else None,
                        "viewer_count": value,
                        "session_id": event.get("session_id") or root.name,
                    })
                # Yield between protobuf frames so UI work and browser input
                # retain priority on slower Windows machines.
                time.sleep(0.001)
    except OSError:
        return []
    rows.sort(key=_viewer_sample_order)
    return rows


def corrected_browser_viewer_rows(session_dir: Path) -> list[dict] | None:
    """Return corrected archived samples when ready, scheduling work otherwise."""
    session_dir = Path(session_dir).resolve()
    events_mtime_ns, events_size = _file_signature(session_dir / "events.ndjson")
    frames_mtime_ns, _ = _file_signature(session_dir / "frames")
    job_key = f"{session_dir}:{events_mtime_ns}:{events_size}:{frames_mtime_ns}"
    future = _BROWSER_VIEWER_CORRECTION_JOBS.get(job_key)
    if future is None:
        future = _BROWSER_VIEWER_CORRECTION_EXECUTOR.submit(
            _build_corrected_browser_viewer_rows,
            str(session_dir),
        )
        _BROWSER_VIEWER_CORRECTION_JOBS[job_key] = future
        while len(_BROWSER_VIEWER_CORRECTION_JOBS) > 32:
            oldest_key = next(iter(_BROWSER_VIEWER_CORRECTION_JOBS))
            oldest = _BROWSER_VIEWER_CORRECTION_JOBS[oldest_key]
            if not oldest.done():
                break
            _BROWSER_VIEWER_CORRECTION_JOBS.pop(oldest_key, None)
    if not future.done():
        return None
    try:
        return future.result()
    except Exception:
        return []


def _correct_browser_metrics(
    metrics: dict,
    session_dirs_to_correct: list[Path],
    *,
    request_correction: bool = False,
) -> tuple[dict, bool, bool]:
    """Keep archived Browser viewer corrections opt-in and off the UI thread."""
    browser_paths = [path for path in session_dirs_to_correct if _is_browser_session(path)]
    if not browser_paths:
        return metrics, False, False
    browser_session_ids = {path.name for path in browser_paths}
    corrected_rows = [
        row for row in metrics.get("viewer_rows", [])
        if row.get("session_id") not in browser_session_ids
    ]
    pending = False
    required = False
    for path in browser_paths:
        source_rows = [
            row for row in metrics.get("viewer_rows", [])
            if row.get("session_id") == path.name
        ]
        frame_rows = [row for row in source_rows if row.get("frame_seq")]
        has_saved_frame = any(
            (path / "frames" / f"frame_{int(row['frame_seq']):06d}.bin").is_file()
            for row in frame_rows[:8]
        )
        if not has_saved_frame:
            corrected_rows.extend(source_rows)
            continue
        if not request_correction:
            required = True
            continue
        rows = corrected_browser_viewer_rows(path)
        if rows is None:
            pending = True
        else:
            corrected_rows.extend(rows)
    if not pending:
        corrected_rows.sort(key=_viewer_sample_order)
    result = dict(metrics)
    result["viewer_rows"] = corrected_rows
    result["viewers"] = [row["viewer_count"] for row in result["viewer_rows"]]
    result["viewer_correction_pending"] = pending
    result["viewer_correction_required"] = required
    return result, pending, required


def browser_live_state(session_dir: Path) -> dict:
    """Incrementally read normalized Browser Provider events for the live page."""
    events_path = session_dir / "events.ndjson"
    key = str(events_path.resolve())
    try:
        size = events_path.stat().st_size
    except OSError:
        return {}
    cache = _BROWSER_LIVE_CACHE.get(key)
    if cache is None or size < cache["offset"]:
        cache = {
            "offset": 0,
            "event_counts": Counter(),
            "viewer_samples": deque(maxlen=600),
            "recent_chat": deque(maxlen=100),
            "recent_gifts": deque(maxlen=100),
            "recent_members": deque(maxlen=100),
            "viewer_count": None,
            "latest_viewer_at_ms": None,
            "likes_total": 0,
            "diamonds": 0,
            "chat_count": 0,
            "gift_count": 0,
            "member_count": 0,
            "last_event_at_ms": None,
        }
        _BROWSER_LIVE_CACHE[key] = cache

    if size > cache["offset"]:
        initial_read = cache["offset"] == 0
        try:
            with events_path.open("rb") as handle:
                handle.seek(cache["offset"])
                payload = handle.read()
                cache["offset"] = handle.tell()
        except OSError:
            payload = b""
        for raw_line in payload.splitlines():
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "unknown")
            cache["event_counts"][event_type] += 1
            received_ms = event.get("received_at_ms") or event.get("timestamp_ms")
            if isinstance(received_ms, (int, float)):
                cache["last_event_at_ms"] = int(received_ms)
            event_time = (
                event.get("received_at_local")
                or event.get("timestamp_local")
                or event.get("received_at_utc")
                or event.get("timestamp_utc")
            )
            user = event.get("unique_id") or event.get("nickname") or event.get("user_id")
            if event_type == "viewer":
                value = (
                    event.get("viewer_count")
                    if initial_read
                    else browser_viewer_count(session_dir, event)
                )
                if isinstance(value, (int, float)):
                    cache["viewer_count"] = int(value)
                    cache["latest_viewer_at_ms"] = received_ms
                    cache["viewer_samples"].append({
                        "received_at_ms": received_ms,
                        "viewer_count": int(value),
                    })
                    if initial_read:
                        cache["bootstrap_latest_viewer_event"] = event
            elif event_type == "like":
                try:
                    total = int(event.get("total_likes") or 0)
                    increment = int(event.get("like_count") or 0)
                    cache["likes_total"] = max(cache["likes_total"], total or cache["likes_total"] + increment)
                except (TypeError, ValueError, OverflowError):
                    pass
            elif event_type == "chat":
                cache["chat_count"] += 1
                cache["recent_chat"].append({
                    "time": event_time,
                    "user": user,
                    "comment": event.get("comment"),
                })
            elif event_type == "gift" and event.get("counted"):
                cache["gift_count"] += 1
                try:
                    cache["diamonds"] += int(event.get("diamond_total") or 0)
                except (TypeError, ValueError, OverflowError):
                    pass
                cache["recent_gifts"].append({
                    "time": event_time,
                    "user": user,
                    "gift_name": event.get("gift_name") or "unknown",
                    "gift_id": event.get("gift_id"),
                    "quantity": event.get("repeat_count") or 1,
                    "diamonds": event.get("diamond_total") or 0,
                })
            elif event_type == "member":
                cache["member_count"] += 1
                cache["recent_members"].append({
                    "time": event_time,
                    "user": user,
                    "fan_club_level": event.get("fan_club_level"),
                    "user_grade_level": event.get("user_grade_level"),
                    "entry_source": event.get("entry_source"),
                })

        if initial_read and cache.get("bootstrap_latest_viewer_event"):
            latest_viewer_event = cache.pop("bootstrap_latest_viewer_event")
            corrected_latest = browser_viewer_count(session_dir, latest_viewer_event)
            if isinstance(corrected_latest, (int, float)):
                cache["viewer_count"] = int(corrected_latest)
                cache["latest_viewer_at_ms"] = (
                    latest_viewer_event.get("received_at_ms")
                    or latest_viewer_event.get("timestamp_ms")
                )
                if cache["viewer_samples"]:
                    cache["viewer_samples"][-1]["viewer_count"] = int(corrected_latest)

    latest_socket_state = "unknown"
    live_sockets: set[str] = set()
    target_connections = 0
    target_url = None
    try:
        with (session_dir / "sockets.ndjson").open("r", encoding="utf-8") as rows:
            for line in rows:
                try:
                    socket = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not socket.get("live_candidate"):
                    continue
                request_id = str(socket.get("request_id") or "")
                state = socket.get("state")
                if state == "created":
                    target_connections += 1
                    if request_id:
                        live_sockets.add(request_id)
                elif state in {"handshake", "classified_live"}:
                    latest_socket_state = "connected"
                    target_url = socket.get("url") or target_url
                    if request_id:
                        live_sockets.add(request_id)
                elif state == "closed":
                    if request_id:
                        live_sockets.discard(request_id)
                    latest_socket_state = "reconnecting" if not live_sockets else "connected"
    except OSError:
        pass
    if live_sockets:
        latest_socket_state = "connected"
    elif cache["event_counts"].get("live_ended"):
        latest_socket_state = "session_ended"
    elif latest_socket_state == "unknown" and cache["event_counts"]:
        latest_socket_state = "reconnecting"

    last_event_ms = cache.get("last_event_at_ms")
    return {
        "username": session_metadata(session_dir).get("username"),
        "connection_state": latest_socket_state,
        "target_websocket_connection_count": target_connections,
        "target_websocket_url": target_url,
        "last_event_at_ms": last_event_ms,
        "viewer_count": cache["viewer_count"],
        "latest_viewer_at_ms": cache["latest_viewer_at_ms"],
        "viewer_samples": list(cache["viewer_samples"]),
        "likes_total": cache["likes_total"],
        "diamonds": cache["diamonds"],
        "chat_count": cache["chat_count"],
        "gift_count": cache["gift_count"],
        "member_count": cache["member_count"],
        "recent_chat": list(cache["recent_chat"]),
        "recent_gifts": list(cache["recent_gifts"]),
        "recent_members": list(cache["recent_members"]),
        "event_counts": dict(cache["event_counts"]),
    }


def benchmark_session_dirs() -> list[Path]:
    """Return provider benchmark sessions that have recorded events."""
    if not BENCHMARK_ROOT.exists():
        return []
    result = []
    try:
        for provider_dir in BENCHMARK_ROOT.iterdir():
            if not provider_dir.is_dir():
                continue
            for path in provider_dir.iterdir():
                if (
                    path.is_dir()
                    and (path / "events.ndjson").is_file()
                    and (path / "events.ndjson").stat().st_size > 0
                ):
                    result.append(path)
    except OSError:
        return []
    return sorted(result, key=lambda item: item.stat().st_mtime, reverse=True)


def active_benchmark_session_dirs() -> list[Path]:
    """Return benchmark captures that still appear to be collecting."""
    active = []
    for path in benchmark_session_dirs():
        metadata = session_metadata(path)
        status = str(metadata.get("status") or "").casefold()
        if status in {"running", "collecting", "connecting"}:
            active.append(path)
            continue
        if metadata.get("ended_at_utc") or metadata.get("live_ended") or status in {
            "stopped", "ended", "error", "failed"
        }:
            continue
        # An in-flight benchmark may not write summary.json until shutdown.
        try:
            if time.time() - (path / "events.ndjson").stat().st_mtime <= 120:
                active.append(path)
        except OSError:
            continue
    return active


_SESSION_ANALYTICS_CACHE: dict[str, tuple[int, int, bool]] = {}


def session_has_analytics(path: Path) -> bool:
    """Use compact metadata first; scan legacy NDJSON at most once per file version."""
    metadata = session_metadata(path)
    live_state = read_json(path / "live_state.json", {})
    event_counts = {}
    for counts in (metadata.get("event_counts"), live_state.get("event_counts")):
        if isinstance(counts, dict):
            event_counts.update(counts)
    if event_counts:
        return any(
            event_type not in {None, "system", "room", "control"}
            and int(count or 0) > 0
            for event_type, count in event_counts.items()
        )
    if metadata.get("status") == "running":
        return True

    events_path = path / "events.ndjson"
    try:
        stat = events_path.stat()
        cache_key = str(events_path.resolve())
        cached = _SESSION_ANALYTICS_CACHE.get(cache_key)
        if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]
        with events_path.open("r", encoding="utf-8", errors="replace") as rows:
            for line in rows:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") not in {None, "system", "room", "control"} and isinstance(event.get("timestamp_ms"), (int, float)):
                    _SESSION_ANALYTICS_CACHE[cache_key] = (
                        stat.st_size,
                        stat.st_mtime_ns,
                        True,
                    )
                    return True
    except OSError:
        return False
    _SESSION_ANALYTICS_CACHE[cache_key] = (
        stat.st_size,
        stat.st_mtime_ns,
        False,
    )
    if len(_SESSION_ANALYTICS_CACHE) > 512:
        _SESSION_ANALYTICS_CACHE.pop(next(iter(_SESSION_ANALYTICS_CACHE)))
    return False


def session_label(path: Path) -> str:
    meta = session_metadata(path)
    status = str(meta.get("status") or "unknown")
    kind = "analytics" if session_has_analytics(path) else "system only"
    return f"{path.name}  |  {status}  |  {kind}"


def resolve_session(manual_id: str, username: str, selected_id: str) -> Path | None:
    candidates = session_dirs(username) + [
        path for path in benchmark_session_dirs()
        if str(session_metadata(path).get("username") or "").lstrip("@") == username
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    manual_id = manual_id.strip()
    if manual_id:
        for candidate in candidates:
            if candidate.name == manual_id:
                return candidate

        # Also accept a TikTok room_id and resolve it to the newest matching
        # captured session for the selected streamer.
        matches = []
        for path in candidates:
            meta = session_metadata(path)
            source_ids = [str(value) for value in meta.get("source_session_ids") or []]
            if (
                str(meta.get("room_id") or "") == manual_id
                or str(meta.get("session_id") or "") == manual_id
                or manual_id in source_ids
            ):
                matches.append(path)
        return matches[0] if matches else None

    if selected_id:
        for candidate in candidates:
            if candidate.name == selected_id:
                return candidate

    return candidates[0] if candidates else None


def _load_session_uncached(session_dir: Path | None):
    summary = {
        "session": None,
        "counts": Counter(),
        "viewers": [],
        "viewer_rows": [],
        "activity_rows": [],
        "likes": 0,
        "likes_observed": 0,
        "like_current_total": None,
        "like_first_total": None,
        "like_baseline_estimate": None,
        "diamonds": 0,
        "chat": 0,
        "joins": 0,
        "follows": 0,
        "shares": 0,
        "subscribes": 0,
        "gifts": 0,
        "recent_chat": [],
        "recent_activity": [],
    }
    if session_dir is None:
        return summary

    is_browser = _is_browser_session(session_dir)
    summary["session"] = session_metadata(session_dir)
    events_path = session_dir / "events.ndjson"
    try:
        lines = events_path.open("r", encoding="utf-8")
    except OSError:
        return summary

    with lines:
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            summary["counts"][event_type] += 1
            if event_type in {"viewer", "roomUserSeq"}:
                # Viewer samples represent the count observed by this process;
                # use receive time so delayed or out-of-order source timestamps
                # cannot make an older sample appear current.
                event_time = (
                    event.get("received_at_local")
                    or event.get("timestamp_local")
                    or event.get("received_at_utc")
                    or event.get("timestamp_utc")
                )
                event_time_ms = event.get("received_at_ms") or event.get("timestamp_ms")
            else:
                event_time = (
                    event.get("timestamp_local")
                    or event.get("received_at_local")
                    or event.get("timestamp_utc")
                    or event.get("received_at_utc")
                )
                event_time_ms = event.get("timestamp_ms") or event.get("received_at_ms")
            if not event_time and isinstance(event_time_ms, (int, float)):
                event_time = pd.Timestamp(event_time_ms, unit="ms", tz="UTC").tz_convert(TAIPEI_TZ).isoformat()

            if event_type in {"viewer", "roomUserSeq"}:
                value = event.get("viewer_count")
                if isinstance(value, (int, float)) and event_time:
                    summary["viewers"].append(value)
                    summary["viewer_rows"].append({
                        "time": event_time,
                        "received_at_ms": event_time_ms if isinstance(event_time_ms, (int, float)) else None,
                        "viewer_count": value,
                        "frame_seq": event.get("frame_seq") if is_browser else None,
                        "msg_id": event.get("msg_id") if is_browser else None,
                        "session_id": session_dir.name,
                    })
            elif event_type == "like":
                batch_count = int(event.get("like_count", 0) or 0)
                summary["likes_observed"] += batch_count
                summary["likes"] = summary["likes_observed"]
                total_likes = event.get("total_likes")
                if isinstance(total_likes, (int, float)):
                    total_likes = int(total_likes)
                    if summary["like_first_total"] is None:
                        summary["like_first_total"] = total_likes
                        summary["like_baseline_estimate"] = max(0, total_likes - batch_count)
                    current = summary["like_current_total"]
                    summary["like_current_total"] = max(current or 0, total_likes)
                    summary["likes"] = summary["like_current_total"]
            elif event_type == "gift" and event.get("counted"):
                summary["gifts"] += 1
                summary["diamonds"] += event.get("diamond_total", 0) or 0
            elif event_type == "chat":
                summary["chat"] += 1
                summary["recent_chat"].append({
                    "time": event_time,
                    "user": event.get("unique_id") or event.get("nickname"),
                    "comment": event.get("comment"),
                })
            elif event_type == "member":
                summary["joins"] += 1
            elif event_type in {"social", "follow", "share"}:
                action = event.get("social_action") or (event_type if event_type != "social" else None)
                if action == "follow":
                    summary["follows"] += 1
                elif action == "share":
                    summary["shares"] += 1
            elif event_type == "subscribe":
                summary["subscribes"] += 1

            if event_time and event_type not in {"system", "room"}:
                summary["activity_rows"].append({
                    "time": event_time,
                    "event_type": event_type,
                })
                actor = event.get("unique_id") or event.get("nickname") or ""
                if event_type == "chat":
                    detail = event.get("comment") or "[emote]"
                elif event_type == "gift":
                    detail = f"{event.get('gift_name') or 'gift'} x{event.get('repeat_count') or 1}"
                elif event_type == "like":
                    detail = f"+{event.get('like_count') or 0}"
                elif event_type == "viewer":
                    detail = f"viewers={event.get('viewer_count')}"
                elif event_type == "social":
                    detail = str(event.get("social_action") or "social")
                else:
                    detail = event_type
                summary["recent_activity"].append({
                    "time": event_time,
                    "type": event_type,
                    "user": actor,
                    "detail": detail,
                })

    summary["viewer_rows"].sort(key=_viewer_sample_order)
    summary["viewers"] = [row["viewer_count"] for row in summary["viewer_rows"]]
    summary["recent_chat"] = summary["recent_chat"][-200:][::-1]
    summary["recent_activity"] = summary["recent_activity"][-50:][::-1]
    return summary


@lru_cache(maxsize=64)
def _cached_session_events(
    session_path: str,
    events_mtime_ns: int,
    events_size: int,
    metadata_mtime_ns: int,
):
    del events_mtime_ns, events_size, metadata_mtime_ns
    return _load_session_uncached(Path(session_path))


def load_session(session_dir: Path | None):
    if session_dir is None:
        return _load_session_uncached(None)
    session_dir = Path(session_dir).resolve()
    events_mtime_ns, events_size = _file_signature(session_dir / "events.ndjson")
    metadata_mtime_ns, _ = _file_signature(session_dir / "session.json")
    return _cached_session_events(
        str(session_dir), events_mtime_ns, events_size, metadata_mtime_ns
    )


def room_session_dirs(username: str, room_id: str | None) -> list[Path]:
    """Return non-aggregated session fragments captured for one LIVE room."""
    if not room_id:
        return []
    matches = []
    candidates = session_dirs(username)
    archive_root = RAW_ROOT / "archive"
    if archive_root.exists():
        candidates.extend(meta.parent for meta in archive_root.rglob("session.json"))
    seen = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        meta = read_json(path / "session.json", {})
        meta_username = str(meta.get("username") or "").lstrip("@")
        if meta_username != username or meta.get("status") == "aggregated":
            continue
        if str(meta.get("room_id") or "") == str(room_id):
            matches.append(path)
    return sorted(matches, key=lambda path: str(path))


def combine_session_metrics(paths: list[Path], preferred: Path | None = None):
    """Combine collector fragments without losing metrics after a restart."""
    combined = load_session(None)
    fragments = [load_session(path) for path in paths]
    for metrics in fragments:
        combined["counts"].update(metrics["counts"])
        for key in ("viewers", "viewer_rows", "activity_rows", "recent_chat", "recent_activity"):
            combined[key].extend(metrics[key])
        for key in ("likes_observed", "diamonds", "chat", "joins", "follows", "shares", "subscribes", "gifts"):
            combined[key] += metrics[key]
        current = metrics.get("like_current_total")
        if current is not None:
            combined["like_current_total"] = max(combined["like_current_total"] or 0, current)
        if combined["like_first_total"] is None and metrics.get("like_first_total") is not None:
            combined["like_first_total"] = metrics["like_first_total"]
            combined["like_baseline_estimate"] = metrics["like_baseline_estimate"]
    combined["likes"] = combined["like_current_total"] if combined["like_current_total"] is not None else combined["likes_observed"]
    combined["viewer_rows"].sort(key=_viewer_sample_order)
    combined["viewers"] = [row["viewer_count"] for row in combined["viewer_rows"]]
    combined["activity_rows"].sort(key=lambda row: str(row.get("time") or ""))
    combined["recent_chat"] = sorted(combined["recent_chat"], key=lambda row: str(row.get("time") or ""), reverse=True)[:200]
    combined["recent_activity"] = sorted(combined["recent_activity"], key=lambda row: str(row.get("time") or ""), reverse=True)[:50]
    combined["session"] = read_json(preferred / "session.json", {}) if preferred else (fragments[-1]["session"] if fragments else None)
    combined["fragment_count"] = len(paths)
    return combined


def _viewer_sample_order(row: dict) -> float:
    received_at_ms = row.get("received_at_ms")
    if isinstance(received_at_ms, (int, float)):
        return float(received_at_ms)
    try:
        return pd.Timestamp(row.get("time")).timestamp() * 1000
    except (TypeError, ValueError, OverflowError):
        return float("-inf")


def local_timestamp(value):
    if not value:
        return None
    try:
        return pd.Timestamp(value).tz_convert(TAIPEI_TZ).isoformat()
    except (TypeError, ValueError):
        return value


def parse_taipei_times(values) -> pd.Series:
    """Parse mixed ISO-8601 timestamps and normalize them to Taiwan time.

    Historical sessions contain both second precision
    (``...20:05:40+08:00``) and microsecond precision
    (``...20:05:40.123456+08:00``). ``format="mixed"`` prevents pandas
    from inferring one format from the first row and rejecting the other.
    Invalid timestamps become ``NaT`` and are filtered by the caller.
    """
    parsed = pd.to_datetime(
        values,
        format="mixed",
        utc=True,
        errors="coerce",
    )
    return parsed.dt.tz_convert(TAIPEI_TZ)


def to_taipei_frame(rows: list[dict], value_column: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["time"] = parse_taipei_times(frame["time"])
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")
    return frame.dropna(subset=["time", value_column]).sort_values("time")


def render_viewer_chart(rows: list[dict]):
    frame = to_taipei_frame(rows, "viewer_count")
    if frame.empty:
        st.info(tr("No viewer samples in this session.", "此場次沒有觀眾人數採樣。"))
        return

    chart = (
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                "time:T",
                title=tr("Taiwan time (Asia/Taipei)", "台灣時間（Asia/Taipei）"),
                axis=alt.Axis(format="%m-%d %H:%M"),
            ),
            y=alt.Y("viewer_count:Q", title=tr("Observed viewers", "觀察到的觀眾數")),
            tooltip=[
                alt.Tooltip("time:T", title=tr("Taiwan time", "台灣時間"), format="%Y-%m-%d %H:%M:%S"),
                alt.Tooltip("viewer_count:Q", title=tr("Viewers", "觀眾數")),
            ],
        )
        .properties(height=340)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)


def render_activity_chart(rows: list[dict]):
    frame = pd.DataFrame(rows)
    if frame.empty:
        return
    frame["time"] = parse_taipei_times(frame["time"])
    frame = frame.dropna(subset=["time"])
    frame = (
        frame.assign(count=1)
        .set_index("time")
        .groupby("event_type")["count"]
        .resample("1min")
        .sum()
        .reset_index()
    )
    chart = (
        alt.Chart(frame)
        .mark_line(point=False)
        .encode(
            x=alt.X(
                "time:T",
                title=tr("Taiwan time (Asia/Taipei)", "台灣時間（Asia/Taipei）"),
                axis=alt.Axis(format="%m-%d %H:%M"),
            ),
            y=alt.Y("count:Q", title=tr("Events per minute", "每分鐘事件數")),
            color=alt.Color("event_type:N", title=tr("Event type", "事件類型")),
            tooltip=[
                alt.Tooltip("time:T", title=tr("Taiwan time", "台灣時間"), format="%Y-%m-%d %H:%M"),
                alt.Tooltip("event_type:N", title=tr("Event type", "事件類型")),
                alt.Tooltip("count:Q", title=tr("Events", "事件數")),
            ],
        )
        .properties(height=280)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)



def render_compare_tab(session_paths: list[Path]):
    frame = compare_sessions(session_paths)
    if frame.empty:
        st.info(tr("No sessions selected for comparison.", "尚未選取要比較的場次。"))
        return
    columns = [
        "session_id", "status", "first_event_local", "last_event_local",
        "observed_duration_seconds", "peak_viewers", "average_viewers",
        "chat_messages", "unique_chatters", "likes_observed",
        "likes_current_total", "total_diamonds", "members", "follows",
        "shares", "subscribes",
    ]
    st.dataframe(
        frame[[column for column in columns if column in frame]],
        use_container_width=True,
        hide_index=True,
    )
    metric_options = {
        "peak_viewers": tr("Peak viewers", "最高同時觀看人數"),
        "average_viewers": tr("Average viewers", "平均同時觀看人數"),
        "likes_observed": tr("Observed likes", "觀察到的按讚"),
        "likes_current_total": tr("Current likes", "目前總按讚數"),
        "total_diamonds": "Diamonds",
        "members": tr("Members", "進場事件"),
        "follows": tr("Follows", "追蹤"),
        "shares": tr("Shares", "分享"),
        "subscribes": tr("Subscribes", "訂閱"),
        "join_rate_per_min": tr("Join rate / min", "每分鐘進場數"),
        "viewer_growth": tr("Viewer growth", "觀眾變化"),
        "viewer_volatility": tr("Viewer volatility", "觀眾波動"),
    }
    metric = st.selectbox(
        tr("Comparison metric", "比較指標"),
        options=list(metric_options),
        format_func=metric_options.__getitem__,
        key="comparison_metric_id",
    )
    label = metric_options[metric]
    column = metric
    chart = alt.Chart(frame).mark_bar().encode(
        x=alt.X("session_id:N", sort="-y", title=tr("Session", "場次")),
        y=alt.Y(f"{column}:Q", title=label),
        tooltip=[
            alt.Tooltip("session_id:N", title=tr("Session", "場次")),
            alt.Tooltip(f"{column}:Q", title=label),
        ],
    ).properties(height=320)
    st.altair_chart(chart, use_container_width=True)


def render_gift_tab(session_paths: list[Path]):
    board = gift_leaderboard(session_paths)
    if board.empty:
        st.info(tr("No counted gift events in the selected sessions.", "所選場次沒有已完成的送禮事件。"))
        return
    concentration = gift_concentration(session_paths)
    cols = st.columns(6)
    cols[0].metric(tr("Captured diamonds", "紀錄到的 Diamonds"), int(concentration["total_diamonds"]))
    cols[1].metric(tr("Unique gifters", "送禮者人數"), concentration["gifter_count"])
    cols[2].metric(tr("Gift events", "禮物事件"), concentration["gift_events"])
    cols[3].metric(tr("Top 1 share", "前 1 名占比"), f"{(concentration['top1_share'] or 0) * 100:.1f}%")
    cols[4].metric(tr("Top 5 share", "前 5 名占比"), f"{(concentration['top5_share'] or 0) * 100:.1f}%")
    cols[5].metric(tr("Top 10 share", "前 10 名占比"), f"{(concentration['top10_share'] or 0) * 100:.1f}%")
    if concentration["peak_minute"]:
        peak_diamonds = int(concentration["peak_minute_diamonds"])
        st.caption(
            tr(
                f"Gift peak: {concentration['peak_minute']} | {peak_diamonds} diamonds in one minute",
                f"送禮高峰：{concentration['peak_minute']}｜單分鐘 {peak_diamonds} Diamonds",
            )
        )
    st.dataframe(board, use_container_width=True, hide_index=True)
    chart = alt.Chart(board.head(15)).mark_bar().encode(
        x=alt.X("diamonds:Q", title="Diamonds"),
        y=alt.Y("gifter:N", sort="-x", title=tr("Gifter", "送禮者")),
        color=alt.Color("send_pattern:N", title=tr("Send pattern", "送禮方式")),
        tooltip=[
            alt.Tooltip("gifter:N", title=tr("Gifter", "送禮者")),
            alt.Tooltip("diamonds:Q", title="Diamonds"),
            alt.Tooltip("items:Q", title=tr("Items", "禮物數量")),
            alt.Tooltip("gift_events:Q", title=tr("Gift events", "禮物事件")),
            alt.Tooltip("max_repeat_count:Q", title=tr("Max repeat count", "最高連續數量")),
            alt.Tooltip("send_pattern:N", title=tr("Send pattern", "送禮方式")),
        ],
    ).properties(height=max(260, min(560, 24 * len(board.head(15)))))
    st.altair_chart(chart, use_container_width=True)
    activity = gift_activity_by_minute(session_paths)
    if not activity.empty:
        st.subheader(tr("Gift activity by minute", "每分鐘送禮活動"))
        chart = alt.Chart(activity).mark_bar().encode(
            x=alt.X("time:T", title=tr("Taiwan time (Asia/Taipei)", "台灣時間（Asia/Taipei）"), axis=alt.Axis(format="%m-%d %H:%M")),
            y=alt.Y("diamonds:Q", title=tr("Captured diamonds", "紀錄到的 Diamonds")),
            tooltip=[
                alt.Tooltip("time:T", title=tr("Taiwan time", "台灣時間"), format="%Y-%m-%d %H:%M"),
                alt.Tooltip("diamonds:Q", title="Diamonds"),
                alt.Tooltip("gift_events:Q", title=tr("Gift events", "禮物事件")),
                alt.Tooltip("chat_messages:Q", title=tr("Chat messages", "聊天訊息")),
                alt.Tooltip("viewer_count:Q", title=tr("Average viewers", "平均觀眾數")),
            ],
        ).properties(height=300).interactive()
        st.altair_chart(chart, use_container_width=True)
        relation = activity[(activity["diamonds"] > 0) | (activity["chat_messages"] > 0)].copy()
        if not relation.empty:
            st.subheader(tr("Gift / chat / viewer relation", "送禮／聊天／觀眾關聯"))
            st.dataframe(relation.sort_values("diamonds", ascending=False).head(100), use_container_width=True, hide_index=True)

    details = gift_detail(session_paths)
    if not details.empty:
        st.subheader(tr("Gift transactions", "送禮明細"))
        st.dataframe(
            details.sort_values("diamonds", ascending=False).head(100),
            use_container_width=True,
            hide_index=True,
        )


def render_traffic_social_tab(session_paths: list[Path]):
    st.subheader(tr("Audience flow", "觀眾人流"))
    audience = pd.DataFrame([audience_metrics(path) for path in session_paths])
    if audience.empty:
        st.info(tr("No audience samples in the selected sessions.", "所選場次沒有觀眾採樣。"))
    else:
        audience_columns = [
            "session_id", "join_rate_per_min", "viewer_growth", "viewer_volatility",
            "early_avg_viewers", "mid_avg_viewers", "late_avg_viewers",
            "early_joins", "mid_joins", "late_joins",
        ]
        st.dataframe(
            audience[[column for column in audience_columns if column in audience]],
            use_container_width=True,
            hide_index=True,
        )

    traffic = traffic_sources(session_paths)
    st.subheader(tr("Entry source", "進場來源"))
    if traffic.empty:
        st.info(tr("No member entry-source events in the selected sessions.", "所選場次沒有進場來源事件。"))
    else:
        source_counts = (
            traffic.groupby("entry_source", dropna=False)
            .size()
            .reset_index(name="joins")
            .sort_values("joins", ascending=False)
        )
        st.dataframe(source_counts, use_container_width=True, hide_index=True)
        chart = alt.Chart(source_counts.head(20)).mark_bar().encode(
            x=alt.X("joins:Q", title=tr("Join events", "進場事件")),
            y=alt.Y("entry_source:N", sort="-x", title=tr("Entry source", "進場來源")),
            tooltip=["entry_source", "joins"],
        ).properties(height=max(260, min(560, 24 * len(source_counts.head(20)))))
        st.altair_chart(chart, use_container_width=True)

    st.subheader(tr("Follow / Share / Subscribe", "追蹤／分享／訂閱"))
    social = social_summary(session_paths)
    if social.empty:
        st.info(tr("No follow, share, or subscribe events in the selected sessions.", "所選場次沒有追蹤、分享或訂閱事件。"))
    else:
        social_counts = (
            social.groupby("action", dropna=False)
            .agg(events=("action", "size"), unique_users=("user", "nunique"))
            .reset_index()
            .sort_values("events", ascending=False)
        )
        st.dataframe(social_counts, use_container_width=True, hide_index=True)
        chart = alt.Chart(social_counts).mark_bar().encode(
            x=alt.X("action:N", title=tr("Action", "互動類型")),
            y=alt.Y("events:Q", title=tr("Events", "事件數")),
            tooltip=["action", "events", "unique_users"],
        ).properties(height=280)
        st.altair_chart(chart, use_container_width=True)


    proxies = social_conversion_proxies(session_paths)
    if not proxies.empty:
        st.subheader(tr("Social rates (observed proxies)", "社群互動率（觀察值）"))
        st.caption(tr("Per-100-join values compare captured events only; they are not causal or unique-viewer conversion rates.", "每百次進場的數值只比較捕獲到的事件，不代表因果關係或不重複觀眾轉換率。"))
        st.dataframe(proxies, use_container_width=True, hide_index=True)

    activity = social_activity_by_minute(session_paths)
    if not activity.empty:
        st.subheader(tr("Social activity over time", "社群互動趨勢"))
        melted = activity.melt(
            id_vars=["session_id", "time"],
            value_vars=["follows", "shares", "subscribes"],
            var_name="action", value_name="events",
        )
        chart = alt.Chart(melted).mark_line(point=True).encode(
            x=alt.X("time:T", title=tr("Taiwan time (Asia/Taipei)", "台灣時間（Asia/Taipei）"), axis=alt.Axis(format="%m-%d %H:%M")),
            y=alt.Y("events:Q", title=tr("Events per minute", "每分鐘事件數")),
            color=alt.Color("action:N", title=tr("Action", "互動類型")),
            strokeDash=alt.StrokeDash("session_id:N", title=tr("Session", "場次")),
            tooltip=[
                alt.Tooltip("time:T", title=tr("Taiwan time", "台灣時間"), format="%Y-%m-%d %H:%M"),
                alt.Tooltip("session_id:N", title=tr("Session", "場次")),
                alt.Tooltip("action:N", title=tr("Action", "互動類型")),
                alt.Tooltip("events:Q", title=tr("Events", "事件數")),
            ],
        ).properties(height=320).interactive()
        st.altair_chart(chart, use_container_width=True)


def render_snapshots_tab(session_paths: list[Path]):
    st.subheader(tr("Room snapshots", "直播間快照"))
    snapshots = snapshot_history(session_paths)
    if snapshots.empty:
        st.info(tr("No snapshots yet. New Collector sessions capture optional room snapshots every five minutes by default; rate limits pause snapshot requests.", "目前沒有快照。新的 Collector session 預設每五分鐘擷取一次選用快照；遇到速率限制時會暫停快照請求。"))
    else:
        valid = snapshots[snapshots["error"].isna()].copy()
        if not valid.empty:
            latest = valid.iloc[-1]
            cols = st.columns(5)
            cols[0].metric("LIVE", tr("Yes", "是") if latest.get("live") else tr("No", "否"))
            cols[1].metric(tr("Snapshot viewers", "快照觀眾數"), int(latest["viewer_count"]) if pd.notna(latest.get("viewer_count")) else "N/A")
            cols[2].metric(tr("Entry count", "進場人數"), int(latest["enter_count"]) if pd.notna(latest.get("enter_count")) else "N/A")
            cols[3].metric(tr("Snapshot likes", "快照按讚數"), int(latest["like_count"]) if pd.notna(latest.get("like_count")) else tr("API key required", "需要 API Key"))
            cols[4].metric(tr("Snapshot shares", "快照分享數"), int(latest["share_count"]) if pd.notna(latest.get("share_count")) else tr("API key required", "需要 API Key"))
            plot = valid.melt(
                id_vars=["time", "source"], value_vars=["viewer_count", "like_count", "share_count"],
                var_name="metric", value_name="value",
            ).dropna(subset=["value"])
            if not plot.empty:
                chart = alt.Chart(plot).mark_line(point=True).encode(
                    x=alt.X("time:T", title=tr("Taiwan time (Asia/Taipei)", "台灣時間（Asia/Taipei）"), axis=alt.Axis(format="%m-%d %H:%M")),
                    y=alt.Y("value:Q", title=tr("Snapshot value", "快照數值")),
                    color=alt.Color("metric:N", title=tr("Metric", "指標")),
                    strokeDash=alt.StrokeDash("source:N", title=tr("Source", "來源")),
                    tooltip=[alt.Tooltip("time:T", format="%Y-%m-%d %H:%M:%S"), "source", "metric", "value"],
                ).properties(height=320).interactive()
                st.altair_chart(chart, use_container_width=True)
        st.dataframe(snapshots.sort_values("time", ascending=False).head(200), use_container_width=True, hide_index=True)

    st.subheader(tr("Dynamic gifter rankings", "動態送禮排行榜"))
    rankings = ranking_history(session_paths)
    if rankings.empty:
        st.info(tr("Ranking snapshots require TIKTOOL_API_KEY. Full audience rankings may also require TIKTOK_COOKIE_HEADER.", "排行榜快照需要 TIKTOOL_API_KEY；完整觀眾排行可能也需要 TIKTOK_COOKIE_HEADER。"))
    else:
        latest_time = rankings["time"].max()
        latest = rankings[rankings["time"] == latest_time].sort_values(["board", "rank"])
        st.caption(f"{tr('Latest ranking snapshot', '最新排行快照')}：{latest_time}")
        st.dataframe(latest, use_container_width=True, hide_index=True)
        chart = alt.Chart(rankings.dropna(subset=["score"])).mark_line(point=True).encode(
            x=alt.X("time:T", title=tr("Taiwan time (Asia/Taipei)", "台灣時間（Asia/Taipei）")),
            y=alt.Y("score:Q", title=tr("Ranking score / diamonds", "排行分數／Diamonds")),
            color=alt.Color("user:N", title=tr("User", "使用者")),
            tooltip=["time:T", "board", "rank", "user", "score"],
        ).properties(height=340).interactive()
        st.altair_chart(chart, use_container_width=True)

    catalogs = [path / "gift_catalog.json" for path in session_paths if (path / "gift_catalog.json").exists()]
    if catalogs:
        catalog = read_json(catalogs[-1], {})
        gifts = (catalog.get("data") or {}).get("gifts") or []
        if gifts:
            st.subheader(tr("Gift catalog", "禮物目錄"))
            st.dataframe(pd.DataFrame(gifts), use_container_width=True, hide_index=True)


def render_health_tab(session_paths: list[Path]):
    reports = [build_health_report(path) for path in session_paths]
    if not reports:
        st.info(tr("No sessions selected for health monitoring.", "尚未選取要檢查健康狀態的場次。"))
        return
    frame = pd.DataFrame(reports)
    st.caption(tr("Connection coverage is the share of the Collector monitoring period when the WebSocket was connected; it is not event completeness. A no-events warning means nothing was captured in the last five minutes and may be a false alarm during quiet streams.", "連線涵蓋率是 Collector 監控期間內 WebSocket 已連線時間比例，不代表整場事件完整率。停寫警示表示最近五分鐘沒有捕獲事件；低流量直播可能誤報。"))
    stalled = [item for item in reports if item.get('event_stalled')]
    if stalled:
        st.warning(f"{tr('No-events warning', '停寫警示')}：" + ", ".join(str(item['session_id']) for item in stalled))
    columns = [
        "session_id", "status", "connection_count", "reconnect_count",
        "disconnect_count", "event_count", "raw_event_count",
        "socket_uptime_ratio", "socket_gap_seconds",
        "observed_connection_coverage", "last_event_age_seconds", "event_stalled",
        "duplicate_events_dropped", "sdk_error_events",
        "unknown_event_count", "offline_confirmed", "live_end_detected",
    ]
    st.dataframe(
        frame[[column for column in columns if column in frame]],
        use_container_width=True,
        hide_index=True,
    )
    st.json({"selected_sessions": reports})


def add_streamer_to_watchlist(raw_username: str) -> str:
    username = raw_username.strip().lstrip("@").strip()
    if not username:
        return tr("Enter a streamer username first.", "請先輸入直播主帳號。")

    config = read_json(CONFIG_PATH, {
        "poll_seconds": 60,
        "probe_timeout_seconds": 15,
        "offline_confirmations": 3,
        "collector_script": "src/collector.py",
        "output_root": "data/raw",
        "streamers": [],
    })
    streamers = config.setdefault("streamers", [])
    found = None
    for item in streamers:
        if str(item.get("username", "")).lstrip("@").strip() == username:
            found = item
            break

    if found is None:
        streamers.append({
            "username": username,
            "label": username,
            "enabled": True,
        })
        action = "added"
    else:
        found["username"] = username
        found["label"] = found.get("label") or username
        found["enabled"] = True
        action = "enabled"

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)
    if action == "added":
        return tr(f"@{username} added to watchlist. Watcher will probe it on the next cycle.", f"已將 @{username} 加入追蹤清單；Watcher 將在下次輪詢時檢查。")
    return tr(f"@{username} enabled in watchlist. Watcher will probe it on the next cycle.", f"已啟用 @{username}；Watcher 將在下次輪詢時檢查。")


def remove_streamer_from_watchlist(raw_username: str) -> str:
    username = raw_username.strip().lstrip("@").strip()
    if not username:
        return tr("Select a streamer first.", "請先選擇直播主。")

    config = read_json(CONFIG_PATH, {"streamers": []})
    streamers = config.setdefault("streamers", [])
    before = len(streamers)
    config["streamers"] = [
        item for item in streamers
        if str(item.get("username", "")).lstrip("@").strip() != username
    ]
    if len(config["streamers"]) == before:
        return tr(f"@{username} is not in the watchlist.", f"追蹤清單中沒有 @{username}。")

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)
    return tr(f"@{username} removed from watchlist. Captured data was kept.", f"已從追蹤清單移除 @{username}；既有收集資料已保留。")



def latest_live_session(username: str | None) -> Path | None:
    if not username:
        return None
    candidates = []
    for path in session_dirs(username):
        meta = session_metadata(path)
        if meta.get("status") == "running":
            if _is_browser_session(path):
                try:
                    snapshot_fresh = time.time() - (path / "session.json").stat().st_mtime <= 30
                except OSError:
                    snapshot_fresh = False
                if not snapshot_fresh and not _browser_capture_is_active(path):
                    continue
            candidates.append((str(meta.get("collector_started_at_utc") or path.name), path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def render_current_connection_status(username: str, watcher_state: dict) -> None:
    """Show current probe and Collector state without treating history as live state."""
    last_probe = pd.to_datetime(
        watcher_state.get("last_probe_at_utc"), utc=True, errors="coerce"
    )
    last_confirmed = pd.to_datetime(
        watcher_state.get("last_confirmed_live_at_utc"), utc=True, errors="coerce"
    )
    error = str(watcher_state.get("last_probe_error") or "")
    config = read_json(CONFIG_PATH, {})
    poll_seconds = config.get("poll_seconds", 60)
    try:
        stale_after = max(180, float(poll_seconds) * 3)
    except (TypeError, ValueError):
        stale_after = 180
    try:
        offline_confirmations = max(1, int(config.get("offline_confirmations", 3)))
    except (TypeError, ValueError):
        offline_confirmations = 3

    if pd.isna(last_probe):
        probe_label = tr("No probe yet", "尚無探測紀錄")
    elif (pd.Timestamp.now(tz="UTC") - last_probe).total_seconds() > stale_after:
        probe_label = tr("Probe stale", "探測逾時")
    elif "is not currently live" in error.casefold():
        misses = int(watcher_state.get("consecutive_misses") or 0)
        probe_label = (
            tr("Not LIVE", "未開播")
            if misses >= offline_confirmations
            else tr(
                f"Confirming not LIVE ({misses}/{offline_confirmations})",
                f"確認未開播中（{misses}/{offline_confirmations}）",
            )
        )
    elif error:
        probe_label = tr("Probe error", "探測錯誤")
    elif pd.notna(last_confirmed) and last_confirmed == last_probe:
        probe_label = tr("LIVE confirmed", "已確認直播中")
    else:
        probe_label = tr("Waiting for LIVE result", "等待開播探測結果")

    collector = latest_live_session(username)
    if collector:
        collector_metadata = session_metadata(collector)
        if collector_metadata.get("provider") == "browser_network":
            live_state = browser_live_state(collector)
            socket_state = str(live_state.get("connection_state") or "starting")
        else:
            live_state = read_json(collector / "live_state.json", {})
            socket_state = str(live_state.get("connection_state") or "starting")
            if socket_state == "disconnected" and live_state.get("current_gap_started_at_ms") is not None:
                socket_state = "reconnecting"
        socket_labels = {
            "connected": tr("Connected", "WebSocket 已連線"),
            "disconnected": tr("Disconnected", "WebSocket 已中斷"),
            "reconnecting": tr("Reconnecting", "重連中"),
            "offline": tr("Not LIVE", "未開播"),
            "not_live": tr("Not LIVE", "未開播"),
            "starting": tr("Connecting", "連線中"),
            "session_ended": tr("Session ended", "場次已結束"),
            "error": tr("Error", "錯誤"),
            "unknown": tr("Waiting for signal", "等待連線訊號"),
        }
        collector_label = socket_labels.get(socket_state, socket_state)
        if collector_metadata.get("provider") == "browser_network":
            collector_label = f"Browser CDP · {collector_label}"
    else:
        active_benchmark = next(
            (
                path for path in active_benchmark_session_dirs()
                if str(session_metadata(path).get("username") or "").lstrip("@") == username
            ),
            None,
        )
        active_browser = next(
            (
                path for path in active_browser_session_dirs()
                if str(session_metadata(path).get("username") or "").lstrip("@").casefold() == username.casefold()
            ),
            None,
        )
        collector_label = (
            tr("Browser capture; see Dashboard", "Browser 正在擷取；請查看儀表板")
            if active_browser else (
            tr("Active capture; see Live Sessions", "場次收集中；請到 Live Sessions 查看連線訊號")
            if active_benchmark else tr("Not connected", "Collector 未連線")
            )
        )

    probe_col, collector_col = st.columns(2)
    probe_col.metric(tr("Current LIVE probe", "目前開播探測"), probe_label)
    collector_col.metric(tr("Collector WebSocket", "Collector WebSocket"), collector_label)
    if pd.notna(last_probe):
        checked_label = last_probe.tz_convert(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
        st.caption(f"{tr('Last probe (Taiwan time)', '最近探測（台灣時間）')}：{checked_label}")
    elif not watcher_state:
        st.caption(
            tr(
                "The Watcher has no probe record yet. Start the Watcher to check whether this account is LIVE now.",
                "Watcher 尚無探測紀錄。啟動 Watcher 後，才能確認帳號目前是否開播。",
            )
        )
    if error and "is not currently live" not in error.casefold():
        st.caption(f"{tr('Probe detail', '探測訊息')}：{error[:160]}")


def render_live_tracking(session_dir: Path | None, metrics: dict, streamer_state: dict):
    if session_dir is None:
        st.info(tr("No active tracking session.", "目前沒有進行中的追蹤場次。"))
        return
    session = metrics.get("session") or {}
    st.caption(
        f"{tr('Live room', '直播間')}：{session.get('room_id') or 'unknown'} | "
        f"{tr('fragments', '片段')}={metrics.get('fragment_count', 1)} | "
        f"{tr('status', '狀態')}={session.get('status', 'unknown')} | "
        f"{tr('last event', '最近事件')}={session.get('last_received_at_local') or tr('waiting', '等待中')}"
    )
    cols = st.columns(6)
    is_browser = session.get("provider") == "browser_network"
    cols[0].metric(
        tr("Capture source", "\u64f7\u53d6\u4f86\u6e90") if is_browser else tr("Watch status", "\u89c0\u6e2c\u72c0\u614b"),
        "Browser Network" if is_browser else streamer_state.get("status", "unknown"),
    )
    cols[1].metric(
        tr("Latest viewer sample", "\u6700\u65b0\u89c0\u773e\u6578\u6a23\u672c"),
        metrics["viewers"][-1] if metrics["viewers"] else "N/A",
        help=tr(
            "Most recent viewer sample received by the collector.",
            "\u6536\u64da\u7aef\u6700\u8fd1\u6536\u5230\u7684\u89c0\u773e\u6578\u6a23\u672c\u3002",
        ) if is_browser else None,
    )
    cols[2].metric(tr("Current likes", "目前按讚數"), metrics["like_current_total"] if metrics["like_current_total"] is not None else metrics["likes"])
    cols[3].metric(tr("Captured diamonds (room)", "直播間紀錄 Diamonds"), int(metrics["diamonds"]))
    cols[4].metric(tr("Chat", "聊天"), metrics["chat"])
    cols[5].metric(tr("Gift events (room)", "直播間禮物事件"), metrics["gifts"])
    if metrics["recent_activity"]:
        st.dataframe(metrics["recent_activity"], use_container_width=True, hide_index=True)
    else:
        st.info(tr("Waiting for live events...", "等待即時事件..."))

def render_provider_benchmark():
    import src.provider_benchmark_report as benchmark_report_module

    benchmark_report_module = importlib.reload(benchmark_report_module)
    analyze_session = benchmark_report_module.analyze_session

    st.title(tr("Live Sessions", "直播場次"))
    st.caption(
        tr(
            "Read-only session dashboard for active and completed LIVE captures. It refreshes every five seconds and shows connection signals recorded by the selected capture session.",
            "\u6b64\u9801\u986f\u793a\u9032\u884c\u4e2d\u548c\u5df2\u5b8c\u6210\u7684 LIVE \u64f7\u53d6\u5834\u6b21\uff0c\u6bcf\u4e94\u79d2\u66f4\u65b0\uff0c\u9023\u7dda\u72c0\u614b\u4f9d\u6240\u9078\u64f7\u53d6\u5834\u6b21\u8a18\u9304\u7684\u8a0a\u865f\u986f\u793a\u3002",
        )
    )
    paths = benchmark_session_dirs()
    if not paths:
        st.info(tr("No LIVE session with recorded events was found.", "找不到含有事件紀錄的直播場次。"))
        return

    labels = [f"{path.parent.name} · {path.name}" for path in paths]
    selected_label = st.selectbox(
        tr("LIVE session", "直播場次"),
        options=labels,
        key="provider_benchmark_session",
    )
    session_dir = paths[labels.index(selected_label)]
    report = analyze_session(
        session_dir,
        integration="TikTokLive Python client + Euler signing service",
        minimum_hours=2.0,
    )

    st.caption(
        f"@{report.get('username') or 'unknown'} · {report.get('provider')} "
        f"{(report.get('package_versions') or {}).get('TikTokLive') or ''} · "
        f"{report['duration_seconds'] / 60:.1f} {tr('minutes elapsed', '分鐘')}"
    )
    connection_labels = {
        "connected": tr("LIVE", "直播中"),
        "not_live": tr("Not LIVE", "未開播"),
        "reconnecting": tr("Reconnecting", "重連中"),
        "disconnected": tr("Disconnected", "已中斷"),
        "live_ended": tr("LIVE ended", "直播已結束"),
        "session_ended": tr("Session ended", "場次已結束"),
        "error": tr("Error", "錯誤"),
        "stale": tr("Signal stale", "狀態訊號逾時"),
        "unknown": tr("Waiting for signal", "等待連線訊號"),
    }
    metric_cols = st.columns(6)
    metric_cols[0].metric(
        tr("Collector WebSocket", "Collector WebSocket"),
        connection_labels.get(report.get("connection_state"), tr("Unknown", "未知")),
        help=tr(
            "LIVE means the selected capture last recorded a connected socket. Not LIVE requires an explicit LIVE end event. Other states describe reconnects, stale signals, or errors.",
            "直播中表示所選 Collector 最後記錄到 WebSocket 已連線；未開播須有明確的直播結束事件。其他狀態表示重連、訊號逾時或錯誤；這與 Watcher 探測分開。",
        ),
    )
    metric_cols[1].metric(tr("Viewer samples", "觀眾採樣"), report["viewer_sample_count"])
    metric_cols[2].metric(tr("Chat messages", "聊天訊息"), report["event_counts"].get("chat", 0))
    metric_cols[3].metric(tr("Gift events", "禮物事件"), report["gift_count"])
    metric_cols[4].metric(tr("Diamonds observed", "紀錄到的 Diamonds"), report["gift_diamonds_observed"])
    metric_cols[5].metric(
        tr("Connections / disconnects", "連線／中斷"),
        f"{report.get('connection_count') or 0} / {report.get('disconnect_count') or 0}",
    )
    latest_event = pd.to_datetime(report.get("last_event_at_utc"), utc=True, errors="coerce")
    latest_signal = pd.to_datetime(report.get("last_connection_signal_utc"), utc=True, errors="coerce")
    now_utc = pd.Timestamp.now(tz="UTC")
    last_event_age = (
        max(0.0, (now_utc - latest_event).total_seconds())
        if pd.notna(latest_event) else None
    )
    signal_time = (
        latest_signal.tz_convert(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
        if pd.notna(latest_signal) else tr("not recorded", "尚無紀錄")
    )
    last_event_label = (
        f"{last_event_age:.0f} s {tr('ago', '前')}"
        if last_event_age is not None else tr("not recorded", "尚無紀錄")
    )
    st.caption(
        f"{tr('Last WebSocket state signal (Taiwan time)', '最近 WebSocket 狀態訊號（台灣時間）')}：{signal_time} · "
        f"{tr('Last captured event', '最近捕獲事件')}：{last_event_label}"
    )

    interval = report["viewer_interval_seconds"]
    st.caption(
        f"{tr('Viewer sample interval', '觀眾採樣間隔')}："
        f"{tr('median', '中位數')} {interval['median']}s · {tr('max', '最大')} {interval['max']}s · "
        f"{tr('Chat user_id coverage', '聊天 user_id 覆蓋率')} {report['chat_user_id_coverage']} · "
        f"{tr('Gift user_id coverage', '禮物 user_id 覆蓋率')} {report['gift_user_id_coverage']}"
    )

    panel_names = [
        "Viewer trend",
        "Recent chat",
        "Recent gift chart",
        "Gift trend",
        "Recent gifts",
        "Diamonds by sender",
    ]
    panel_localized = {
        "Viewer trend": tr("Viewer trend", "觀眾趨勢"),
        "Recent chat": tr("Recent chat", "最新聊天"),
        "Recent gift chart": tr("Recent gift chart", "最新禮物圖表"),
        "Gift trend": tr("Gift trend", "禮物趨勢"),
        "Recent gifts": tr("Recent gifts", "最新禮物"),
        "Diamonds by sender": tr("Diamonds by sender", "依送禮者統計 Diamonds"),
    }
    control_cols = st.columns(4)
    panel_layout = control_cols[0].radio(
        tr("Live panel layout", "即時面板版面"),
        ["Multi-panel", "Single panel"],
        horizontal=True,
        format_func=lambda value: tr(value, "多面板" if value == "Multi-panel" else "單一面板"),
        key="benchmark_panel_layout",
    )
    latest_limit = int(
        control_cols[1].number_input(
            tr("Latest chat/gift limit", "最新聊天／禮物顯示筆數"),
            min_value=1,
            max_value=100,
            value=12,
            step=1,
            key="benchmark_recent_limit",
        )
    )
    show_chat_list = control_cols[2].toggle(
        tr("Show recent chat list", "顯示最新聊天清單"), value=True, key="benchmark_show_chat_list"
    )
    show_gift_list = control_cols[3].toggle(
        tr("Show recent gift list", "顯示最新禮物清單"), value=True, key="benchmark_show_gift_list"
    )
    if panel_layout == "Multi-panel":
        selected_panels = st.multiselect(
            tr("Panels", "面板"),
            panel_names,
            default=[
                "Viewer trend",
                "Recent chat",
                "Recent gift chart",
                "Gift trend",
                "Recent gifts",
            ],
            format_func=panel_localized.__getitem__,
            key="benchmark_selected_panels",
        )
    else:
        selected_panels = [
            st.selectbox(
                tr("Panel", "面板"),
                panel_names,
                format_func=panel_localized.__getitem__,
                key="benchmark_single_panel",
            )
        ]

    viewer_rows = []
    recent_chat = deque(maxlen=latest_limit)
    recent_gifts = deque(maxlen=latest_limit)
    gift_activity_rows = []
    diamonds_by_sender = defaultdict(lambda: {
        "user_id": None,
        "unique_id": None,
        "counted_gift_events": 0,
        "diamonds": 0.0,
    })
    try:
        with (session_dir / "events.ndjson").open(
            "r", encoding="utf-8", errors="replace"
        ) as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "chat":
                    recent_chat.append(event)
                elif event_type == "gift" and event.get("counted"):
                    user_id = event.get("user_id")
                    unique_id = event.get("unique_id") or event.get("nickname")
                    sender_key = (
                        str(user_id)
                        if user_id is not None
                        else f"username:{unique_id or 'unknown'}"
                    )
                    sender = diamonds_by_sender[sender_key]
                    sender["user_id"] = str(user_id) if user_id is not None else ""
                    sender["unique_id"] = unique_id or "unknown"
                    sender["counted_gift_events"] += 1
                    try:
                        sender["diamonds"] += float(event.get("diamond_total") or 0)
                    except (TypeError, ValueError):
                        pass
                    timestamp_ms = event.get("received_at_ms")
                    try:
                        time_label = pd.to_datetime(
                            timestamp_ms, unit="ms", utc=True
                        ).tz_convert(TAIPEI_TZ).strftime("%H:%M:%S")
                    except (TypeError, ValueError, OverflowError):
                        time_label = ""
                    recent_gifts.append({
                        "time": time_label,
                        "unique_id": unique_id or "unknown",
                        "user_id": str(user_id) if user_id is not None else "",
                        "gift": event.get("gift_name") or "gift",
                        "repeat_count": event.get("repeat_count") or 1,
                        "diamonds": event.get("diamond_total"),
                    })
                    gift_activity_rows.append({
                        "received_at": timestamp_ms,
                        "unique_id": unique_id or "unknown",
                        "gift": event.get("gift_name") or "gift",
                        "repeat_count": event.get("repeat_count") or 1,
                        "diamonds": event.get("diamond_total") or 0,
                    })
                if event_type not in {"viewer", "roomUserSeq"}:
                    continue
                viewer_count = event.get("viewer_count", event.get("viewerCount"))
                timestamp_ms = event.get("received_at_ms", event.get("timestamp_ms"))
                if isinstance(viewer_count, (int, float)) and isinstance(
                    timestamp_ms, (int, float)
                ):
                    viewer_rows.append({
                        "received_at": timestamp_ms,
                        "viewer_count": viewer_count,
                    })
    except OSError:
        st.warning(tr("The benchmark event file is temporarily unavailable.", "Benchmark 事件檔暫時無法讀取。"))
        return

    def render_viewer_trend():
        st.subheader(tr("Viewer trend", "觀眾趨勢"))
        if not viewer_rows:
            st.info(tr("Waiting for the first Viewer sample.", "等待第一筆觀眾人數採樣。"))
            return
        viewer_frame = pd.DataFrame(viewer_rows)
        viewer_frame["received_at"] = pd.to_datetime(
            viewer_frame["received_at"], unit="ms", utc=True
        ).dt.tz_convert(TAIPEI_TZ)
        chart = (
            alt.Chart(viewer_frame)
            .mark_line(point=True)
            .encode(
                x=alt.X("received_at:T", title=tr("Taiwan time", "台灣時間")),
                y=alt.Y("viewer_count:Q", title=tr("Concurrent viewers", "同時觀看人數"), scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("received_at:T", title=tr("Time", "時間")), alt.Tooltip("viewer_count:Q", title=tr("Viewers", "觀眾數"))],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

    def render_recent_chat():
        st.subheader(f"{tr('Recent chat', '最新聊天')} | {tr('latest', '最近')} {latest_limit}")
        if not show_chat_list:
            st.caption(tr("Recent chat list is hidden.", "最新聊天清單已隱藏。"))
            return
        if not recent_chat:
            st.info(tr("No chat or sticker messages have arrived yet.", "尚未收到聊天或表情貼訊息。"))
            return
        for event in reversed(recent_chat):
            user = event.get("unique_id") or event.get("nickname") or "Viewer"
            user_id = event.get("user_id")
            comment = str(event.get("comment") or "").strip()
            emotes = event.get("emotes")
            emotes = emotes if isinstance(emotes, list) else []
            if not comment:
                comment = tr("Sticker", "表情貼") if event.get("message_kind") == "emote" or emotes else tr("(empty message)", "（空白訊息）")
            received_at = event.get("received_at_ms")
            try:
                time_label = pd.to_datetime(
                    received_at, unit="ms", utc=True
                ).tz_convert(TAIPEI_TZ).strftime("%H:%M:%S")
            except (TypeError, ValueError, OverflowError):
                time_label = ""

            with st.container(border=True):
                identity = f"@{user}"
                if user_id is not None:
                    identity += f" | user_id {user_id}"
                if time_label:
                    identity += f" | {time_label}"
                st.text(identity)
                st.text(comment)
                if emotes:
                    image_columns = st.columns(min(len(emotes), 4))
                    for index, emote in enumerate(emotes[:4]):
                        image_url = trusted_emote_image_url(
                            emote.get("image_url") if isinstance(emote, dict) else None
                        )
                        with image_columns[index]:
                            if image_url:
                                st.image(image_url, width=72)
                            else:
                                st.caption(tr("Sticker", "表情貼"))
                    if len(emotes) > 4:
                        st.caption(f"{len(emotes) - 4} {tr('more stickers', '個表情貼未顯示')}")

    def render_recent_gift_chart():
        st.subheader(f"{tr('Recent gift chart', '最新禮物圖表')} | {tr('latest', '最近')} {latest_limit}")
        chart_rows = [
            row for row in gift_activity_rows
            if isinstance(row.get("received_at"), (int, float))
        ][-latest_limit:]
        if not chart_rows:
            st.info(tr("Waiting for counted gifts with timestamps.", "等待有時間戳記的已完成送禮事件。"))
            return
        gift_frame = pd.DataFrame(chart_rows)
        gift_frame["event_time"] = pd.to_datetime(
            gift_frame["received_at"], unit="ms", utc=True
        ).dt.tz_convert(TAIPEI_TZ)
        gift_frame["diamonds"] = pd.to_numeric(
            gift_frame["diamonds"], errors="coerce"
        ).fillna(0)
        gift_frame["repeat_count"] = pd.to_numeric(
            gift_frame["repeat_count"], errors="coerce"
        ).fillna(1)
        chart = (
            alt.Chart(gift_frame)
            .mark_bar()
            .encode(
                x=alt.X("event_time:T", title=tr("Taiwan time", "台灣時間")),
                y=alt.Y("repeat_count:Q", title=tr("Gift quantity", "禮物數量")),
                color=alt.Color("gift:N", title=tr("Gift", "禮物")),
                tooltip=[
                    alt.Tooltip("event_time:T", title=tr("Time", "時間")),
                    alt.Tooltip("unique_id:N", title=tr("Sender", "送禮者")),
                    alt.Tooltip("gift:N", title=tr("Gift", "禮物")),
                    alt.Tooltip("repeat_count:Q", title=tr("Quantity", "數量")),
                    alt.Tooltip("diamonds:Q", title="Diamonds"),
                ],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

    def render_gift_trend():
        st.subheader(tr("Gift trend", "禮物趨勢"))
        if not gift_activity_rows:
            st.info(tr("Waiting for counted gifts.", "等待已完成的送禮事件。"))
            return
        gift_frame = pd.DataFrame(gift_activity_rows)
        gift_frame["event_time"] = pd.to_datetime(
            gift_frame["received_at"], unit="ms", utc=True, errors="coerce"
        ).dt.tz_convert(TAIPEI_TZ)
        gift_frame = gift_frame.dropna(subset=["event_time"])
        if gift_frame.empty:
            st.info(tr("Waiting for gift timestamps.", "等待禮物時間戳記。"))
            return
        gift_frame["diamonds"] = pd.to_numeric(
            gift_frame["diamonds"], errors="coerce"
        ).fillna(0)
        gift_frame["minute"] = gift_frame["event_time"].dt.floor("min")
        trend = (
            gift_frame.groupby("minute", as_index=False)
            .agg(diamonds=("diamonds", "sum"), gift_events=("gift", "count"))
        )
        metric = st.radio(
            tr("Trend metric", "趨勢指標"),
            ["Diamonds per minute", "Gift events per minute"],
            horizontal=True,
            format_func=lambda value: tr(value, "每分鐘 Diamonds" if value == "Diamonds per minute" else "每分鐘禮物事件"),
            key="benchmark_gift_trend_metric",
        )
        field = "diamonds" if metric == "Diamonds per minute" else "gift_events"
        chart = (
            alt.Chart(trend)
            .mark_line(point=True)
            .encode(
                x=alt.X("minute:T", title=tr("Taiwan time", "台灣時間")),
                y=alt.Y(f"{field}:Q", title=tr(metric, "每分鐘 Diamonds" if metric == "Diamonds per minute" else "每分鐘禮物事件"), scale=alt.Scale(zero=False)),
                tooltip=[
                    alt.Tooltip("minute:T", title=tr("Minute", "分鐘")),
                    alt.Tooltip("diamonds:Q", title="Diamonds"),
                    alt.Tooltip("gift_events:Q", title=tr("Gift events", "禮物事件")),
                ],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

    def render_recent_gifts():
        st.subheader(f"{tr('Recent gifts', '最新禮物')} | {tr('latest', '最近')} {latest_limit}")
        if not show_gift_list:
            st.caption(tr("Recent gift list is hidden.", "最新禮物清單已隱藏。"))
        elif recent_gifts:
            st.dataframe(
                list(reversed(recent_gifts)),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(tr("No counted gifts have arrived yet.", "尚未收到已完成的送禮事件。"))

    def render_diamonds_by_sender():
        st.subheader(tr("Diamonds by sender", "依送禮者統計 Diamonds"))
        if diamonds_by_sender:
            diamond_rows = sorted(
                diamonds_by_sender.values(),
                key=lambda row: row["diamonds"],
                reverse=True,
            )
            st.dataframe(
                pd.DataFrame(diamond_rows).head(20),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(tr("No counted gift diamonds have arrived yet.", "尚未收到可統計的送禮 Diamonds。"))

    panel_renderers = {
        "Viewer trend": render_viewer_trend,
        "Recent chat": render_recent_chat,
        "Recent gift chart": render_recent_gift_chart,
        "Gift trend": render_gift_trend,
        "Recent gifts": render_recent_gifts,
        "Diamonds by sender": render_diamonds_by_sender,
    }
    if not selected_panels:
        st.info(tr("Choose one or more panels to display.", "請選擇要顯示的面板。"))
    elif panel_layout == "Single panel":
        panel_renderers[selected_panels[0]]()
    else:
        for index in range(0, len(selected_panels), 2):
            columns = st.columns(2)
            for offset, column in enumerate(columns):
                panel_index = index + offset
                if panel_index >= len(selected_panels):
                    continue
                panel_name = selected_panels[panel_index]
                with column:
                    with st.container(border=True):
                        panel_renderers[panel_name]()


def render_lightweight_live_view(session_dir: Path, streamer_state: dict) -> bool:
    """Render the bounded live-state snapshot and report whether full history was requested."""
    metadata = session_metadata(session_dir)
    is_browser = metadata.get("provider") == "browser_network"
    live_state = (
        browser_live_state(session_dir)
        if is_browser
        else read_json(session_dir / "live_state.json", {})
    )
    if not live_state and not is_browser:
        st.warning(
            tr(
                "This collector session has no lightweight live state yet. Restart the collector to enable live-state updates, or request a full historical scan below.",
                "這場 Collector 尚未產生輕量即時狀態。重新啟動 Collector 後即可更新；也可以在下方要求完整歷史掃描。",
            )
        )
    else:
        last_event_ms = live_state.get("last_event_at_ms")
        if isinstance(last_event_ms, (int, float)):
            last_event_age = max(
                0.0,
                (pd.Timestamp.now(tz="UTC").timestamp() * 1000 - last_event_ms) / 1000,
            )
            last_event_label = f"{last_event_age:.1f} s"
        else:
            last_event_label = tr("waiting", "等待中")

        st.caption(
            f"@{live_state.get('username') or metadata.get('username') or 'unknown'} | "
            f"{tr('Session', '場次')} {session_dir.name} | "
            f"{tr('Last event age', '最近事件經過')} {last_event_label}"
        )
        socket_state = str(live_state.get("connection_state") or "unknown")
        session_status = str(metadata.get("status") or "")
        if session_status == "error":
            socket_state = "error"
        elif session_status in {"offline_confirmed", "live_end"} or socket_state == "offline":
            socket_state = "not_live"
        elif session_status and session_status != "running":
            socket_state = "session_ended"
        elif socket_state == "disconnected" and live_state.get("current_gap_started_at_ms") is not None:
            socket_state = "reconnecting"
        socket_labels = {
            "connected": tr("LIVE", "直播中"),
            "not_live": tr("Not LIVE", "未開播"),
            "reconnecting": tr("Reconnecting", "重連中"),
            "disconnected": tr("Disconnected", "已中斷"),
            "starting": tr("Connecting", "連線中"),
            "session_ended": tr("Session ended", "場次已結束"),
            "error": tr("Error", "錯誤"),
            "unknown": tr("Waiting for signal", "等待連線訊號"),
        }
        watcher_task_labels = {
            "collecting": tr("Collecting", "收集中"),
            "connecting": tr("Connecting", "連線中"),
            "live_confirmed": tr("LIVE confirmed (probe only)", "已確認直播（僅探測）"),
            "not_live": tr("Not LIVE", "未開播"),
            "probe_error": tr("Probe error", "探測錯誤"),
            "restart_pending": tr("Restart pending", "等待重啟"),
            "waiting": tr("Waiting for probe", "等待探測"),
            "quota_paused": tr("Quota paused", "配額暫停"),
        }
        columns = st.columns(7)
        columns[0].metric(
            tr("Browser LIVE WebSocket", "瀏覽器 LIVE WebSocket") if is_browser else tr("Collector WebSocket", "Collector WebSocket"),
            socket_labels.get(socket_state, tr("Error", "錯誤") if "error" in socket_state.casefold() else socket_state),
            help=tr(
                "Passive connection state observed through Chrome CDP.",
                "透過 Chrome CDP 被動觀察到的連線狀態。",
            ) if is_browser else tr(
                "Connection state reported by the long-running Collector socket.",
                "長時間運作的 Collector WebSocket 回報的連線狀態。",
            ),
        )
        watcher_task = str(streamer_state.get("status") or "unknown")
        if is_browser:
            columns[1].metric(
                tr("Browser capture", "瀏覽器擷取"),
                tr("LIVE ended", "直播已結束") if socket_state == "session_ended" else tr("Observing", "觀測中"),
            )
        else:
            columns[1].metric(
                tr("Watcher task", "Watcher 工作狀態"),
                watcher_task_labels.get(watcher_task, watcher_task),
            )
        viewer_count = live_state.get("viewer_count")
        viewer_at_ms = live_state.get("latest_viewer_at_ms")
        viewer_time_help = tr(
            "Latest WebcastRoomUserSeqMessage count observed through Chrome CDP.",
            "\u900f\u904e Chrome CDP \u89c0\u5bdf\u5230\u7684\u6700\u65b0 WebcastRoomUserSeqMessage \u4eba\u6578\u3002",
        )
        if isinstance(viewer_at_ms, (int, float)):
            viewer_time = pd.to_datetime(viewer_at_ms, unit="ms", utc=True).tz_convert(TAIPEI_TZ)
            viewer_time_help += " " + tr("Sample received", "\u6a23\u672c\u6536\u5230\u6642\u9593") + ": " + viewer_time.strftime("%H:%M:%S")
        columns[2].metric(
            tr("Latest viewer sample", "\u6700\u65b0\u89c0\u773e\u6578\u6a23\u672c"),
            viewer_count if viewer_count is not None else "N/A",
            help=viewer_time_help,
        )
        columns[3].metric(tr("Likes", "按讚"), live_state.get("likes_total") or 0)
        columns[4].metric(tr("Diamonds", "鑽石數"), int(live_state.get("diamonds") or 0))
        columns[5].metric(tr("Chat", "聊天"), live_state.get("chat_count", 0))
        columns[6].metric(tr("Gifts", "禮物"), live_state.get("gift_count", 0))

        if is_browser:
            st.caption(
                f"{tr('Observed LIVE socket', '\u89c0\u5bdf\u5230\u7684 LIVE \u9023\u7dda')}?"
                f"{live_state.get('target_websocket_url') or 'webcast socket'} ? "
                f"{tr('connections', '\u9023\u7dda\u6578')} {live_state.get('target_websocket_connection_count', 0)}"
            )
        else:
            st.caption(
                f"{tr('Collector connection state', '\u64f7\u53d6\u9023\u7dda\u72c0\u614b')}?{socket_labels.get(socket_state, socket_state)}"
            )

        gap_started_at_ms = live_state.get("current_gap_started_at_ms")
        current_gap = (
            max(0.0, (pd.Timestamp.now(tz="UTC").timestamp() * 1000 - gap_started_at_ms) / 1000)
            if isinstance(gap_started_at_ms, (int, float))
            else None
        )
        connection_columns = st.columns(5)
        connection_columns[0].metric(tr("Reconnects", "重新連線"), metadata.get("reconnect_count", 0))
        connection_columns[1].metric(tr("Disconnects", "斷線"), metadata.get("disconnect_count", 0))
        connection_columns[2].metric(tr("Connection gaps", "連線缺口"), live_state.get("gap_count", 0))
        connection_columns[3].metric(
            tr("Current gap", "目前缺口"),
            f"{current_gap:.1f} s" if current_gap is not None else "—",
        )
        connection_columns[4].metric(
            tr("Live-state write errors", "即時狀態寫入錯誤"),
            metadata.get("live_state_write_errors", 0),
        )
        if live_state.get("last_gap_seconds") is not None:
            st.caption(
                f"{tr('Last completed connection gap', '最近一次連線缺口')}："
                f"{live_state['last_gap_seconds']:.1f} s | "
                f"{tr('Total observed gap time', '累計觀測缺口時間')}："
                f"{live_state.get('total_gap_seconds', 0):.1f} s"
            )

        viewer_samples = live_state.get("viewer_samples") or []
        if viewer_samples:
            frame = pd.DataFrame(viewer_samples)
            frame["time"] = pd.to_datetime(frame["received_at_ms"], unit="ms", utc=True).dt.tz_convert(TAIPEI_TZ)
            chart = alt.Chart(frame).mark_line().encode(
                x=alt.X("time:T", title=tr("Time", "時間")),
                y=alt.Y("viewer_count:Q", title=tr("Viewers", "觀眾數")),
                tooltip=["time:T", "viewer_count:Q"],
            ).properties(height=220)
            st.altair_chart(chart, use_container_width=True)

        worker_state = read_json(DATA_ROOT / "tts" / "state.json", {})
        if worker_state.get("active_session") == session_dir.name:
            tts_metrics = worker_state.get("metrics") or {}
            st.markdown(f"#### {tr('TTS health', 'TTS 健康狀態')}")
            tts_columns = st.columns(5)
            tts_columns[0].metric(tr("Worker", "Worker"), worker_state.get("status", "unknown"))
            tts_columns[1].metric(tr("Queue depth", "佇列長度"), tts_metrics.get("queue_depth", 0))
            tts_columns[2].metric(
                tr("Oldest queue age", "最久等待"),
                f"{(tts_metrics.get('oldest_queue_age_seconds') or 0):.1f} s",
            )
            tts_columns[3].metric(
                tr("Playback p50", "播報 p50"),
                f"{tts_metrics['p50_event_to_playback_ms']} ms" if tts_metrics.get("p50_event_to_playback_ms") is not None else "N/A",
            )
            tts_columns[4].metric(
                tr("Playback p95", "播報 p95"),
                f"{tts_metrics['p95_event_to_playback_ms']} ms" if tts_metrics.get("p95_event_to_playback_ms") is not None else "N/A",
            )
            gift_tts_columns = st.columns(5)
            gift_tts_columns[0].metric(
                tr("Gift failures", "\u79ae\u7269\u64ad\u5831\u5931\u6557"),
                tts_metrics.get("gift_delivery_failures", 0),
            )
            gift_tts_columns[1].metric(
                tr("Failure alerts", "\u5931\u6557\u63d0\u793a\u97f3"),
                tts_metrics.get("gift_failure_alerts_played", 0),
            )
            gift_tts_columns[2].metric(
                tr("Alert errors", "\u63d0\u793a\u97f3\u932f\u8aa4"),
                tts_metrics.get("gift_failure_alert_errors", 0),
            )
            gift_tts_columns[3].metric(
                tr("Pending Gift replays", "\u5f85\u91cd\u64ad\u79ae\u7269"),
                tts_metrics.get("pending_gift_replay_count", 0),
            )
            gift_tts_columns[4].metric(
                tr("Replay unavailable", "\u7121\u6cd5\u81ea\u52d5\u91cd\u64ad"),
                tts_metrics.get("gift_replay_unavailable", 0),
            )
            load_cols = st.columns(6)
            load_cols[0].metric(
                tr("Queue peak", "\u4f47\u5217\u5c16\u5cf0"),
                f"{tts_metrics.get('max_queue_depth', 0)} / {tts_metrics.get('queue_capacity', 0)}",
            )
            load_cols[1].metric(
                tr("Full queue waits", "\u4f47\u5217\u6eff\u6642\u7b49\u5f85"),
                tts_metrics.get("queue_full_waits", 0),
            )
            load_cols[2].metric(
                tr("Offline fallback audio", "\u96e2\u7dda\u5099\u63f4\u97f3\u8a0a\u5408\u6210"),
                tts_metrics.get("gift_offline_fallback_succeeded", 0),
            )
            load_cols[3].metric(
                tr("Offline fallback errors", "\u96e2\u7dda\u5099\u63f4\u5931\u6557"),
                tts_metrics.get("gift_offline_fallback_failures", 0),
            )
            load_cols[4].metric(
                tr("Chat offline fallback", "\u804a\u5929\u96e2\u7dda\u5099\u63f4"),
                tts_metrics.get("chat_offline_fallback_succeeded", 0),
            )
            load_cols[5].metric(
                tr("Chat fallback errors", "\u804a\u5929\u5099\u63f4\u5931\u6557"),
                tts_metrics.get("chat_offline_fallback_failures", 0),
            )
            if tts_metrics.get("gift_delivery_failures", 0):
                st.warning(tr(
                    "Some Gift announcements failed; see the Live TTS delivery stages for details.",
                    "\u6709\u79ae\u7269\u64ad\u5831\u5931\u6557\uff1b\u8acb\u5230 Live TTS \u7684\u64ad\u5831\u968e\u6bb5\u67e5\u770b\u8a73\u60c5\u3002",
                ))
            if tts_metrics.get("skipped"):
                st.json(tts_metrics["skipped"])

        chat_column, gift_column = st.columns(2)
        with chat_column:
            if st.toggle(
                tr("Show recent chat", "顯示最新聊天"),
                value=True,
                key=f"live_chat_visible_{session_dir.name}",
            ):
                st.dataframe(
                    list(reversed(live_state.get("recent_chat") or [])),
                    use_container_width=True,
                    hide_index=True,
                )
        with gift_column:
            if st.toggle(
                tr("Show recent gifts", "顯示最新禮物"),
                value=True,
                key=f"live_gifts_visible_{session_dir.name}",
            ):
                st.dataframe(
                    list(reversed(live_state.get("recent_gifts") or [])),
                    use_container_width=True,
                    hide_index=True,
                )
        if is_browser and live_state.get("recent_members"):
            st.subheader(tr("Recent entries", "最近進場觀眾"))
            st.dataframe(
                list(reversed(live_state["recent_members"])),
                use_container_width=True,
                hide_index=True,
            )

    return st.toggle(
        tr("Load full historical charts and comparisons", "載入完整歷史圖表與比較"),
        value=False,
        key=f"full_history_{session_dir.name}",
        help=tr(
            "Historical analysis scans the complete NDJSON session files.",
            "歷史分析會完整掃描這場直播的 NDJSON 檔案。",
        ),
    )


def render_session_catalog() -> None:
    """Show a bounded, unified index of Collector and Provider Benchmark sessions."""
    raw_paths = session_dirs()
    benchmark_paths = benchmark_session_dirs()
    candidates = []

    for path in raw_paths[:20]:
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            modified_at = 0.0
        source = "Browser Network" if _is_browser_session(path) else "Collector"
        candidates.append((path, source, session_metadata(path), modified_at))

    for path in benchmark_paths[:20]:
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            modified_at = 0.0
        metadata = read_json(path / "summary.json", {})
        source_name = metadata.get("backend") or path.parent.name
        candidates.append((path, f"LIVE session · {source_name}", metadata, modified_at))

    candidates.sort(key=lambda item: item[3], reverse=True)
    visible = candidates[:20]
    st.subheader(tr("Session capture history", "收集場次紀錄"))
    if not visible:
        st.info(tr("No recorded sessions yet.", "目前還沒有已記錄的場次。"))
        return

    rows = []
    benchmark_options = {}
    for path, source, metadata, _ in visible:
        event_counts = (
            metadata.get("event_counts")
            or metadata.get("normalized_event_counts")
            or {}
        )
        started = (
            metadata.get("started_at_utc")
            or metadata.get("started_at_local")
            or metadata.get("created_at_utc")
        )
        started_at = pd.to_datetime(started, utc=True, errors="coerce")
        if pd.notna(started_at):
            started_label = started_at.tz_convert(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            started_label = path.name
        username = metadata.get("username") or (path.name[16:] if len(path.name) > 16 else path.name)
        status = metadata.get("status") or tr("in progress", "進行中")
        rows.append({
            tr("Session", "場次"): path.name,
            tr("Streamer", "直播主"): f"@{username}",
            tr("Source", "來源"): source,
            tr("Status", "狀態"): status,
            tr("Started (Taiwan time)", "開始時間（台灣）"): started_label,
            tr("Chats", "聊天"): str(event_counts.get("chat", "—")),
            tr("Gifts", "禮物"): str(event_counts.get("gift", "—")),
            tr("Viewer samples", "觀眾採樣"): str(event_counts.get("viewer", "—")),
        })
        if source.startswith("LIVE session"):
            benchmark_label = f"{path.parent.name} · {path.name}"
            benchmark_options[benchmark_label] = path

    st.caption(
        tr(
            f"Showing the {len(visible)} most recent of {len(raw_paths) + len(benchmark_paths)} sessions. This list reads session summaries only.",
            f"顯示最近 {len(visible)} 場（共 {len(raw_paths) + len(benchmark_paths)} 場）；清單只讀場次摘要。",
        )
    )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if benchmark_options:
        selected_label = st.selectbox(
            tr("Open LIVE session", "開啟直播場次"),
            options=list(benchmark_options),
            key="dashboard_benchmark_session",
        )
        if st.button(
            tr("View session details", "查看場次詳細資料"),
            key="dashboard_open_benchmark_session",
        ):
            st.session_state["provider_benchmark_session"] = selected_label
            st.switch_page(live_sessions_page)


def render_session_settlement(session_dir: Path) -> None:
    """Show a read-only end-of-LIVE report for the selected archived session."""
    session_metadata_row = session_metadata(session_dir)
    if session_metadata_row.get("status") == "running":
        st.info(tr(
            "The settlement view is available after this capture ends. Use Overview and the event tabs while LIVE is running.",
            "場次結束後即可查看結算；直播進行中請使用總覽與事件分頁。",
        ))
        return
    try:
        report = load_session_report(session_dir)
    except Exception as error:
        st.error(tr("Could not build the session report.", "無法產生場次報表。") + f" {error}")
        return

    events = report.get("events") or {}
    counts = events.get("counts") or {}
    timeline = report.get("timeline") or {}
    users = report.get("users") or {}
    tts = report.get("tts") or {}
    tts_metrics = tts.get("metrics") or {}
    live_end = report.get("live_end_detection") or {}

    st.subheader(tr("Session settlement", "直播結束統整"))
    live_end_status = live_end.get("status") or "NOT_OBSERVED"
    if live_end_status == "LIVE_ENDED":
        end_label = tr("LIVE ended", "已偵測直播結束")
    elif live_end_status == "NOT_OBSERVED":
        end_label = tr("End signal not observed", "未收到結束訊號")
    else:
        end_label = str(live_end_status)
    duration_seconds = timeline.get("duration_seconds")
    duration_label = (
        f"{int(duration_seconds) // 3600}h {int(duration_seconds) % 3600 // 60}m"
        if isinstance(duration_seconds, (int, float)) else "—"
    )
    summary_columns = st.columns(6)
    summary_columns[0].metric(tr("LIVE end", "直播結束"), end_label)
    summary_columns[1].metric(tr("Observed event span", "事件涵蓋時間"), duration_label)
    summary_columns[2].metric(tr("Peak concurrent viewers", "最高同時觀眾"), events.get("peak_viewers") if events.get("peak_viewers") is not None else "—")
    summary_columns[3].metric(tr("Chat messages", "聊天室訊息"), counts.get("chat", 0))
    summary_columns[4].metric(tr("Member events", "Member／進場事件"), counts.get("member", 0))
    summary_columns[5].metric(tr("Gift diamonds", "禮物鑽石"), f"{events.get('gift_diamonds', 0):,}")

    timing_caption = [
        f"{tr('LIVE ended at', '直播結束時間')}：{report.get('live_end_at') or '—'}",
        f"{tr('Capture stopped at', '擷取停止時間')}：{report.get('ended_at') or '—'}",
    ]
    if report.get("post_end_capture_seconds") is not None:
        timing_caption.append(
            f"{tr('captured after end signal', '結束訊號後仍擷取')}：{report['post_end_capture_seconds'] / 60:.1f} {tr('min', '分鐘')}"
        )
    st.caption(" · ".join(timing_caption))

    trend_rows = []
    for phase in timeline.get("phases") or []:
        trend_rows.append({
            tr("Period", "時段"): phase.get("label"),
            tr("Time (Taiwan)", "時間（台灣）"): f"{phase.get('from') or '—'} – {phase.get('to') or '—'}",
            tr("Chat", "聊天"): phase.get("chat", 0),
            tr("Member", "進場事件"): phase.get("members", 0),
            tr("Average viewers", "平均觀眾"): phase.get("viewer_average"),
            tr("Peak viewers", "最高觀眾"): phase.get("viewer_peak"),
            tr("Gift items", "禮物數量"): phase.get("gift_quantity", 0),
            tr("Diamonds", "鑽石"): phase.get("gift_diamonds", 0),
            tr("Likes received", "收到的 Like"): phase.get("likes_received", 0),
            tr("Cumulative Likes", "累計 Like"): phase.get("like_total_end"),
        })
    if trend_rows:
        st.markdown(f"#### {tr('Audience and engagement by period', '分時段人數與互動')}")
        st.dataframe(pd.DataFrame(trend_rows), use_container_width=True, hide_index=True)

    viewer_cols = st.columns(7)
    viewer_cols[0].metric(tr("Viewer samples", "觀眾數樣本"), counts.get("viewer", 0))
    viewer_cols[1].metric(tr("Average viewers", "平均觀眾"), timeline.get("viewer_average") if timeline.get("viewer_average") is not None else "—")
    viewer_cols[2].metric(tr("Median viewers", "觀眾中位數"), timeline.get("viewer_median") if timeline.get("viewer_median") is not None else "—")
    viewer_cols[3].metric(tr("Last concurrent sample", "最後同時觀眾樣本"), events.get("last_viewers") if events.get("last_viewers") is not None else "—")
    viewer_cols[4].metric(tr("Last total_user_count", "最後 total_user_count"), events.get("last_room_user_count") if events.get("last_room_user_count") is not None else "—")
    viewer_cols[5].metric(tr("Likes received", "收到的 Like"), events.get("likes_received", 0))
    viewer_cols[6].metric(tr("Last cumulative Likes", "最後累計 Like"), events.get("last_total_likes") if events.get("last_total_likes") is not None else "—")
    st.caption(tr(
        "total_user_count is a separate TikTok field and is not the concurrent viewer sample.",
        "total_user_count 是 TikTok 的另一個欄位，不代表同時觀眾數。",
    ))

    gift_rows = report.get("gifts") or []
    st.markdown(f"#### {tr('Gift detail', '禮物明細')}")
    gift_frame = pd.DataFrame([
        {
            tr("Gift ID", "Gift ID"): row.get("gift_id"),
            tr("Gift name", "名稱"): row.get("display_name"),
            tr("Items", "數量"): row.get("quantity", 0),
            tr("Diamonds", "鑽石"): row.get("diamonds", 0),
            tr("Mapping", "名稱對照"): tr("Mapped", "已對照") if row.get("catalog_status") == "mapped" else tr("Needs review", "待確認"),
        }
        for row in gift_rows
    ])
    if not gift_frame.empty:
        total_row = {
            tr("Gift ID", "Gift ID"): tr("Total", "合計"),
            tr("Gift name", "名稱"): f"{len(gift_rows)} {tr('types', '種')}",
            tr("Items", "數量"): events.get("gift_item_quantity", 0),
            tr("Diamonds", "鑽石"): events.get("gift_diamonds", 0),
            tr("Mapping", "名稱對照"): "",
        }
        gift_frame = pd.concat([gift_frame, pd.DataFrame([total_row])], ignore_index=True)
        st.dataframe(gift_frame, use_container_width=True, hide_index=True)
    else:
        st.info(tr("No Gift events were captured in this session.", "這場沒有捕獲到禮物事件。"))

    donor_rows = users.get("top_gifters") or []
    if donor_rows:
        st.markdown(f"#### {tr('Top Gifters', '送禮者排行')}")
        donor_frame = pd.DataFrame([
            {
                tr("User ID", "使用者 ID"): f"@{row.get('user')}",
                tr("Nickname", "暱稱"): row.get("nickname") or "—",
                tr("Gift events", "送禮事件"): row.get("gift_events", 0),
                tr("Gift items", "禮物數量"): row.get("quantity", 0),
                tr("Gift types", "Gift 種類"): row.get("gift_ids", 0),
                tr("Diamonds", "鑽石"): row.get("diamonds", 0),
            }
            for row in donor_rows
        ])
        st.dataframe(donor_frame, use_container_width=True, hide_index=True)

    profile_counts = users.get("profile_field_counts") or {}
    profile_rows = []
    for event_type, label in (("chat", tr("Chat", "聊天")), ("gift", tr("Gift", "禮物")), ("member", "Member／進場")):
        row = profile_counts.get(event_type) or {}
        profile_rows.append({
            tr("Event type", "事件類型"): label,
            tr("Events", "事件數"): row.get("events", 0),
            tr("Has user grade", "含使用者等級"): row.get("user_grade", 0),
            tr("Has fan-club level", "含粉絲團等級"): row.get("fan_club", 0),
        })
    if profile_rows:
        st.markdown(f"#### {tr('User fields carried by events', '事件帶到的使用者資料')}")
        st.dataframe(pd.DataFrame(profile_rows), use_container_width=True, hide_index=True)
        st.caption(tr(
            "Missing fields mean TikTok did not include them in that event. The session contains fan-club levels but no independent iron-fan flag.",
            "欄位缺少表示 TikTok 該筆事件未提供；本場有粉絲團等級，但沒有獨立的鐵粉標記。",
        ))

    rank_rows = report.get("latest_rank_snapshot") or []
    if rank_rows:
        st.markdown(f"#### {tr('Latest TikTok ranking snapshot', '最後一次收到的 TikTok 排名資料')}")
        st.dataframe(pd.DataFrame(rank_rows), use_container_width=True, hide_index=True)
        st.caption(tr("This shows the ranks array carried by the event, not a guaranteed complete leaderboard.", "此表呈現事件所帶的 ranks 陣列，不保證是完整排行榜。"))

    if tts_metrics:
        st.markdown(f"#### {tr('TTS from this session', '本場 TTS 結果')}")
        tts_columns = st.columns(6)
        tts_columns[0].metric(tr("Chat spoken", "聊天播出"), f"{tts_metrics.get('chat_spoken', 0)} / {tts_metrics.get('chat_events_seen', 0)}")
        tts_columns[1].metric(tr("Gift spoken", "禮物播出"), f"{tts_metrics.get('gift_spoken', 0)} / {tts_metrics.get('gift_queued', 0)}")
        tts_columns[2].metric(tr("Gift failures", "禮物失敗"), tts_metrics.get("gift_delivery_failures", 0))
        tts_columns[3].metric(tr("Playback P50", "播報 P50"), f"{tts_metrics.get('p50_event_to_playback_ms', '—')} ms")
        tts_columns[4].metric(tr("Playback P95", "播報 P95"), f"{tts_metrics.get('p95_event_to_playback_ms', '—')} ms")
        tts_columns[5].metric(tr("Worker", "Worker"), tts.get("status") or "—")
        if tts_metrics.get("gift_delivery_failures") and not tts.get("gift_delivery_log"):
            st.warning(tr(
                "Gift failures are counted, but this session has no per-event delivery log to identify the failed Gift IDs.",
                "有記錄禮物播報失敗數，但場次中沒有逐筆投遞紀錄，無法指出失敗的 Gift ID。",
            ))

    st.caption(
        f"{tr('Unique chatters', '不重複聊天帳號')}：{users.get('unique_chatters', 0)} · "
        f"{tr('Unique Gifters', '不重複送禮帳號')}：{users.get('unique_gifters', 0)} · "
        f"{tr('Likes updated', 'Like 更新')}：{counts.get('like', 0)} · "
        f"{tr('Last total Likes', '最後總 Like 數')}：{events.get('last_total_likes') if events.get('last_total_likes') is not None else '—'}"
    )


def render_dashboard():
    config = read_json(CONFIG_PATH, {"streamers": []})
    state = {}
    streamers = [
        str(item.get("username", "")).lstrip("@")
        for item in config.get("streamers", [])
        if item.get("username")
    ]
    active_browser_streamers = [
        str(session_metadata(path).get("username") or "").lstrip("@")
        for path in active_browser_session_dirs()
        if session_metadata(path).get("username")
    ]
    historical_streamers = [
        str(session_metadata(path).get("username") or "").lstrip("@")
        for path in session_dirs()
        if session_metadata(path).get("username")
    ]
    analysis_streamers = list(dict.fromkeys([*active_browser_streamers, *streamers, *historical_streamers]))
    states = state.get("streamers", {})

    st.title(tr("TikTok LIVE Analytics", "TikTok 直播分析"))
    st.caption(
        tr(
            f"All displayed timestamps use Taiwan time: Asia/Taipei. Live data refreshes every {LIVE_REFRESH_SECONDS} seconds.",
            f"所有時間皆為台灣時間（Asia/Taipei）；即時資料每 {LIVE_REFRESH_SECONDS} 秒更新。",
        )
    )
    with st.sidebar:
        st.caption(
            tr(
                "LIVE capture comes from the Browser Network session you started in Chrome.",
                "??????? Chrome ??? Browser Network ?????",
            )
        )
        st.divider()
        st.header(tr("Session analysis", "場次分析"))
        username = st.selectbox(
            tr("Streamer", "直播主"),
            options=analysis_streamers or [""],
            format_func=lambda value: f"@{value}" if value else tr("No streamer", "沒有直播主"),
            key="analysis_username",
        )
        previous_username = st.session_state.get("_dashboard_previous_analysis_username")
        if previous_username != username:
            st.session_state["_dashboard_previous_analysis_username"] = username
            for dependent_key in (
                "analysis_manual_id",
                "analysis_selected_id",
                "comparison_session_ids",
            ):
                st.session_state.pop(dependent_key, None)
        tracking_dir = latest_live_session(username)
        manual_id = st.text_input(
            tr("Session ID or room ID (optional)", "Session ID 或直播間 ID（選填）"),
            placeholder="20260913_195928_chloe_o723_ or 7684970432562875157",
            help=tr("Enter a folder Session ID or TikTok room_id to select the LIVE.", "輸入資料夾 Session ID 或 TikTok room_id 來選擇直播場次。"),
            key="analysis_manual_id",
        )
        available = session_dirs(username) if username else []
        if username:
            available.extend(
                path for path in benchmark_session_dirs()
                if str(session_metadata(path).get("username") or "").lstrip("@") == username
            )
        available.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        if tracking_dir and tracking_dir not in available:
            available.insert(0, tracking_dir)
        analytics_sessions = [path for path in available if session_has_analytics(path)]
        if analytics_sessions:
            available = analytics_sessions
            if tracking_dir and tracking_dir not in available:
                available.insert(0, tracking_dir)
        available_ids = [path.name for path in available]
        labels = {path.name: session_label(path) for path in available}
        selected_id = st.selectbox(
            tr("Available sessions", "可用場次"),
            options=available_ids or [""],
            index=(available_ids.index(tracking_dir.name) if tracking_dir and tracking_dir.name in available_ids else 0),
            format_func=lambda value: labels.get(value, value or tr("No session", "沒有場次")),
            key="analysis_selected_id",
            disabled=bool(manual_id.strip()),
        )
        comparison_ids = st.multiselect(
            tr("Compare sessions", "比較場次"),
            options=available_ids,
            default=([selected_id] if selected_id else []),
            key="comparison_session_ids",
            help=tr("Select multiple sessions for trend, Gift, traffic, and social comparisons.", "選取多個場次，比較趨勢、禮物、人流與社群互動。"),
        )
        if manual_id.strip():
            st.caption(tr("Manual Session ID overrides the dropdown.", "手動輸入的 Session ID 優先於下拉選單。"))
        st.caption(f"{tr('Session data', '場次資料')}：{RAW_ROOT} · {BENCHMARK_ROOT} · {BROWSER_PROBE_ROOT}")

    with st.expander(tr("Recent sessions", "\u6700\u8fd1\u5834\u6b21"), expanded=False):
        render_session_catalog()

    if not username:
        st.warning(tr("No enabled streamer configured.", "尚未設定已啟用的直播主。"))
        return

    session_dir = resolve_session(manual_id, username, selected_id)
    if manual_id.strip() and session_dir is None:
        st.error(f"{tr('Session ID / room ID not found', '找不到 Session ID／直播間 ID')}：{manual_id.strip()}")
        return
    lightweight_live = bool(
        tracking_dir
        and not manual_id.strip()
        and session_dir == tracking_dir
    )
    if lightweight_live and not render_lightweight_live_view(
        tracking_dir, states.get(username, {})
    ):
        return

    metrics = load_session(session_dir)
    session = metrics["session"] or {}
    session_provider = session.get("provider") or ("browser_network" if _is_browser_session(session_dir) else None)
    viewer_correction_pending = False
    viewer_correction_required = False
    viewer_correction_key = f"browser_viewer_correction_requested_{session_dir.name}"
    request_viewer_correction = bool(st.session_state.get(viewer_correction_key, False))
    if session_provider == "browser_network" and session.get("status") != "running":
        metrics, viewer_correction_pending, viewer_correction_required = _correct_browser_metrics(
            metrics,
            [session_dir],
            request_correction=request_viewer_correction,
        )
    streamer_state = states.get(username, {})
    tracking_room_id = (session_metadata(tracking_dir) or {}).get("room_id") if tracking_dir else None
    tracking_fragments = room_session_dirs(username, tracking_room_id) if tracking_dir else []
    tracking_metrics = combine_session_metrics(tracking_fragments, tracking_dir) if tracking_fragments else metrics
    selected_room_id = session.get("room_id")
    selected_fragments = room_session_dirs(username, selected_room_id)
    selected_room_metrics = combine_session_metrics(selected_fragments, session_dir) if selected_fragments else metrics
    if session_provider == "browser_network" and session.get("status") != "running":
        selected_room_metrics, room_viewer_correction_pending, room_viewer_correction_required = _correct_browser_metrics(
            selected_room_metrics,
            selected_fragments or [session_dir],
            request_correction=request_viewer_correction,
        )
        viewer_correction_pending = viewer_correction_pending or room_viewer_correction_pending
        viewer_correction_required = viewer_correction_required or room_viewer_correction_required
    if tracking_dir and session_dir == tracking_dir and not manual_id.strip():
        display_metrics = tracking_metrics
    else:
        display_metrics = selected_room_metrics
        viewer_correction_pending = bool(
            viewer_correction_pending
            or selected_room_metrics.get("viewer_correction_pending")
        )
        viewer_correction_required = bool(
            viewer_correction_required
            or selected_room_metrics.get("viewer_correction_required")
        )
    comparison_paths = [
        path for session_id in comparison_ids
        for path in available
        if session_id and path.name == session_id
    ]
    if session_dir and session_dir not in comparison_paths:
        comparison_paths.insert(0, session_dir)
    for fragment in reversed(selected_fragments):
        if fragment not in comparison_paths:
            comparison_paths.insert(0, fragment)

    st.subheader(f"@{username}")
    if session_dir:
        st.caption(
            f"Session ID：{session_dir.name} | "
            f"{tr('status', '狀態')}={session.get('status', 'unknown')} | "
            f"{tr('session timezone', '場次時區')}={session.get('timezone', TAIPEI_TZ)}"
        )
    else:
        st.info(tr("No captured session is available for this streamer.", "此直播主目前沒有可用的收集紀錄。"))
        return

    if tracking_dir and session_dir == tracking_dir:
        st.success(f"{tr('LIVE collection active', '直播收集中')} - {tracking_dir.name}")
    elif metrics["viewer_rows"] or viewer_correction_pending or viewer_correction_required:
        st.info(tr("Historical session analysis - no active LIVE collector for this streamer.", "歷史場次分析；此直播主目前沒有運作中的 LIVE Collector。"))
    else:
        st.warning(tr("This session contains system logs only. Select a session labeled analytics to display trends.", "此場次只有系統紀錄。請選擇標示為 analytics 的場次查看趨勢。"))

    st.markdown(f"#### {tr('Audience', '觀眾概況')}")
    audience_cols = st.columns(4)
    if session_provider == "browser_network":
        capture_status = "LIVE" if session.get("status") == "running" else tr("Capture ended", "\u64f7\u53d6\u5df2\u7d50\u675f")
        audience_cols[0].metric(tr("Capture status", "\u64f7\u53d6\u72c0\u614b"), capture_status)
        audience_cols[1].metric(
            tr("Latest viewer sample", "\u6700\u65b0\u89c0\u773e\u6578\u6a23\u672c"),
            display_metrics["viewers"][-1]
            if display_metrics["viewers"]
            else tr("Correcting...", "\u6b63\u5728\u6821\u6b63") if viewer_correction_pending
            else tr("Needs correction", "\u5f85\u6821\u6b63") if viewer_correction_required
            else "N/A",
            help=tr(
                "Most recent WebcastRoomUserSeqMessage sample received by Chrome CDP. It updates when TikTok sends a new sample.",
                "Chrome CDP \u6700\u8fd1\u6536\u5230\u7684 WebcastRoomUserSeqMessage \u6a23\u672c\uff1bTikTok \u50b3\u4f86\u65b0\u6a23\u672c\u6642\u624d\u66f4\u65b0\u3002",
            ),
        )
    else:
        audience_cols[0].metric(tr("Tracking status", "\u8ffd\u8e64\u72c0\u614b"), streamer_state.get("status", "unknown"))
        audience_cols[1].metric(tr("Latest viewer sample", "\u6700\u65b0\u89c0\u773e\u6578\u6a23\u672c"), display_metrics["viewers"][-1] if display_metrics["viewers"] else "N/A")
    audience_cols[2].metric(
        tr("Peak viewers", "最高觀眾數"),
        max(display_metrics["viewers"])
        if display_metrics["viewers"]
        else tr("Correcting...", "\u6b63\u5728\u6821\u6b63") if viewer_correction_pending
        else tr("Needs correction", "\u5f85\u6821\u6b63") if viewer_correction_required
        else "N/A",
    )
    if viewer_correction_pending:
        st.info(tr(
            "Accurate Browser viewer samples are being rebuilt from the captured frames. Other dashboard sections are ready while this runs.",
            "\u6b63\u5728\u5f9e\u5df2\u4fdd\u5b58\u7684\u5f71\u683c\u91cd\u5efa\u6b63\u78ba\u7684\u700f\u89bd\u5668\u89c0\u773e\u6578\uff1b\u8655\u7406\u671f\u9593\u5176\u4ed6\u5100\u8868\u677f\u5167\u5bb9\u4ecd\u53ef\u4f7f\u7528\u3002",
        ))
    elif viewer_correction_required:
        st.info(tr(
            "This archived session has viewer samples that need raw-frame correction. The dashboard stays responsive until you request it.",
            "此場次的觀眾樣本需要從原始影格校正；在你啟動校正前，儀表板會保持即時可操作。",
        ))
        if st.button(
            tr("Correct archived viewer samples", "校正封存觀眾人數"),
            key=f"{viewer_correction_key}_button",
        ):
            st.session_state[viewer_correction_key] = True
            st.rerun()
    audience_cols[3].metric(tr("Join events", "進場事件"), display_metrics["joins"])

    st.markdown(f"#### {tr('Engagement & captured value', '互動與收集數據')}")
    engagement_cols = st.columns(6)
    engagement_cols[0].metric(tr("Chat messages", "聊天訊息"), display_metrics["chat"])
    engagement_cols[1].metric(tr("Current likes", "目前總按讚數"), display_metrics["like_current_total"] if display_metrics["like_current_total"] is not None else display_metrics["likes"])
    engagement_cols[2].metric(tr("Observed likes", "收集到的按讚增量"), display_metrics["likes_observed"])
    engagement_cols[3].metric(tr("Follows / Shares", "追蹤／分享"), f"{display_metrics['follows']} / {display_metrics['shares']}")
    engagement_cols[4].metric(tr("Subscribes", "訂閱"), display_metrics["subscribes"])
    engagement_cols[5].metric(tr("Captured diamonds", "紀錄到的 Diamonds"), int(display_metrics["diamonds"]))
    if display_metrics["like_current_total"] is not None:
        st.caption(
            tr(
                "Like total uses TikTok totalLikes; observed likes is the batch increment captured by this collector. Estimated pre-capture likes:",
                "目前按讚數使用 TikTok totalLikes；收集到的按讚增量是 Collector 捕獲的批次增量。估計開始收集前的按讚數：",
            )
            + f" {display_metrics['like_baseline_estimate']}."
        )

    tab_overview, tab_chat, tab_gifts, tab_compare, tab_audience, tab_more, tab_settlement = st.tabs(
        [
            tr("Overview", "\u7e3d\u89bd"),
            tr("Chat", "\u804a\u5929"),
            tr("Gifts", "\u79ae\u7269"),
            tr("Compare", "\u6bd4\u8f03"),
            tr("Audience & social", "\u89c0\u773e\u8207\u793e\u7fa4"),
            tr("Rankings & health", "\u6392\u884c\u8207\u7cfb\u7d71"),
            tr("Session settlement", "場次結算"),
        ],
        key="dashboard_analysis_tabs",
        on_change="rerun",
    )
    if tab_overview.open:
        with tab_overview:
            render_live_tracking(tracking_dir, tracking_metrics, streamer_state)

            st.markdown(f"#### {tr('Viewer and activity trends', '\u89c0\u773e\u8207\u4e92\u52d5\u8da8\u52e2')}")
            if not metrics["viewer_rows"] and not metrics["activity_rows"]:
                st.warning(tr("No timestamped analytics events are available in this session.", "此場次沒有含時間戳記的分析事件。"))
            else:
                trend_cols = st.columns(3)
                trend_cols[0].metric(
                    tr("Viewer samples", "觀眾採樣"),
                    tr("Correcting...", "\u6b63\u5728\u6821\u6b63")
                    if metrics.get("viewer_correction_pending")
                    else tr("Needs correction", "\u5f85\u6821\u6b63")
                    if metrics.get("viewer_correction_required")
                    else len(metrics["viewer_rows"]),
                )
                trend_cols[1].metric(tr("Captured events", "捕獲事件"), sum(metrics["counts"].values()))
                trend_cols[2].metric(tr("Event categories", "事件類別"), len([value for value in metrics["counts"].values() if value]))
            st.subheader(tr("Viewer trend", "觀眾趨勢"))
            if metrics.get("viewer_correction_pending"):
                st.info(tr(
                    "The archived Browser viewer samples are being corrected from their raw frames. This view will update automatically when ready.",
                    "\u6b63\u5728\u4f7f\u7528\u539f\u59cb\u5f71\u683c\u6821\u6b63\u5df2\u5c01\u5b58\u7684\u89c0\u773e\u4eba\u6578\uff1b\u5b8c\u6210\u5f8c\u6b64\u5716\u8868\u6703\u81ea\u52d5\u66f4\u65b0\u3002",
                ))
            elif metrics.get("viewer_correction_required"):
                st.info(tr(
                    "Use Correct archived viewer samples above to rebuild this chart from the saved raw frames.",
                    "請使用上方的「校正封存觀眾人數」按鈕，從已保存的原始影格重建圖表。",
                ))
            else:
                render_viewer_chart(metrics["viewer_rows"])
            st.subheader(tr("Activity trend by event type", "依事件類型查看互動趨勢"))
            render_activity_chart(metrics["activity_rows"])
            counts_frame = pd.DataFrame(
                [{"event_type": key, "events": value} for key, value in metrics["counts"].most_common()]
            )
            if not counts_frame.empty:
                st.subheader(tr("Captured event breakdown", "捕獲事件統計"))
                st.dataframe(counts_frame, use_container_width=True, hide_index=True)

    if tab_chat.open:
        with tab_chat:
            show_dashboard_chat = st.toggle(
                tr("Show recent chat list", "顯示最新聊天清單"), value=True, key="dashboard_show_recent_chat"
            )
            chat_limit = int(
                st.number_input(
                    tr("Latest messages to show", "顯示最新訊息筆數"),
                    min_value=1,
                    max_value=200,
                    value=30,
                    step=1,
                    key="dashboard_recent_chat_limit",
                )
            )
            if not show_dashboard_chat:
                st.caption(tr("Recent chat list is hidden.", "最新聊天清單已隱藏。"))
            elif metrics["recent_chat"]:
                st.dataframe(
                    metrics["recent_chat"][:chat_limit],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info(tr("No chat messages in this session.", "此場次沒有聊天訊息。"))

    if tab_compare.open:
        with tab_compare:
            render_compare_tab(comparison_paths)

    if tab_gifts.open:
        with tab_gifts:
            render_gift_tab(comparison_paths)

    if tab_audience.open:
        with tab_audience:
            render_traffic_social_tab(comparison_paths)

    if tab_more.open:
        with tab_more:
            render_snapshots_tab(comparison_paths)
            render_health_tab(comparison_paths)

            quality = session.get("data_quality", {})
            st.json({
                "session_id": session.get("session_id", session_dir.name),
                "status": session.get("status"),
                "collector_started_at_local": session.get("collector_started_at_local"),
                "collector_ended_at_local": session.get("collector_ended_at_local"),
                "last_received_at_local": session.get("last_received_at_local") or local_timestamp(session.get("last_received_at_utc")),
                "socket_uptime_ratio": quality.get("socket_uptime_ratio"),
                "socket_gap_seconds": quality.get("socket_gap_seconds"),
                "duplicate_events_dropped": quality.get("duplicate_events_dropped"),
                "sdk_error_events": quality.get("sdk_error_events"),
            })

    if tab_settlement.open:
        with tab_settlement:
            render_session_settlement(session_dir)

    st.divider()
@st.fragment(run_every=LIVE_REFRESH_SECONDS)
def render_dashboard_page():
    render_dashboard()


@st.fragment(run_every=5)
def render_provider_benchmark_page():
    render_provider_benchmark()


@st.fragment(run_every=LIVE_REFRESH_SECONDS)
def render_live_tts_page():
    config = read_json(CONFIG_PATH, {"streamers": []})
    state = read_json(WATCHER_STATE, {"streamers": {}})
    streamers = [
        str(item.get("username", "")).lstrip("@")
        for item in config.get("streamers", [])
        if item.get("username")
    ]
    active_browser_streamers = [
        str(session_metadata(path).get("username") or "").lstrip("@")
        for path in active_browser_session_dirs()
        if session_metadata(path).get("username")
    ]
    streamers = list(dict.fromkeys([*active_browser_streamers, *streamers]))
    import app.tts_ui as tts_ui
    tts_ui = importlib.reload(tts_ui)
    tts_ui.render_tts(streamers, state.get("streamers", {}))


def render_reports_page():
    config = read_json(CONFIG_PATH, {"streamers": []})
    streamers = [
        str(item.get("username", "")).lstrip("@")
        for item in config.get("streamers", [])
        if item.get("username")
    ]
    streamers = list(dict.fromkeys(
        [*streamers]
        + [str(session_metadata(path).get("username") or "").lstrip("@") for path in session_dirs()]
    ))
    from app.report_runtime import load_reports_ui
    load_reports_ui().render_reports([RAW_ROOT, BENCHMARK_ROOT, BROWSER_PROBE_ROOT], streamers)


def render_chat_sender_page():
    from app.chat_sender_ui import render_chat_sender
    render_chat_sender()


render_language_selector()

live_sessions_page = st.Page(
    render_provider_benchmark_page,
    title=tr("Live Sessions", "直播場次"),
    url_path="sessions",
)

pages = {
    tr("Live", "直播"): [
        st.Page(render_dashboard_page, title=tr("Dashboard", "儀表板"), default=True),
        live_sessions_page,
        st.Page(render_live_tts_page, title=tr("Live TTS", "直播語音"), url_path="live-tts"),
    ],
    tr("Tools", "工具"): [
        st.Page(render_reports_page, title=tr("Reports", "直播報表"), url_path="reports"),
        st.Page(
            render_chat_sender_page,
            title=tr("Chat Sender", "聊天室訊息發送"),
            url_path="chat-sender",
            visibility="hidden",
        ),
    ],
}
st.navigation(pages, position="sidebar").run()
