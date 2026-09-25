"""Summarize a compact provider benchmark session without reading raw payloads."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from statistics import median
from typing import Any, Iterator


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _received_ms(row: dict[str, Any]) -> int | float | None:
    value = row.get("received_at_ms")
    if not isinstance(value, (int, float)):
        value = row.get("timestamp_ms")
    return value if isinstance(value, (int, float)) else None


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _coverage(rows: list[dict[str, Any]], field: str) -> float | None:
    return _ratio(sum(row.get(field) not in (None, "") for row in rows), len(rows))


def _resource_summary(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            samples = list(csv.DictReader(stream))
    except OSError:
        return None

    rss_values: list[int] = []
    cpu_values: list[float] = []
    for sample in samples:
        try:
            rss_values.append(int(sample["rss_bytes"]))
        except (KeyError, TypeError, ValueError):
            pass
        try:
            cpu_values.append(float(sample["cpu_seconds"]))
        except (KeyError, TypeError, ValueError):
            pass

    return {
        "sample_count": len(samples),
        "first_sample_utc": samples[0].get("timestamp_utc") if samples else None,
        "last_sample_utc": samples[-1].get("timestamp_utc") if samples else None,
        "cpu_seconds_start": cpu_values[0] if cpu_values else None,
        "cpu_seconds_end": cpu_values[-1] if cpu_values else None,
        "cpu_seconds_delta": round(cpu_values[-1] - cpu_values[0], 3) if len(cpu_values) > 1 else None,
        "rss_bytes_start": rss_values[0] if rss_values else None,
        "rss_bytes_end": rss_values[-1] if rss_values else None,
        "rss_bytes_peak": max(rss_values) if rss_values else None,
        "rss_bytes_delta": rss_values[-1] - rss_values[0] if len(rss_values) > 1 else None,
    }


def analyze_session(
    session_dir: Path,
    *,
    integration: str,
    minimum_hours: float,
) -> dict[str, Any]:
    summary = _read_json(session_dir / "summary.json")
    event_path = session_dir / "events.ndjson"
    rows = list(_iter_events(event_path))
    counts: Counter[str] = Counter()
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        event_type = str(row.get("type") or "unknown")
        if event_type == "roomUserSeq":
            event_type = "viewer"
        counts[event_type] += 1
        by_type[event_type].append(row)

    all_times = sorted(time for row in rows if (time := _received_ms(row)) is not None)
    event_intervals = [
        (later - earlier) / 1000
        for earlier, later in zip(all_times, all_times[1:])
        if later >= earlier
    ]
    viewer_times = sorted(
        time
        for row in by_type.get("viewer", [])
        if (time := _received_ms(row)) is not None
    )
    viewer_intervals = [
        (later - earlier) / 1000
        for earlier, later in zip(viewer_times, viewer_times[1:])
        if later >= earlier
    ]

    chats = by_type.get("chat", [])
    gifts = by_type.get("gift", [])
    counted_gifts = [row for row in gifts if row.get("counted", True)]
    diamonds = 0.0
    for row in counted_gifts:
        value = row.get("diamond_total")
        if value is None:
            per_gift = row.get("diamond_per_gift", row.get("diamond_count"))
            repeat = row.get("repeat_count") or 1
            if isinstance(per_gift, (int, float)) and isinstance(repeat, (int, float)):
                value = per_gift * repeat
        if isinstance(value, (int, float)):
            diamonds += float(value)

    streak_gifts = [
        row for row in gifts
        if row.get("gift_type") == 1 or row.get("gift_type") == "1"
    ]
    completed_streaks = [
        row for row in streak_gifts
        if row.get("repeat_end") is True
        or row.get("repeat_end") == 1
        or (row.get("streaking") is False and row.get("counted") is True)
    ]
    intermediate_streaks = [
        row for row in streak_gifts if row.get("streaking") is True
    ]

    duration_seconds = summary.get("collector_total_seconds")
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        if all_times:
            duration_seconds = (max(all_times) - min(all_times)) / 1000
        else:
            duration_seconds = 0
    duration_seconds = round(float(duration_seconds), 3)

    connected_events = sum(
        row.get("system_event") == "connected" for row in by_type.get("system", [])
    )
    disconnected_events = sum(
        row.get("system_event") == "disconnected" for row in by_type.get("system", [])
    )
    first_room_id = next(
        (row.get("room_id") for row in rows if row.get("room_id") not in (None, "")),
        None,
    )
    first_received = min(all_times) if all_times else None
    last_received = max(all_times) if all_times else None
    inferred_start = (
        datetime.fromtimestamp(first_received / 1000, tz=timezone.utc).isoformat()
        if first_received is not None else None
    )
    inferred_end = (
        datetime.fromtimestamp(last_received / 1000, tz=timezone.utc).isoformat()
        if last_received is not None else None
    )

    minimum_seconds = max(0.0, minimum_hours * 3600)
    if not summary:
        duration_state = "in_progress" if rows else "no_data"
        overall_state = "in_progress" if rows else "no_data"
    elif duration_seconds < minimum_seconds:
        duration_state = "incomplete"
        overall_state = "incomplete"
    else:
        duration_state = "observed"
        overall_state = "ready_for_review"

    uptime = summary.get("socket_uptime_ratio")
    if not isinstance(uptime, (int, float)):
        uptime = None

    package_versions = {"TikTokLive": _package_version("TikTokLive")}
    resources = _resource_summary(session_dir / "resource_samples.csv")

    return {
        "integration": integration,
        "session_dir": str(session_dir.resolve()),
        "provider": summary.get("backend") or "TikTokLive",
        "package_versions": package_versions,
        "username": summary.get("username") or session_dir.name[16:],
        "room_id": summary.get("room_id") or first_room_id,
        "status": summary.get("status") or "in_progress",
        "started_at_utc": summary.get("started_at_utc") or inferred_start,
        "ended_at_utc": summary.get("ended_at_utc") or inferred_end,
        "duration_seconds": duration_seconds,
        "connected_seconds": summary.get("socket_connected_seconds"),
        "socket_uptime_ratio": uptime,
        "connection_count": summary.get("connection_count") or connected_events,
        "disconnect_count": summary.get("disconnect_count") or disconnected_events,
        "event_counts": dict(sorted(counts.items())),
        "event_interval_seconds": {
            "median": round(median(event_intervals), 3) if event_intervals else None,
            "max": round(max(event_intervals), 3) if event_intervals else None,
        },
        "viewer_sample_count": len(by_type.get("viewer", [])),
        "viewer_interval_seconds": {
            "median": round(median(viewer_intervals), 3) if viewer_intervals else None,
            "max": round(max(viewer_intervals), 3) if viewer_intervals else None,
        },
        "chat_user_id_coverage": _coverage(chats, "user_id"),
        "chat_unique_id_coverage": _coverage(chats, "unique_id"),
        "gift_user_id_coverage": _coverage(gifts, "user_id"),
        "gift_unique_id_coverage": _coverage(gifts, "unique_id"),
        "gift_count": len(gifts),
        "counted_gift_count": len(counted_gifts),
        "gift_diamonds_observed": round(diamonds, 3),
        "streak_gift_event_count": len(streak_gifts),
        "completed_streak_event_count": len(completed_streaks),
        "intermediate_streak_event_count": len(intermediate_streaks),
        "resources": resources,
        "criteria": {
            "minimum_duration": duration_state,
            "viewer_events": "observed" if by_type.get("viewer") else "not_observed",
            "chat_events": "observed" if chats else "not_observed",
            "gift_events": "observed" if gifts else "not_observed",
            "gift_streak_completion": "observed" if completed_streaks else "not_observed",
            "transaction_replay_deduplication": "not_measured_by_probe",
            "fallback_transition": "not_measured_by_probe",
            "cpu_and_memory": "measured" if resources and resources["sample_count"] else "not_measured",
        },
        "overall_state": overall_state,
        "notes": [
            "This report summarizes the compact benchmark events; it does not read raw provider payloads.",
            "A ready_for_review result is not an automatic provider approval. Review inconclusive criteria and run separate reconnect/fallback/resource checks.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize a compact TikTok LIVE provider benchmark session."
    )
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--integration",
        default="TikTokLive Python client + Euler signing service",
    )
    parser.add_argument("--minimum-hours", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = analyze_session(
        args.session_dir,
        integration=args.integration,
        minimum_hours=args.minimum_hours,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
