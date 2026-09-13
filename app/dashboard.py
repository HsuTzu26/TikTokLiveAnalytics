from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
RAW_ROOT = DATA_ROOT / "raw"
WATCHER_STATE = DATA_ROOT / "watcher" / "watcher_state.json"
WATCHER_LOG = DATA_ROOT / "watcher" / "watcher.log"
WATCHER_PID = DATA_ROOT / "watcher" / "watcher.pid"
WATCHER_STOP = DATA_ROOT / "watcher" / "watcher.stop"
CONFIG_PATH = ROOT / "watchlist.json"


st.set_page_config(
    page_title="TikTok LIVE Analytics",
    page_icon="📡",
    layout="wide",
)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


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


def start_watcher() -> str:
    pid = watcher_pid()
    if pid_is_running(pid):
        return f"Watcher already running (PID {pid})."

    WATCHER_PID.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "src" / "watcher.py"),
            "--config",
            str(CONFIG_PATH),
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
    WATCHER_PID.write_text(str(process.pid), encoding="ascii")
    return f"Watcher started (PID {process.pid})."


def stop_watcher() -> str:
    pid = watcher_pid()
    if not pid_is_running(pid):
        WATCHER_PID.unlink(missing_ok=True)
        return "Watcher is not running."

    WATCHER_STOP.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_STOP.write_text("requested", encoding="ascii")
    return f"Graceful stop requested for Watcher (PID {pid})."


def latest_session(username: str) -> Path | None:
    if not RAW_ROOT.exists():
        return None
    suffix = f"_{username}"
    candidates = [
        path for path in RAW_ROOT.iterdir()
        if path.is_dir() and path.name.endswith(suffix)
    ]
    return max(candidates, key=lambda path: path.name) if candidates else None


def load_session(session_dir: Path | None):
    summary = {
        "session": None,
        "counts": Counter(),
        "viewers": [],
        "likes": 0,
        "diamonds": 0,
        "chat": 0,
        "joins": 0,
        "follows": 0,
        "gifts": 0,
        "recent_chat": [],
    }
    if session_dir is None:
        return summary

    summary["session"] = read_json(session_dir / "session.json", {})
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
            if event_type == "viewer":
                value = event.get("viewer_count")
                if isinstance(value, (int, float)):
                    summary["viewers"].append(value)
            elif event_type == "like":
                summary["likes"] += event.get("like_count", 0) or 0
            elif event_type == "gift" and event.get("counted"):
                summary["gifts"] += 1
                summary["diamonds"] += event.get("diamond_total", 0) or 0
            elif event_type == "chat":
                summary["chat"] += 1
                summary["recent_chat"].append({
                    "time": event.get("received_at_local")
                    or event.get("timestamp_local")
                    or event.get("received_at_utc")
                    or event.get("timestamp_utc"),
                    "user": event.get("unique_id") or event.get("nickname"),
                    "comment": event.get("comment"),
                })
            elif event_type == "member":
                summary["joins"] += 1
            elif event_type == "social" and event.get("social_action") == "follow":
                summary["follows"] += 1

    summary["recent_chat"] = summary["recent_chat"][-30:][::-1]
    return summary


def render_dashboard():
    config = read_json(CONFIG_PATH, {"streamers": []})
    state = read_json(WATCHER_STATE, {"streamers": {}})
    streamers = config.get("streamers", [])
    states = state.get("streamers", {})

    st.title("TikTok LIVE Analytics")
    st.caption("Display timezone: Asia/Taipei")
    st.caption("Watcher / Collector / Dashboard 分離；Streamlit 不持有 TikTok WebSocket。")

    with st.sidebar:
        st.header("Watch control")
        pid = watcher_pid()
        running = pid_is_running(pid)
        st.write(f"Watcher: **{'RUNNING' if running else 'STOPPED'}**")
        if pid:
            st.caption(f"PID: {pid}")
        if st.button("Start watcher", disabled=running, use_container_width=True):
            st.success(start_watcher())
            st.rerun()
        if st.button("Stop watcher", disabled=not running, use_container_width=True):
            st.warning(stop_watcher())
            st.rerun()
        st.divider()
        st.caption(f"Config: {CONFIG_PATH}")
        st.caption(f"Raw data: {RAW_ROOT}")

    if not streamers:
        st.warning("watchlist.json 沒有設定主播。")
        return

    for item in streamers:
        username = str(item.get("username", "")).lstrip("@")
        if not username:
            continue
        streamer_state = states.get(username, {})
        session_dir = latest_session(username)
        metrics = load_session(session_dir)
        session = metrics["session"] or {}

        st.subheader(f"@{username}")
        cols = st.columns(6)
        cols[0].metric("Watch status", streamer_state.get("status", "unknown"))
        cols[1].metric("Current viewer", metrics["viewers"][-1] if metrics["viewers"] else "—")
        cols[2].metric("Peak viewer", max(metrics["viewers"]) if metrics["viewers"] else "—")
        cols[3].metric("Chat", metrics["chat"])
        cols[4].metric("Likes", metrics["likes"])
        cols[5].metric("Diamonds", metrics["diamonds"])

        if session:
            st.caption(
                f"Session: {session.get('session_id', session_dir.name if session_dir else '—')} | "
                f"status={session.get('status', 'unknown')} | "
                f"raw events={session.get('raw_event_count', '—')}"
            )

        tab_overview, tab_chat, tab_quality = st.tabs(
            ["Trend", "Recent chat", "Data quality"]
        )
        with tab_overview:
            if metrics["viewers"]:
                st.line_chart({"viewer_count": metrics["viewers"]})
            else:
                st.info("尚未有 viewer samples。")
            st.write({"event_counts": dict(metrics["counts"])})
        with tab_chat:
            if metrics["recent_chat"]:
                st.dataframe(metrics["recent_chat"], use_container_width=True, hide_index=True)
            else:
                st.info("尚未有 chat。")
        with tab_quality:
            quality = session.get("data_quality", {})
            st.json({
                "socket_uptime_ratio": quality.get("socket_uptime_ratio"),
                "socket_gap_seconds": quality.get("socket_gap_seconds"),
                "duplicate_events_dropped": quality.get("duplicate_events_dropped"),
                "sdk_error_events": quality.get("sdk_error_events"),
                "last_probe_error": streamer_state.get("last_probe_error"),
            })

    st.divider()
    st.subheader("Watcher log")
    try:
        log_lines = WATCHER_LOG.read_text(encoding="utf-8").splitlines()
        st.code("\n".join(log_lines[-80:]) or "(empty)")
    except OSError:
        st.info("尚未建立 watcher.log。")


if hasattr(st, "fragment"):
    @st.fragment(run_every=5)
    def live_dashboard():
        render_dashboard()

    live_dashboard()
else:
    render_dashboard()