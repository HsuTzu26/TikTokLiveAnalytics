from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import importlib.util
from collections import Counter, defaultdict, deque
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

analytics_module = importlib.reload(analytics_module)
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


def start_watcher() -> str:
    pid = watcher_pid()
    if pid_is_running(pid):
        return tr(f"Watcher already running (PID {pid}).", f"Watcher 已在執行中（PID {pid}）。")

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


def session_has_analytics(path: Path) -> bool:
    """Return quickly once a timestamped non-system event is found."""
    events_path = path / "events.ndjson"
    try:
        with events_path.open("r", encoding="utf-8", errors="replace") as rows:
            for line in rows:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") not in {None, "system"} and isinstance(event.get("timestamp_ms"), (int, float)):
                    return True
    except OSError:
        return False
    return False


def session_label(path: Path) -> str:
    meta = read_json(path / "session.json", {})
    status = str(meta.get("status") or "unknown")
    kind = "analytics" if session_has_analytics(path) else "system only"
    return f"{path.name}  |  {status}  |  {kind}"


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
        "shares": 0,
        "subscribes": 0,
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
            elif event_type == "social":
                action = event.get("social_action")
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

    summary["recent_chat"] = summary["recent_chat"][-200:][::-1]
    summary["recent_activity"] = summary["recent_activity"][-50:][::-1]
    return summary


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
    combined["viewer_rows"].sort(key=lambda row: str(row.get("time") or ""))
    combined["activity_rows"].sort(key=lambda row: str(row.get("time") or ""))
    combined["recent_chat"] = sorted(combined["recent_chat"], key=lambda row: str(row.get("time") or ""), reverse=True)[:200]
    combined["recent_activity"] = sorted(combined["recent_activity"], key=lambda row: str(row.get("time") or ""), reverse=True)[:50]
    combined["session"] = read_json(preferred / "session.json", {}) if preferred else (fragments[-1]["session"] if fragments else None)
    combined["fragment_count"] = len(paths)
    return combined


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
        meta = read_json(path / "session.json", {})
        if meta.get("status") == "running":
            candidates.append((str(meta.get("collector_started_at_utc") or path.name), path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


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
    cols[0].metric(tr("Watch status", "監控狀態"), streamer_state.get("status", "unknown"))
    cols[1].metric(tr("Current viewer", "目前觀眾數"), metrics["viewers"][-1] if metrics["viewers"] else "N/A")
    cols[2].metric(tr("Current likes", "目前按讚數"), metrics["like_current_total"] if metrics["like_current_total"] is not None else metrics["likes"])
    cols[3].metric(tr("Captured diamonds (room)", "直播間紀錄 Diamonds"), int(metrics["diamonds"]))
    cols[4].metric(tr("Chat", "聊天"), metrics["chat"])
    cols[5].metric(tr("Gift events (room)", "直播間禮物事件"), metrics["gifts"])
    if metrics["recent_activity"]:
        st.dataframe(metrics["recent_activity"], use_container_width=True, hide_index=True)
    else:
        st.info(tr("Waiting for live events...", "等待即時事件..."))

def render_provider_benchmark():
    from src.provider_benchmark_report import analyze_session

    st.title(tr("Provider Benchmark", "Provider Benchmark 測試"))
    st.caption(
        tr(
            "Read-only view of active and completed isolated provider sessions. This page does not start a Watcher or another LIVE connection.",
            "唯讀查看進行中或已完成的 Provider session。本頁不會啟動 Watcher，也不會建立額外的 LIVE 連線。",
        )
    )
    paths = benchmark_session_dirs()
    if not paths:
        st.info(tr("No provider benchmark session with recorded events was found.", "找不到含有事件紀錄的 Provider session。"))
        return

    labels = [f"{path.parent.name} · {path.name}" for path in paths]
    selected_label = st.selectbox(
        tr("Benchmark session", "Benchmark 場次"),
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
    metric_cols = st.columns(5)
    metric_cols[0].metric(tr("Viewer samples", "觀眾採樣"), report["viewer_sample_count"])
    metric_cols[1].metric(tr("Chat messages", "聊天訊息"), report["event_counts"].get("chat", 0))
    metric_cols[2].metric(tr("Gift events", "禮物事件"), report["gift_count"])
    metric_cols[3].metric(tr("Diamonds observed", "紀錄到的 Diamonds"), report["gift_diamonds_observed"])
    metric_cols[4].metric(
        tr("Connections / disconnects", "連線／中斷"),
        f"{report.get('connection_count') or 0} / {report.get('disconnect_count') or 0}",
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


def render_dashboard():
    config = read_json(CONFIG_PATH, {"streamers": []})
    state = read_json(WATCHER_STATE, {"streamers": {}})
    streamers = [
        str(item.get("username", "")).lstrip("@")
        for item in config.get("streamers", [])
        if item.get("username")
    ]
    states = state.get("streamers", {})

    st.title(tr("TikTok LIVE Analytics", "TikTok 直播分析"))
    st.caption(
        tr(
            f"All displayed timestamps use Taiwan time: Asia/Taipei. Live data refreshes every {LIVE_REFRESH_SECONDS} seconds.",
            f"所有時間皆為台灣時間（Asia/Taipei）；即時資料每 {LIVE_REFRESH_SECONDS} 秒更新。",
        )
    )
    quota_until = state.get("quota_pause_until_utc")
    if quota_until:
        quota_time = pd.to_datetime(quota_until, utc=True, errors="coerce")
        if pd.notna(quota_time) and quota_time > pd.Timestamp.now(tz="UTC"):
            st.warning(
                tr(
                    "TikTool quota cooldown: new LIVE checks and Collector starts are paused until ",
                    "TikTool 配額冷卻中：暫停新直播探測與 Collector 啟動至 ",
                )
                + quota_time.tz_convert(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")
                + f" {tr('Taiwan time', '台灣時間')}"
                + tr(". Existing collection connections are unaffected.", "。已正常運作的收集連線不受影響。")
            )

    with st.sidebar:
        st.header(tr("Watch control", "監控控制"))
        pid = watcher_pid()
        running = pid_is_running(pid)
        benchmark_collecting = bool(benchmark_session_dirs())
        watcher_sdk_available = importlib.util.find_spec("tiktok_live_events") is not None
        st.write(f"Watcher：**{tr('RUNNING', '執行中') if running else tr('STOPPED', '已停止')}**")
        if pid:
            st.caption(f"PID: {pid}")
        if benchmark_collecting:
            st.caption(
                tr("Provider Benchmark is collecting. Use its page; starting the legacy Watcher would open another TikTok connection.", "Provider Benchmark 正在收集。請使用該頁面；啟動舊版 Watcher 會建立另一條 TikTok 連線。")
            )
        elif not watcher_sdk_available:
            st.caption(
                tr("The legacy Watcher cannot start because tiktok_live_events is not installed in this Streamlit environment.", "舊版 Watcher 無法啟動：目前 Streamlit 環境未安裝 tiktok_live_events。")
            )
        if st.button(
            tr("Start watcher", "啟動 Watcher"),
            disabled=running or benchmark_collecting or not watcher_sdk_available,
            use_container_width=True,
        ):
            st.success(start_watcher())
            st.rerun()
        if st.button(tr("Stop watcher", "停止 Watcher"), disabled=not running, use_container_width=True):
            st.warning(stop_watcher())
            st.rerun()

        st.divider()
        st.header(tr("Track a streamer", "管理直播主"))
        new_streamer = st.text_input(
            tr("Streamer username", "直播主帳號"),
            placeholder="@username",
            key="new_streamer_username",
        )
        if st.button(tr("Add / enable streamer", "新增／啟用直播主"), use_container_width=True):
            st.success(add_streamer_to_watchlist(new_streamer))
            st.rerun()

        configured_streamers = [value for value in streamers if value]
        remove_streamer = st.selectbox(
            tr("Remove streamer", "移除直播主"),
            options=configured_streamers or [""],
            format_func=lambda value: f"@{value}" if value else tr("No streamer", "沒有直播主"),
            key="remove_streamer_username",
        )
        if st.button(
            tr("Remove from watchlist", "從追蹤清單移除"),
            disabled=not remove_streamer,
            use_container_width=True,
        ):
            st.warning(remove_streamer_from_watchlist(remove_streamer))
            st.rerun()

        st.divider()
        st.header(tr("Session analysis", "場次分析"))
        username = st.selectbox(
            tr("Streamer", "直播主"),
            options=streamers or [""],
            format_func=lambda value: f"@{value}" if value else tr("No streamer", "沒有直播主"),
            key="analysis_username",
        )
        manual_id = st.text_input(
            tr("Session ID or room ID (optional)", "Session ID 或直播間 ID（選填）"),
            placeholder="20260913_195928_chloe_o723_ or 7684970432562875157",
            help=tr("Enter a folder Session ID or TikTok room_id to select the LIVE.", "輸入資料夾 Session ID 或 TikTok room_id 來選擇直播場次。"),
            key="analysis_manual_id",
        )
        available = session_dirs(username) if username else []
        analytics_sessions = [path for path in available if session_has_analytics(path)]
        if analytics_sessions:
            available = analytics_sessions
        available_ids = [path.name for path in available]
        labels = {path.name: session_label(path) for path in available}
        selected_id = st.selectbox(
            tr("Available sessions", "可用場次"),
            options=available_ids or [""],
            index=0,
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
        st.caption(f"{tr('Raw data', '原始資料')}：{RAW_ROOT}")

    if not username:
        st.warning(tr("No enabled streamer configured.", "尚未設定已啟用的直播主。"))
        return

    session_dir = resolve_session(manual_id, username, selected_id)
    if manual_id.strip() and session_dir is None:
        st.error(f"{tr('Session ID / room ID not found', '找不到 Session ID／直播間 ID')}：{manual_id.strip()}")
        return
    metrics = load_session(session_dir)
    session = metrics["session"] or {}
    streamer_state = states.get(username, {})
    tracking_dir = latest_live_session(username)
    tracking_room_id = (read_json(tracking_dir / "session.json", {}) or {}).get("room_id") if tracking_dir else None
    tracking_fragments = room_session_dirs(username, tracking_room_id) if tracking_dir else []
    tracking_metrics = combine_session_metrics(tracking_fragments, tracking_dir) if tracking_fragments else metrics
    selected_room_id = session.get("room_id")
    selected_fragments = room_session_dirs(username, selected_room_id)
    selected_room_metrics = combine_session_metrics(selected_fragments, session_dir) if selected_fragments else metrics
    if tracking_dir and not manual_id.strip():
        display_metrics = tracking_metrics
    else:
        display_metrics = selected_room_metrics
    comparison_paths = [
        RAW_ROOT / session_id
        for session_id in comparison_ids
        if session_id and (RAW_ROOT / session_id).is_dir()
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

    if tracking_dir:
        st.success(f"{tr('LIVE collection active', '直播收集中')} - {tracking_dir.name}")
    elif metrics["viewer_rows"]:
        st.info(tr("Historical session analysis - no active LIVE collector for this streamer.", "歷史場次分析；此直播主目前沒有運作中的 LIVE Collector。"))
    else:
        st.warning(tr("This session contains system logs only. Select a session labeled analytics to display trends.", "此場次只有系統紀錄。請選擇標示為 analytics 的場次查看趨勢。"))

    st.markdown(f"#### {tr('Audience', '觀眾概況')}")
    audience_cols = st.columns(4)
    audience_cols[0].metric(tr("Tracking status", "追蹤狀態"), streamer_state.get("status", "unknown"))
    audience_cols[1].metric(tr("Current viewers", "目前觀眾數"), display_metrics["viewers"][-1] if display_metrics["viewers"] else "N/A")
    audience_cols[2].metric(tr("Peak viewers", "最高觀眾數"), max(display_metrics["viewers"]) if display_metrics["viewers"] else "N/A")
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

    tab_tracking, tab_overview, tab_chat, tab_compare, tab_gifts, tab_traffic, tab_snapshots, tab_health, tab_quality = st.tabs(
        [
            tr("Live tracking", "即時監控"), tr("Trends", "趨勢"), tr("Recent chat", "最新聊天"), tr("Multi-session", "多場次比較"),
            tr("Gifts", "禮物"), tr("Traffic & social", "人流與社群"), tr("Snapshots & rankings", "快照與排行"), tr("System health", "系統健康狀態"), tr("Data quality", "資料品質"),
        ]
    )
    with tab_tracking:
        render_live_tracking(tracking_dir, tracking_metrics, streamer_state)

    with tab_overview:
        if not metrics["viewer_rows"] and not metrics["activity_rows"]:
            st.warning(tr("No timestamped analytics events are available in this session.", "此場次沒有含時間戳記的分析事件。"))
        else:
            trend_cols = st.columns(3)
            trend_cols[0].metric(tr("Viewer samples", "觀眾採樣"), len(metrics["viewer_rows"]))
            trend_cols[1].metric(tr("Captured events", "捕獲事件"), sum(metrics["counts"].values()))
            trend_cols[2].metric(tr("Event categories", "事件類別"), len([value for value in metrics["counts"].values() if value]))
        st.subheader(tr("Viewer trend", "觀眾趨勢"))
        render_viewer_chart(metrics["viewer_rows"])
        st.subheader(tr("Activity trend by event type", "依事件類型查看互動趨勢"))
        render_activity_chart(metrics["activity_rows"])
        counts_frame = pd.DataFrame(
            [{"event_type": key, "events": value} for key, value in metrics["counts"].most_common()]
        )
        if not counts_frame.empty:
            st.subheader(tr("Captured event breakdown", "捕獲事件統計"))
            st.dataframe(counts_frame, use_container_width=True, hide_index=True)

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

    with tab_compare:
        render_compare_tab(comparison_paths)

    with tab_gifts:
        render_gift_tab(comparison_paths)

    with tab_traffic:
        render_traffic_social_tab(comparison_paths)

    with tab_snapshots:
        render_snapshots_tab(comparison_paths)

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
    log_header, log_control = st.columns([4, 1])
    log_header.subheader(tr("Watcher log (latest)", "Watcher 最新紀錄"))
    log_limit = log_control.number_input(
        tr("Lines", "顯示行數"),
        min_value=1,
        max_value=1000,
        value=10,
        step=10,
        key="watcher_log_line_limit",
    )
    try:
        log_lines = read_tail_lines(WATCHER_LOG, log_limit)
        st.code("\n".join(log_lines) or "(empty)")
    except OSError:
        st.info(tr("Watcher log is not available.", "無法讀取 Watcher 紀錄。"))


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
    from app.report_runtime import load_reports_ui
    load_reports_ui().render_reports(RAW_ROOT, streamers)


def render_chat_sender_page():
    from app.chat_sender_ui import render_chat_sender
    render_chat_sender()


render_language_selector()

pages = {
    tr("Live", "直播"): [
        st.Page(render_dashboard_page, title=tr("Dashboard", "儀表板"), default=True),
        st.Page(
            render_provider_benchmark_page,
            title=tr("Provider Benchmark", "Provider Benchmark 測試"),
            url_path="provider-benchmark",
        ),
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
