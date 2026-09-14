from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from src.analytics import (
    audience_metrics,
    build_health_report,
    compare_sessions,
    gift_detail,
    gift_leaderboard,
    social_summary,
    traffic_sources,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
RAW_ROOT = DATA_ROOT / "raw"
WATCHER_STATE = DATA_ROOT / "watcher" / "watcher_state.json"
WATCHER_LOG = DATA_ROOT / "watcher" / "watcher.log"
WATCHER_PID = DATA_ROOT / "watcher" / "watcher.pid"
WATCHER_STOP = DATA_ROOT / "watcher" / "watcher.stop"
CONFIG_PATH = ROOT / "watchlist.json"
TAIPEI_TZ = "Asia/Taipei"


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
    WATCHER_PID.unlink(missing_ok=True)
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
    # watcher.py owns watcher.pid. Do not write the dashboard launcher PID:
    # the venv launcher may spawn a different Python process and the watcher
    # would otherwise exit with "already running".
    for _ in range(30):
        time.sleep(0.1)
        pid = watcher_pid()
        if pid and pid_is_running(pid):
            return f"Watcher started (PID {pid})."
        if process.poll() is not None:
            break
    if process.poll() is None:
        return f"Watcher start requested (launcher PID {process.pid}); PID file not ready yet."
    return f"Watcher failed to start (exit code {process.returncode}); check {WATCHER_LOG}."


def stop_watcher() -> str:
    pid = watcher_pid()
    if not pid_is_running(pid):
        WATCHER_PID.unlink(missing_ok=True)
        return "Watcher is not running."

    WATCHER_STOP.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_STOP.write_text("requested", encoding="ascii")
    return f"Graceful stop requested for Watcher (PID {pid})."


def session_dirs(username: str | None = None) -> list[Path]:
    if not RAW_ROOT.exists():
        return []
    result = []
    for path in RAW_ROOT.iterdir():
        if not path.is_dir() or not (path / "session.json").exists():
            continue
        if username and not path.name.endswith(f"_{username}"):
            continue
        result.append(path)
    return sorted(result, key=lambda item: item.name, reverse=True)


def resolve_session(manual_id: str, username: str, selected_id: str) -> Path | None:
    manual_id = manual_id.strip()
    if manual_id:
        candidate = RAW_ROOT / manual_id
        if candidate.is_dir() and (candidate / "session.json").exists():
            return candidate

        # Also accept a TikTok room_id and resolve it to the newest matching
        # captured session for the selected streamer.
        matches = []
        for path in session_dirs(username):
            meta = read_json(path / "session.json", {})
            source_ids = [str(value) for value in meta.get("source_session_ids") or []]
            if (
                str(meta.get("room_id") or "") == manual_id
                or str(meta.get("session_id") or "") == manual_id
                or manual_id in source_ids
            ):
                matches.append(path)
        return matches[0] if matches else None

    if selected_id:
        candidate = RAW_ROOT / selected_id
        if candidate.is_dir():
            return candidate

    candidates = session_dirs(username)
    return candidates[0] if candidates else None


def load_session(session_dir: Path | None):
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
        "gifts": 0,
        "recent_chat": [],
        "recent_activity": [],
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
            event_time = (
                event.get("timestamp_local")
                or event.get("received_at_local")
                or event.get("timestamp_utc")
                or event.get("received_at_utc")
            )

            if event_type == "viewer":
                value = event.get("viewer_count")
                if isinstance(value, (int, float)) and event_time:
                    summary["viewers"].append(value)
                    summary["viewer_rows"].append({
                        "time": event_time,
                        "viewer_count": value,
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
            elif event_type == "social" and event.get("social_action") == "follow":
                summary["follows"] += 1

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

    summary["recent_chat"] = summary["recent_chat"][-30:][::-1]
    summary["recent_activity"] = summary["recent_activity"][-50:][::-1]
    return summary


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
        st.info("No viewer samples in this session.")
        return

    chart = (
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                "time:T",
                title="Taiwan time (Asia/Taipei)",
                axis=alt.Axis(format="%m-%d %H:%M"),
            ),
            y=alt.Y("viewer_count:Q", title="Observed viewers"),
            tooltip=[
                alt.Tooltip("time:T", title="Taiwan time", format="%Y-%m-%d %H:%M:%S"),
                alt.Tooltip("viewer_count:Q", title="Viewers"),
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
        .resample("1min")["count"]
        .sum()
        .reset_index()
    )
    chart = (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X(
                "time:T",
                title="Taiwan time (Asia/Taipei)",
                axis=alt.Axis(format="%m-%d %H:%M"),
            ),
            y=alt.Y("count:Q", title="Events per minute"),
            tooltip=[
                alt.Tooltip("time:T", title="Taiwan time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip("count:Q", title="Events"),
            ],
        )
        .properties(height=280)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)



def render_compare_tab(session_paths: list[Path]):
    frame = compare_sessions(session_paths)
    if frame.empty:
        st.info("No sessions selected for comparison.")
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
        "Peak viewers": "peak_viewers",
        "Average viewers": "average_viewers",
        "Observed likes": "likes_observed",
        "Current likes": "likes_current_total",
        "Diamonds": "total_diamonds",
        "Members": "members",
        "Follows": "follows",
        "Shares": "shares",
        "Subscribes": "subscribes",
        "Join rate / min": "join_rate_per_min",
        "Viewer growth": "viewer_growth",
        "Viewer volatility": "viewer_volatility",
    }
    label = st.selectbox("Comparison metric", list(metric_options), key="comparison_metric")
    column = metric_options[label]
    chart = alt.Chart(frame).mark_bar().encode(
        x=alt.X("session_id:N", sort="-y", title="Session"),
        y=alt.Y(f"{column}:Q", title=label),
        tooltip=[
            alt.Tooltip("session_id:N", title="Session"),
            alt.Tooltip(f"{column}:Q", title=label),
        ],
    ).properties(height=320)
    st.altair_chart(chart, use_container_width=True)


def render_gift_tab(session_paths: list[Path]):
    board = gift_leaderboard(session_paths)
    if board.empty:
        st.info("No counted gift events in the selected sessions.")
        return
    st.dataframe(board, use_container_width=True, hide_index=True)
    chart = alt.Chart(board.head(15)).mark_bar().encode(
        x=alt.X("diamonds:Q", title="Diamonds"),
        y=alt.Y("gifter:N", sort="-x", title="Gifter"),
        color=alt.Color("send_pattern:N", title="Send pattern"),
        tooltip=[
            "gifter", "diamonds", "items", "gift_events",
            "max_repeat_count", "send_pattern",
        ],
    ).properties(height=max(260, min(560, 24 * len(board.head(15)))))
    st.altair_chart(chart, use_container_width=True)
    details = gift_detail(session_paths)
    if not details.empty:
        st.subheader("Gift transactions")
        st.dataframe(
            details.sort_values("diamonds", ascending=False).head(100),
            use_container_width=True,
            hide_index=True,
        )


def render_traffic_social_tab(session_paths: list[Path]):
    st.subheader("Audience flow")
    audience = pd.DataFrame([audience_metrics(path) for path in session_paths])
    if audience.empty:
        st.info("No audience samples in the selected sessions.")
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
    st.subheader("Entry source")
    if traffic.empty:
        st.info("No member entry-source events in the selected sessions.")
    else:
        source_counts = (
            traffic.groupby("entry_source", dropna=False)
            .size()
            .reset_index(name="joins")
            .sort_values("joins", ascending=False)
        )
        st.dataframe(source_counts, use_container_width=True, hide_index=True)
        chart = alt.Chart(source_counts.head(20)).mark_bar().encode(
            x=alt.X("joins:Q", title="Join events"),
            y=alt.Y("entry_source:N", sort="-x", title="Entry source"),
            tooltip=["entry_source", "joins"],
        ).properties(height=max(260, min(560, 24 * len(source_counts.head(20)))))
        st.altair_chart(chart, use_container_width=True)

    st.subheader("Follow / Share / Subscribe")
    social = social_summary(session_paths)
    if social.empty:
        st.info("No follow, share, or subscribe events in the selected sessions.")
    else:
        social_counts = (
            social.groupby("action", dropna=False)
            .agg(events=("action", "size"), unique_users=("user", "nunique"))
            .reset_index()
            .sort_values("events", ascending=False)
        )
        st.dataframe(social_counts, use_container_width=True, hide_index=True)
        chart = alt.Chart(social_counts).mark_bar().encode(
            x=alt.X("action:N", title="Action"),
            y=alt.Y("events:Q", title="Events"),
            tooltip=["action", "events", "unique_users"],
        ).properties(height=280)
        st.altair_chart(chart, use_container_width=True)


def render_health_tab(session_paths: list[Path]):
    reports = [build_health_report(path) for path in session_paths]
    if not reports:
        st.info("No sessions selected for health monitoring.")
        return
    frame = pd.DataFrame(reports)
    columns = [
        "session_id", "status", "connection_count", "reconnect_count",
        "disconnect_count", "event_count", "raw_event_count",
        "socket_uptime_ratio", "socket_gap_seconds",
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
        return "Enter a streamer username first."

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
    return f"@{username} {action} to watchlist. Watcher will probe it on the next cycle."


def remove_streamer_from_watchlist(raw_username: str) -> str:
    username = raw_username.strip().lstrip("@").strip()
    if not username:
        return "Select a streamer first."

    config = read_json(CONFIG_PATH, {"streamers": []})
    streamers = config.setdefault("streamers", [])
    before = len(streamers)
    config["streamers"] = [
        item for item in streamers
        if str(item.get("username", "")).lstrip("@").strip() != username
    ]
    if len(config["streamers"]) == before:
        return f"@{username} is not in the watchlist."

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)
    return f"@{username} removed from watchlist. Captured data was kept."



def latest_live_session(username: str | None) -> Path | None:
    if not username:
        return None
    candidates = []
    for path in session_dirs(username):
        meta = read_json(path / "session.json", {})
        if meta.get("status") == "running":
            candidates.append((str(meta.get("collector_started_at_utc") or path.name), path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def render_live_tracking(session_dir: Path | None, metrics: dict, streamer_state: dict):
    if session_dir is None:
        st.info("No active tracking session.")
        return
    session = metrics.get("session") or {}
    st.caption(
        f"Live session: {session_dir.name} | status={session.get('status', 'unknown')} | "
        f"last event={session.get('last_received_at_local') or 'waiting'}"
    )
    cols = st.columns(6)
    cols[0].metric("Watch status", streamer_state.get("status", "unknown"))
    cols[1].metric("Current viewer", metrics["viewers"][-1] if metrics["viewers"] else "N/A")
    cols[2].metric("Current likes", metrics["like_current_total"] if metrics["like_current_total"] is not None else metrics["likes"])
    cols[3].metric("Current diamonds", metrics["diamonds"])
    cols[4].metric("Chat", metrics["chat"])
    cols[5].metric("Gift events", metrics["gifts"])
    if metrics["recent_activity"]:
        st.dataframe(metrics["recent_activity"], use_container_width=True, hide_index=True)
    else:
        st.info("Waiting for live events...")

def render_dashboard():
    config = read_json(CONFIG_PATH, {"streamers": []})
    state = read_json(WATCHER_STATE, {"streamers": {}})
    streamers = [
        str(item.get("username", "")).lstrip("@")
        for item in config.get("streamers", [])
        if item.get("username")
    ]
    states = state.get("streamers", {})

    st.title("TikTok LIVE Analytics")
    st.caption("All displayed timestamps use Taiwan time: Asia/Taipei.")

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
        st.header("Track a streamer")
        new_streamer = st.text_input(
            "Streamer username",
            placeholder="@username",
            key="new_streamer_username",
        )
        if st.button("Add / enable streamer", use_container_width=True):
            st.success(add_streamer_to_watchlist(new_streamer))
            st.rerun()

        configured_streamers = [value for value in streamers if value]
        remove_streamer = st.selectbox(
            "Remove streamer",
            options=configured_streamers or [""],
            format_func=lambda value: f"@{value}" if value else "No streamer",
            key="remove_streamer_username",
        )
        if st.button(
            "Remove from watchlist",
            disabled=not remove_streamer,
            use_container_width=True,
        ):
            st.warning(remove_streamer_from_watchlist(remove_streamer))
            st.rerun()

        st.divider()
        st.header("Session analysis")
        username = st.selectbox(
            "Streamer",
            options=streamers or [""],
            format_func=lambda value: f"@{value}" if value else "No streamer",
            key="analysis_username",
        )
        manual_id = st.text_input(
            "Session ID or room ID (optional)",
            placeholder="20260913_195928_chloe_o723_ or 7684970432562875157",
            help="Enter a folder Session ID or TikTok room_id to select the LIVE.",
            key="analysis_manual_id",
        )
        available = session_dirs(username) if username else []
        sessions_with_events = [
            path for path in available
            if (path / "events.ndjson").exists()
            and (path / "events.ndjson").stat().st_size > 0
        ]
        if sessions_with_events:
            available = sessions_with_events
        available_ids = [path.name for path in available]
        selected_id = st.selectbox(
            "Available sessions",
            options=available_ids or [""],
            index=0,
            key="analysis_selected_id",
            disabled=bool(manual_id.strip()),
        )
        comparison_ids = st.multiselect(
            "Compare sessions",
            options=available_ids,
            default=([selected_id] if selected_id else []),
            key="comparison_session_ids",
            help="Select multiple sessions for trend, Gift, traffic, and social comparisons.",
        )
        if manual_id.strip():
            st.caption("Manual Session ID overrides the dropdown.")
        st.caption(f"Raw data: {RAW_ROOT}")

    if not username:
        st.warning("No enabled streamer configured.")
        return

    session_dir = resolve_session(manual_id, username, selected_id)
    if manual_id.strip() and session_dir is None:
        st.error(f"Session ID / room ID not found: {manual_id.strip()}")
        return
    metrics = load_session(session_dir)
    session = metrics["session"] or {}
    streamer_state = states.get(username, {})
    tracking_dir = latest_live_session(username)
    tracking_metrics = load_session(tracking_dir) if tracking_dir else metrics
    display_metrics = tracking_metrics if tracking_dir and not manual_id.strip() else metrics
    comparison_paths = [
        RAW_ROOT / session_id
        for session_id in comparison_ids
        if session_id and (RAW_ROOT / session_id).is_dir()
    ]
    if session_dir and session_dir not in comparison_paths:
        comparison_paths.insert(0, session_dir)

    st.subheader(f"@{username}")
    if session_dir:
        st.caption(
            f"Session ID: {session_dir.name} | "
            f"status={session.get('status', 'unknown')} | "
            f"session timezone={session.get('timezone', TAIPEI_TZ)}"
        )
    else:
        st.info("No captured session is available for this streamer.")
        return

    cols = st.columns(8)
    cols[0].metric("Watch status", streamer_state.get("status", "unknown"))
    cols[1].metric("Current viewer", display_metrics["viewers"][-1] if display_metrics["viewers"] else "N/A")
    cols[2].metric("Peak viewer", max(display_metrics["viewers"]) if display_metrics["viewers"] else "N/A")
    cols[3].metric("Chat", display_metrics["chat"])
    cols[4].metric("Current likes", display_metrics["like_current_total"] if display_metrics["like_current_total"] is not None else display_metrics["likes"])
    cols[5].metric("Observed likes", display_metrics["likes_observed"])
    cols[6].metric("Current diamonds", display_metrics["diamonds"])
    cols[7].metric("Members", display_metrics["joins"])
    if display_metrics["like_current_total"] is not None:
        st.caption(
            "Like total uses TikTok totalLikes; observed likes is the batch increment "
            f"captured by this collector. Estimated pre-capture likes: {display_metrics['like_baseline_estimate']}."
        )

    tab_tracking, tab_overview, tab_chat, tab_compare, tab_gifts, tab_traffic, tab_health, tab_quality = st.tabs(
        [
            "Live tracking", "Trends", "Recent chat", "Multi-session",
            "Gifts", "Traffic & social", "System health", "Data quality",
        ]
    )
    with tab_tracking:
        render_live_tracking(tracking_dir, tracking_metrics, streamer_state)

    with tab_overview:
        st.subheader("Viewer trend")
        render_viewer_chart(metrics["viewer_rows"])
        st.subheader("Activity trend")
        render_activity_chart(metrics["activity_rows"])
        st.write({"event_counts": dict(metrics["counts"])})

    with tab_chat:
        if metrics["recent_chat"]:
            st.dataframe(
                metrics["recent_chat"],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No chat messages in this session.")

    with tab_compare:
        render_compare_tab(comparison_paths)

    with tab_gifts:
        render_gift_tab(comparison_paths)

    with tab_traffic:
        render_traffic_social_tab(comparison_paths)

    with tab_health:
        render_health_tab(comparison_paths)

    with tab_quality:
        quality = session.get("data_quality", {})
        st.json({
            "session_id": session.get("session_id", session_dir.name),
            "status": session.get("status"),
            "collector_started_at_local": session.get("collector_started_at_local"),
            "collector_ended_at_local": session.get("collector_ended_at_local"),
            "last_received_at_local": session.get("last_received_at_local") or local_timestamp(session.get("last_received_at_utc")) ,
            "socket_uptime_ratio": quality.get("socket_uptime_ratio"),
            "socket_gap_seconds": quality.get("socket_gap_seconds"),
            "duplicate_events_dropped": quality.get("duplicate_events_dropped"),
            "sdk_error_events": quality.get("sdk_error_events"),
            "last_probe_error": streamer_state.get("last_probe_error"),
        })

    st.divider()
    st.subheader("Watcher log (latest)")
    try:
        log_lines = WATCHER_LOG.read_text(encoding="utf-8").splitlines()
        st.code("\n".join(log_lines[-80:]) or "(empty)")
    except OSError:
        st.info("Watcher log is not available.")


if hasattr(st, "fragment"):
    @st.fragment(run_every=5)
    def live_dashboard():
        render_dashboard()

    live_dashboard()
else:
    render_dashboard()
