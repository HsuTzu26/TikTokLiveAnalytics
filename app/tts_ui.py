"""Streamlit controls for the optional local LIVE chat TTS worker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

from src.tts.pipeline import TTSSettings


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
TTS_ROOT = DATA_ROOT / "tts"
RAW_ROOT = DATA_ROOT / "raw"
WATCHER_ROOT = DATA_ROOT / "watcher"
WATCHER_STATE = WATCHER_ROOT / "watcher_state.json"
WATCHER_PID = WATCHER_ROOT / "watcher.pid"
WORKER_SCRIPT = ROOT / "src" / "tts_worker.py"
CONFIG_PATH = TTS_ROOT / "config.json"
STATE_PATH = TTS_ROOT / "state.json"
STOP_PATH = TTS_ROOT / "stop.json"
LOG_PATH = TTS_ROOT / "worker.log"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def pid_is_running(pid: object) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
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
    except OSError:
        return False
    return True


def watcher_pid() -> int | None:
    try:
        return int(WATCHER_PID.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def start_worker(username: str, settings: TTSSettings) -> tuple[bool, str]:
    TTS_ROOT.mkdir(parents=True, exist_ok=True)
    current_state = read_json(STATE_PATH, {})
    current_pid = current_state.get("pid") if isinstance(current_state, dict) else None
    if pid_is_running(current_pid):
        return False, f"TTS worker already running (PID {current_pid}). Stop it before switching streamer."

    write_json(CONFIG_PATH, settings.to_mapping())
    STOP_PATH.unlink(missing_ok=True)
    log_handle = LOG_PATH.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "src.tts_worker", "--username", username],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as error:
        log_handle.close()
        return False, f"Could not start TTS worker ({type(error).__name__})."
    finally:
        if not log_handle.closed:
            log_handle.close()

    time.sleep(0.15)
    if process.poll() is not None:
        failed = read_json(STATE_PATH, {})
        reason = failed.get("last_error") if isinstance(failed, dict) else None
        return False, f"TTS worker exited during startup{': ' + str(reason) if reason else ''}; check its log below."

    state = read_json(STATE_PATH, {})
    if not isinstance(state, dict) or state.get("pid") != process.pid:
        state = {
            "pid": process.pid,
            "username": username,
            "status": "starting",
            "started_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            "updated_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            "metrics": {},
        }
        write_json(STATE_PATH, state)
    return True, f"TTS worker started for @{username} (PID {process.pid})."


def request_worker_stop() -> None:
    write_json(STOP_PATH, {"requested_at_local": datetime.now().astimezone().isoformat(timespec="seconds")})


def _tail_log(path: Path, limit: int = 12) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readlines()[-limit:]
    except OSError:
        return []


def render_tts(streamers: list[str], watcher_states: dict) -> None:
    st.title("Live Chat TTS")
    st.caption("Optional bilingual text-to-speech for chat captured by the existing Collector. Status refreshes every 2 seconds.")
    st.info(
        "This worker reads the Collector’s local events.ndjson file. It does not open another TikTok connection, "
        "does not replay earlier chat when started mid-stream, and never sends messages to the chat."
    )
    st.warning(
        "Audio currently plays through this Windows computer’s default sound output. To include it in LIVE Studio/OBS, "
        "capture desktop audio or route it through a virtual audio cable."
    )

    choices = [name for name in streamers if name]
    if not choices:
        st.error("No streamer is configured in the Watch list.")
        return

    state = read_json(STATE_PATH, {})
    current_pid = state.get("pid") if isinstance(state, dict) else None
    running = pid_is_running(current_pid)
    current_username = str((state or {}).get("username") or "")
    selected = st.selectbox(
        "Streamer",
        options=choices,
        index=choices.index(current_username) if current_username in choices else 0,
        format_func=lambda value: f"@{value}",
        disabled=running,
        key="tts_streamer",
    )

    watcher_state = watcher_states.get(selected, {}) if isinstance(watcher_states, dict) else {}
    st.caption(
        f"Collector: {watcher_state.get('status', 'unknown')}"
        + (f" · PID {watcher_state.get('collector_pid')}" if watcher_state.get("collector_pid") else "")
    )

    saved = read_json(CONFIG_PATH, {})
    settings = TTSSettings.from_mapping(saved)
    st.subheader("Voice and moderation")
    with st.form("tts_settings_form"):
        voice_cols = st.columns(2)
        zh_voice = voice_cols[0].text_input(
            "Traditional Chinese voice",
            value=settings.zh_voice,
            disabled=running,
            help="Default: zh-TW-HsiaoChenNeural",
        )
        en_voice = voice_cols[1].text_input(
            "English voice",
            value=settings.en_voice,
            disabled=running,
            help="Default: en-US-AriaNeural",
        )
        setting_cols = st.columns(4)
        rate = setting_cols[0].number_input(
            "Speech rate (%)", min_value=-50, max_value=100,
            value=settings.rate_percent, step=5, disabled=running,
        )
        volume = setting_cols[1].slider(
            "Playback volume", min_value=0, max_value=100,
            value=settings.volume, disabled=running,
        )
        max_length = setting_cols[2].number_input(
            "Max characters", min_value=1, max_value=200,
            value=settings.max_text_length, step=5, disabled=running,
        )
        queue_size = setting_cols[3].number_input(
            "Queue size", min_value=1, max_value=100,
            value=settings.queue_size, step=1, disabled=running,
        )
        guard_cols = st.columns(4)
        cooldown = guard_cols[0].number_input(
            "Per-user cooldown (seconds)", min_value=0.0, max_value=300.0,
            value=float(settings.user_cooldown_seconds), step=0.5, disabled=running,
        )
        duplicate_window = guard_cols[1].number_input(
            "Duplicate filter window (seconds)", min_value=0.0, max_value=600.0,
            value=float(settings.duplicate_window_seconds), step=1.0, disabled=running,
        )
        ignore_emoji = guard_cols[2].checkbox(
            "Ignore emoji", value=settings.ignore_emoji, disabled=running,
        )
        request_gap = guard_cols[3].number_input(
            "Minimum TTS request gap (seconds)", min_value=0.5, max_value=30.0,
            value=float(settings.min_request_interval_seconds), step=0.5, disabled=running,
            help="Spaces remote speech synthesis requests; failures trigger an automatic backoff.",
        )
        blacklist = st.text_area(
            "Blocked words or phrases (one per line)",
            value="\n".join(settings.blacklist_terms),
            disabled=running,
            help="A matching message is skipped before it reaches the speech service.",
        )
        saved_settings = st.form_submit_button("Save settings", disabled=running)
        form_values = {
            "zh_voice": zh_voice,
            "en_voice": en_voice,
            "rate_percent": int(rate),
            "volume": int(volume),
            "max_text_length": int(max_length),
            "queue_size": int(queue_size),
            "user_cooldown_seconds": float(cooldown),
            "duplicate_window_seconds": float(duplicate_window),
            "min_request_interval_seconds": float(request_gap),
            "ignore_emoji": bool(ignore_emoji),
            "blacklist_terms": blacklist.splitlines(),
        }
        if saved_settings:
            normalized = TTSSettings.from_mapping(form_values)
            write_json(CONFIG_PATH, normalized.to_mapping())
            st.success("TTS settings saved locally.")

    st.caption(
        "URLs are removed, long text is truncated, repeated-character spam is reduced, and a full queue drops new messages. "
        "Chinese/English routing selects one voice for each whole message."
    )

    watcher_pid_value = None
    try:
        watcher_pid_value = int((WATCHER_PID.read_text(encoding="ascii")).strip())
    except (OSError, ValueError):
        pass
    watcher_running = pid_is_running(watcher_pid_value)
    controls = st.columns(2)
    if controls[0].button(
        "Start TTS worker",
        type="primary",
        disabled=running or not watcher_running,
        use_container_width=True,
    ):
        normalized = TTSSettings.from_mapping(form_values)
        ok, message = start_worker(selected, normalized)
        (st.success if ok else st.error)(message)
        st.rerun()

    stop_pending = STOP_PATH.exists()
    if controls[1].button(
        "Stop TTS worker",
        disabled=not running or stop_pending,
        use_container_width=True,
    ):
        request_worker_stop()
        st.warning("Graceful stop requested. Any current speech will be stopped; collection continues.")
        st.rerun()

    if not watcher_running:
        st.info("Start the Watcher first. TTS attaches only to an existing Collector’s local event file.")

    if running:
        st.success(f"TTS worker running for @{current_username} · PID {current_pid} · status: {state.get('status', 'starting')}")
    elif isinstance(state, dict) and state.get("status") == "error":
        st.error(f"Last TTS worker failed: {state.get('last_error') or 'see worker log'}")
    elif isinstance(state, dict) and state.get("status") == "stopped":
        st.caption(f"Last worker stopped at {state.get('stopped_at_local', 'unknown')}.")

    metrics = (state or {}).get("metrics", {}) if isinstance(state, dict) else {}
    metric_cols = st.columns(5)
    metric_cols[0].metric("Chats seen", metrics.get("chat_events_seen", 0))
    metric_cols[1].metric("Spoken", metrics.get("spoken", 0))
    metric_cols[2].metric("Queued", f"{metrics.get('queue_depth', 0)} / {metrics.get('queue_capacity', settings.queue_size)}")
    metric_cols[3].metric("Skipped", sum((metrics.get("skipped") or {}).values()))
    latency = metrics.get("average_event_to_playback_ms")
    metric_cols[4].metric("Avg. chat-to-playback", f"{latency} ms" if latency is not None else "N/A")
    if metrics.get("skipped"):
        with st.expander("Skipped message counts"):
            st.json(metrics["skipped"])
    if metrics.get("last_error"):
        st.warning(f"Last TTS/audio error: {metrics['last_error']}")
    if metrics.get("tts_retry_after_local"):
        st.info(f"TTS service backoff active until {metrics['tts_retry_after_local']} (Taiwan time).")
    if LOG_PATH.exists():
        with st.expander("TTS worker log"):
            st.code("".join(_tail_log(LOG_PATH)).strip() or "(empty)")
