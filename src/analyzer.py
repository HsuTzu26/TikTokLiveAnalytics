import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def iso_utc(timestamp_ms):
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).isoformat()


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    idx = (len(values) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(values) - 1)
    frac = idx - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate TikTok LIVE minimal collector data."
    )
    parser.add_argument(
        "session_dir",
        help="Session directory containing events.ndjson",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Aggregation window in seconds (default: 30)",
    )
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    events_path = session_dir / "events.ndjson"

    if not events_path.exists():
        raise FileNotFoundError(f"Missing: {events_path}")

    events = []
    with events_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))

    if not events:
        raise RuntimeError("No events found.")

    # Use server/event timestamp rather than file arrival order.
    events = [e for e in events if e.get("timestamp_ms") is not None]
    events.sort(key=lambda e: e["timestamp_ms"])

    window_ms = args.window * 1000
    first_ts = events[0]["timestamp_ms"]
    anchor = (first_ts // window_ms) * window_ms

    buckets = defaultdict(lambda: {
        "chat_count": 0,
        "chat_users": set(),
        "like_count": 0,
        "like_events": 0,
        "gift_events": 0,
        "gift_users": set(),
        "diamonds": 0,
        "viewer_values": [],
    })

    all_chat_users = set()
    all_gift_users = set()
    viewer_values = []

    total_chat = 0
    total_like_count = 0
    total_like_events = 0
    total_gift_events = 0
    total_diamonds = 0

    for e in events:
        ts = e["timestamp_ms"]
        bucket_start = anchor + ((ts - anchor) // window_ms) * window_ms
        b = buckets[bucket_start]

        etype = e.get("type")

        if etype == "chat":
            total_chat += 1
            b["chat_count"] += 1
            uid = e.get("unique_id")
            if uid:
                all_chat_users.add(uid)
                b["chat_users"].add(uid)

        elif etype == "like":
            n = e.get("like_count", 0) or 0
            total_like_count += n
            total_like_events += 1
            b["like_count"] += n
            b["like_events"] += 1

        elif etype == "gift":
            if e.get("counted"):
                total_gift_events += 1
                diamonds = e.get("diamond_total", 0) or 0
                total_diamonds += diamonds

                b["gift_events"] += 1
                b["diamonds"] += diamonds

                uid = e.get("unique_id")
                if uid:
                    all_gift_users.add(uid)
                    b["gift_users"].add(uid)

        elif etype == "viewer":
            v = e.get("viewer_count")
            if isinstance(v, (int, float)):
                viewer_values.append(v)
                b["viewer_values"].append(v)

    output_csv = session_dir / f"timeseries_{args.window}s.csv"

    fieldnames = [
        "window_start_utc",
        "viewer_last",
        "viewer_avg",
        "viewer_min",
        "viewer_max",
        "viewer_delta",
        "chat_count",
        "unique_chatters",
        "like_count",
        "like_events",
        "gift_events",
        "unique_gifters",
        "diamonds",
    ]

    rows = []
    previous_viewer = None

    for bucket_start in sorted(buckets):
        b = buckets[bucket_start]
        vv = b["viewer_values"]

        viewer_last = vv[-1] if vv else None
        viewer_avg = sum(vv) / len(vv) if vv else None
        viewer_min = min(vv) if vv else None
        viewer_max = max(vv) if vv else None

        viewer_delta = None
        if viewer_last is not None and previous_viewer is not None:
            viewer_delta = viewer_last - previous_viewer
        if viewer_last is not None:
            previous_viewer = viewer_last

        rows.append({
            "window_start_utc": iso_utc(bucket_start),
            "viewer_last": viewer_last,
            "viewer_avg": round(viewer_avg, 2) if viewer_avg is not None else None,
            "viewer_min": viewer_min,
            "viewer_max": viewer_max,
            "viewer_delta": viewer_delta,
            "chat_count": b["chat_count"],
            "unique_chatters": len(b["chat_users"]),
            "like_count": b["like_count"],
            "like_events": b["like_events"],
            "gift_events": b["gift_events"],
            "unique_gifters": len(b["gift_users"]),
            "diamonds": b["diamonds"],
        })

    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    duration_sec = (
        (events[-1]["timestamp_ms"] - events[0]["timestamp_ms"]) / 1000
        if len(events) > 1 else 0
    )

    summary = {
        "window_seconds": args.window,
        "first_event_utc": iso_utc(events[0]["timestamp_ms"]),
        "last_event_utc": iso_utc(events[-1]["timestamp_ms"]),
        "observed_duration_seconds": round(duration_sec, 2),
        "event_count": len(events),
        "chat": {
            "messages": total_chat,
            "unique_chatters": len(all_chat_users),
        },
        "likes": {
            "like_events": total_like_events,
            "likes_received": total_like_count,
        },
        "gifts": {
            "counted_gift_events": total_gift_events,
            "unique_gifters": len(all_gift_users),
            "total_diamonds": total_diamonds,
        },
        "viewers": {
            "samples": len(viewer_values),
            "peak_observed": max(viewer_values) if viewer_values else None,
            "average_observed": (
                round(sum(viewer_values) / len(viewer_values), 2)
                if viewer_values else None
            ),
            "median_observed": percentile(viewer_values, 0.5),
            "min_observed": min(viewer_values) if viewer_values else None,
        },
    }

    summary_path = session_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[done] {len(events)} events processed")
    print(f"[csv]  {output_csv}")
    print(f"[json] {summary_path}")
    print()
    print(f"Chat messages:   {total_chat}")
    print(f"Likes received:  {total_like_count}")
    print(f"Gift events:     {total_gift_events}")
    print(f"Total diamonds:  {total_diamonds}")
    if viewer_values:
        print(f"Peak viewers:    {max(viewer_values)}")
        print(
            f"Avg viewers*:    "
            f"{sum(viewer_values) / len(viewer_values):.2f}"
        )
        print("*Observed sample average, not TikTok official ACU.")


if __name__ == "__main__":
    main()
