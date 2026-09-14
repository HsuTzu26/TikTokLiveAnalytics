from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import pstdev
from zoneinfo import ZoneInfo

import pandas as pd

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def iter_events(session_dir: Path):
    path = session_dir / "events.ndjson"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def event_time_ms(event):
    value = event.get("timestamp_ms") or event.get("received_at_ms")
    return value if isinstance(value, (int, float)) else None


def iso_local(timestamp_ms):
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(TAIPEI_TZ).isoformat()


def user_id(event):
    return str(event.get("unique_id") or event.get("user_id") or event.get("nickname") or "unknown")


def audience_metrics(session_dir: Path):
    events = list(iter_events(session_dir) or [])
    viewer_rows = [
        (event_time_ms(event), float(event["viewer_count"]))
        for event in events
        if event.get("type") == "viewer"
        and isinstance(event.get("viewer_count"), (int, float))
        and event_time_ms(event) is not None
    ]
    member_times = [event_time_ms(event) for event in events if event.get("type") == "member" and event_time_ms(event) is not None]
    timestamps = [event_time_ms(event) for event in events if event_time_ms(event) is not None]
    if not timestamps:
        return {
            "session_id": session_dir.name,
            "join_rate_per_min": None,
            "viewer_growth": None,
            "viewer_volatility": None,
            "early_avg_viewers": None,
            "mid_avg_viewers": None,
            "late_avg_viewers": None,
            "early_joins": 0,
            "mid_joins": 0,
            "late_joins": 0,
        }
    start, end = min(timestamps), max(timestamps)
    duration_ms = max(1, end - start)
    duration_min = duration_ms / 60000
    values = [value for _, value in viewer_rows]
    average = sum(values) / len(values) if values else None
    phase_viewers = {"early": [], "mid": [], "late": []}
    phase_joins = {"early": 0, "mid": 0, "late": 0}
    for timestamp, value in viewer_rows:
        ratio = (timestamp - start) / duration_ms
        phase = "early" if ratio < 1 / 3 else ("mid" if ratio < 2 / 3 else "late")
        phase_viewers[phase].append(value)
    for timestamp in member_times:
        ratio = (timestamp - start) / duration_ms
        phase = "early" if ratio < 1 / 3 else ("mid" if ratio < 2 / 3 else "late")
        phase_joins[phase] += 1
    return {
        "session_id": session_dir.name,
        "join_rate_per_min": round(len(member_times) / duration_min, 3),
        "viewer_growth": round(values[-1] - values[0], 3) if len(values) >= 2 else None,
        "viewer_volatility": round(pstdev(values) / average, 4) if values and average else None,
        "early_avg_viewers": round(sum(phase_viewers["early"]) / len(phase_viewers["early"]), 2) if phase_viewers["early"] else None,
        "mid_avg_viewers": round(sum(phase_viewers["mid"]) / len(phase_viewers["mid"]), 2) if phase_viewers["mid"] else None,
        "late_avg_viewers": round(sum(phase_viewers["late"]) / len(phase_viewers["late"]), 2) if phase_viewers["late"] else None,
        "early_joins": phase_joins["early"],
        "mid_joins": phase_joins["mid"],
        "late_joins": phase_joins["late"],
    }


def session_summary(session_dir: Path):
    meta = read_json(session_dir / "session.json", {}) or {}
    events = list(iter_events(session_dir) or [])
    timestamps = [value for event in events if (value := event_time_ms(event)) is not None]
    viewers = [event.get("viewer_count") for event in events if event.get("type") == "viewer" and isinstance(event.get("viewer_count"), (int, float))]
    chats = [event for event in events if event.get("type") == "chat"]
    gifts = [event for event in events if event.get("type") == "gift" and event.get("counted")]
    likes = [event for event in events if event.get("type") == "like"]
    members = [event for event in events if event.get("type") == "member"]
    social = [event for event in events if event.get("type") == "social"]
    subscribes = [event for event in events if event.get("type") == "subscribe"]
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    diamonds = sum(float(event.get("diamond_total") or 0) for event in gifts)
    likes_received = sum(int(event.get("like_count") or 0) for event in likes)
    like_totals = [int(event["total_likes"]) for event in likes if isinstance(event.get("total_likes"), (int, float))]
    likes_current_total = max(like_totals) if like_totals else None
    likes_first_total = like_totals[0] if like_totals else None
    likes_baseline_estimate = max(0, likes_first_total - int(likes[0].get("like_count") or 0)) if like_totals else None
    actions = Counter(str(event.get("social_action") or "unknown") for event in social)
    return {
        "session_id": str(meta.get("session_id") or session_dir.name),
        "session_dir": str(session_dir),
        "username": str(meta.get("username") or ""),
        "status": meta.get("status"),
        "timezone": meta.get("timezone", "Asia/Taipei"),
        "first_event_local": iso_local(start),
        "last_event_local": iso_local(end),
        "observed_duration_seconds": round((end - start) / 1000, 3) if start is not None and end is not None else None,
        "event_count": len(events),
        "viewer_samples": len(viewers),
        "peak_viewers": max(viewers) if viewers else None,
        "average_viewers": round(sum(viewers) / len(viewers), 2) if viewers else None,
        "median_viewers": float(pd.Series(viewers).median()) if viewers else None,
        "min_viewers": min(viewers) if viewers else None,
        "chat_messages": len(chats),
        "unique_chatters": len({user_id(event) for event in chats}),
        "like_events": len(likes),
        "likes_observed": likes_received,
        "likes_received": likes_received,
        "likes_current_total": likes_current_total,
        "likes_baseline_estimate": likes_baseline_estimate,
        "gift_events": len(gifts),
        "unique_gifters": len({user_id(event) for event in gifts}),
        "total_diamonds": diamonds,
        "members": len(members),
        "follows": actions.get("follow", 0),
        "shares": actions.get("share", 0),
        "social_events": len(social),
        "subscribes": len(subscribes),
        "room_id": meta.get("room_id"),
        **{key: value for key, value in audience_metrics(session_dir).items() if key != "session_id"},
    }


def compare_sessions(session_dirs: list[Path]):
    rows = [session_summary(path) for path in session_dirs]
    return pd.DataFrame(rows)


def gift_leaderboard(session_dirs: list[Path]):
    rows = []
    for session_dir in session_dirs:
        session_id = session_dir.name
        for event in iter_events(session_dir) or []:
            if event.get("type") != "gift" or not event.get("counted"):
                continue
            repeat_count = int(event.get("repeat_count") or 1)
            rows.append({
                "session_id": session_id,
                "time": event.get("timestamp_local") or event.get("received_at_local"),
                "gifter": user_id(event),
                "gift_name": event.get("gift_name") or "unknown",
                "gift_id": event.get("gift_id"),
                "gift_events": 1,
                "items": repeat_count,
                "diamonds": float(event.get("diamond_total") or 0),
                "repeat_count": repeat_count,
                "transaction_id": event.get("transaction_id"),
            })
    if not rows:
        return pd.DataFrame(columns=["gifter", "gift_events", "items", "diamonds", "unique_gifts", "max_repeat_count", "repeated_events", "max_transaction_diamonds", "sessions", "send_pattern"])
    frame = pd.DataFrame(rows)
    grouped = frame.groupby("gifter", dropna=False)
    result = grouped.agg(
        gift_events=("gift_events", "sum"),
        items=("items", "sum"),
        diamonds=("diamonds", "sum"),
        unique_gifts=("gift_name", "nunique"),
        max_repeat_count=("repeat_count", "max"),
        repeated_events=("repeat_count", lambda values: int((values > 1).sum())),
        max_transaction_diamonds=("diamonds", "max"),
        sessions=("session_id", "nunique"),
    ).reset_index()
    result["send_pattern"] = result.apply(
        lambda row: "mixed" if row["repeated_events"] and row["gift_events"] > row["repeated_events"] else ("repeat_streak" if row["repeated_events"] else "single_send"),
        axis=1,
    )
    return result.sort_values(["diamonds", "items"], ascending=False).reset_index(drop=True)


def gift_detail(session_dirs: list[Path]):
    rows = []
    for session_dir in session_dirs:
        for event in iter_events(session_dir) or []:
            if event.get("type") == "gift" and event.get("counted"):
                rows.append({
                    "session_id": session_dir.name,
                    "time": event.get("timestamp_local") or event.get("received_at_local"),
                    "gifter": user_id(event),
                    "gift_name": event.get("gift_name") or "unknown",
                    "items": int(event.get("repeat_count") or 1),
                    "diamonds": float(event.get("diamond_total") or 0),
                    "transaction_id": event.get("transaction_id"),
                })
    return pd.DataFrame(rows)


def gift_concentration(session_dirs: list[Path]):
    board = gift_leaderboard(session_dirs)
    if board.empty:
        return {
            "gifter_count": 0,
            "gift_events": 0,
            "total_diamonds": 0.0,
            "top1_share": None,
            "top5_share": None,
            "top10_share": None,
            "peak_minute": None,
            "peak_minute_diamonds": 0.0,
        }
    total = float(board["diamonds"].sum())
    details = gift_detail(session_dirs)
    peak_minute = None
    peak_diamonds = 0.0
    if not details.empty and details["time"].notna().any():
        times = pd.to_datetime(details["time"], format="mixed", utc=True, errors="coerce")
        details = details.assign(_time=times).dropna(subset=["_time"])
        if not details.empty:
            minute = details.set_index("_time").resample("1min")["diamonds"].sum()
            if not minute.empty:
                peak_timestamp = minute.idxmax()
                peak_minute = peak_timestamp.tz_convert(TAIPEI_TZ).isoformat()
                peak_diamonds = float(minute.max())
    return {
        "gifter_count": int(len(board)),
        "gift_events": int(board["gift_events"].sum()),
        "total_diamonds": total,
        "top1_share": round(float(board.head(1)["diamonds"].sum()) / total, 4) if total else None,
        "top5_share": round(float(board.head(5)["diamonds"].sum()) / total, 4) if total else None,
        "top10_share": round(float(board.head(10)["diamonds"].sum()) / total, 4) if total else None,
        "peak_minute": peak_minute,
        "peak_minute_diamonds": peak_diamonds,
    }


def gift_activity_by_minute(session_dirs: list[Path]):
    rows = []
    for session_dir in session_dirs:
        for event in iter_events(session_dir) or []:
            timestamp = event_time_ms(event)
            if timestamp is None:
                continue
            event_type = event.get("type")
            if event_type == "gift" and event.get("counted"):
                rows.append({
                    "session_id": session_dir.name,
                    "timestamp_ms": timestamp,
                    "diamonds": float(event.get("diamond_total") or 0),
                    "gift_events": 1,
                    "chat_messages": 0,
                    "viewer_count": None,
                })
            elif event_type == "chat":
                rows.append({
                    "session_id": session_dir.name,
                    "timestamp_ms": timestamp,
                    "diamonds": 0.0,
                    "gift_events": 0,
                    "chat_messages": 1,
                    "viewer_count": None,
                })
            elif event_type == "viewer" and isinstance(event.get("viewer_count"), (int, float)):
                rows.append({
                    "session_id": session_dir.name,
                    "timestamp_ms": timestamp,
                    "diamonds": 0.0,
                    "gift_events": 0,
                    "chat_messages": 0,
                    "viewer_count": float(event["viewer_count"]),
                })
    if not rows:
        return pd.DataFrame(columns=["session_id", "time", "diamonds", "gift_events", "chat_messages", "viewer_count"])
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(TAIPEI_TZ).dt.floor("min")
    grouped = frame.groupby(["session_id", "time"], as_index=False).agg(
        diamonds=("diamonds", "sum"),
        gift_events=("gift_events", "sum"),
        chat_messages=("chat_messages", "sum"),
        viewer_count=("viewer_count", "mean"),
    )
    return grouped.sort_values(["time", "session_id"]).reset_index(drop=True)


def traffic_sources(session_dirs: list[Path]):
    rows = []
    for session_dir in session_dirs:
        for event in iter_events(session_dir) or []:
            if event.get("type") == "member":
                rows.append({
                    "session_id": session_dir.name,
                    "time": event.get("timestamp_local") or event.get("received_at_local"),
                    "user": user_id(event),
                    "entry_source": event.get("entry_source") or "unknown",
                    "entry_action": event.get("entry_action") or "unknown",
                    "entry_type": event.get("entry_type") or "unknown",
                })
    return pd.DataFrame(rows)


def social_summary(session_dirs: list[Path]):
    rows = []
    for session_dir in session_dirs:
        for event in iter_events(session_dir) or []:
            event_type = event.get("type")
            if event_type == "social":
                action = event.get("social_action") or "unknown"
            elif event_type == "subscribe":
                action = "subscribe"
            else:
                continue
            rows.append({
                "session_id": session_dir.name,
                "time": event.get("timestamp_local") or event.get("received_at_local"),
                "action": action,
                "user": user_id(event),
            })
    return pd.DataFrame(rows)


def social_activity_by_minute(session_dirs: list[Path]):
    """Return Taiwan-time social and audience activity grouped by minute."""
    rows = []
    for session_dir in session_dirs:
        for event in iter_events(session_dir) or []:
            timestamp = event_time_ms(event)
            if timestamp is None:
                continue
            event_type = event.get("type")
            row = {
                "session_id": session_dir.name, "timestamp_ms": timestamp,
                "follows": 0, "shares": 0, "subscribes": 0,
                "joins": 0, "viewer_count": None,
            }
            if event_type == "social":
                action = str(event.get("social_action") or "unknown")
                if action == "follow":
                    row["follows"] = 1
                elif action == "share":
                    row["shares"] = 1
                else:
                    continue
            elif event_type == "subscribe":
                row["subscribes"] = 1
            elif event_type == "member":
                row["joins"] = 1
            elif event_type == "viewer" and isinstance(event.get("viewer_count"), (int, float)):
                row["viewer_count"] = float(event["viewer_count"])
            else:
                continue
            rows.append(row)
    columns = ["session_id", "time", "follows", "shares", "subscribes", "joins", "viewer_count"]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(TAIPEI_TZ).dt.floor("min")
    grouped = frame.groupby(["session_id", "time"], as_index=False).agg(
        follows=("follows", "sum"), shares=("shares", "sum"),
        subscribes=("subscribes", "sum"), joins=("joins", "sum"),
        viewer_count=("viewer_count", "mean"),
    )
    return grouped[columns].sort_values(["time", "session_id"]).reset_index(drop=True)


def social_conversion_proxies(session_dirs: list[Path]):
    """Summarize observed actions; these rates are not causal conversions."""
    rows = []
    for session_dir in session_dirs:
        events = list(iter_events(session_dir) or [])
        timestamps = [value for event in events if (value := event_time_ms(event)) is not None]
        duration_minutes = max((max(timestamps) - min(timestamps)) / 60000, 1 / 60) if timestamps else None
        joins = sum(event.get("type") == "member" for event in events)
        actions = {"follow": [], "share": [], "subscribe": []}
        for event in events:
            if event.get("type") == "social":
                action = str(event.get("social_action") or "unknown")
            elif event.get("type") == "subscribe":
                action = "subscribe"
            else:
                continue
            if action in actions:
                actions[action].append(event)
        row = {"session_id": session_dir.name, "observed_minutes": round(duration_minutes, 2) if duration_minutes else None, "joins": joins}
        for action, action_events in actions.items():
            count = len(action_events)
            row[f"{action}s"] = count
            row[f"unique_{action}_users"] = len({user_id(event) for event in action_events})
            row[f"{action}_rate_per_min"] = round(count / duration_minutes, 3) if duration_minutes else None
            row[f"{action}_per_100_joins_proxy"] = round(count * 100 / joins, 2) if joins else None
        rows.append(row)
    return pd.DataFrame(rows)


def build_health_report(session_dir: Path):
    meta = read_json(session_dir / "session.json", {}) or {}
    event_count = 0
    event_types = Counter()
    for event in iter_events(session_dir) or []:
        event_count += 1
        event_types[str(event.get("type") or "unknown")] += 1
    diagnostic_kinds = Counter()
    diagnostics = session_dir / "diagnostics.ndjson"
    if diagnostics.exists():
        with diagnostics.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                diagnostic_kinds[str(row.get("kind") or row.get("event_type") or "unknown")] += 1
    quality = meta.get("data_quality") or {}
    return {
        "session_id": meta.get("session_id") or session_dir.name,
        "username": meta.get("username"),
        "status": meta.get("status"),
        "checked_at_local": datetime.now(TAIPEI_TZ).isoformat(),
        "collector_started_at_local": meta.get("collector_started_at_local"),
        "collector_ended_at_local": meta.get("collector_ended_at_local"),
        "last_received_at_local": meta.get("last_received_at_local"),
        "connection_count": meta.get("connection_count"),
        "reconnect_count": meta.get("reconnect_count"),
        "disconnect_count": meta.get("disconnect_count"),
        "event_count": event_count,
        "raw_event_count": meta.get("raw_event_count"),
        "event_types": dict(event_types),
        "diagnostic_kinds": dict(diagnostic_kinds),
        "socket_uptime_ratio": quality.get("socket_uptime_ratio"),
        "socket_gap_seconds": quality.get("socket_gap_seconds"),
        "duplicate_events_dropped": quality.get("duplicate_events_dropped"),
        "sdk_error_events": quality.get("sdk_error_events"),
        "unknown_event_count": diagnostic_kinds.get("unknown_event", 0) + diagnostic_kinds.get("unknown", 0),
        "offline_confirmed": meta.get("offline_confirmed"),
        "live_end_detected": meta.get("live_end_detected"),
    }


def write_health_report(session_dir: Path):
    report = build_health_report(session_dir)
    target = session_dir / "health.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
