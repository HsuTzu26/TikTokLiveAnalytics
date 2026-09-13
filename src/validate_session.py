import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Validate TikTok LIVE gift/diamond aggregation."
    )
    parser.add_argument(
        "session_dir",
        help="Session directory containing events.ndjson",
    )
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    events_path = session_dir / "events.ndjson"

    if not events_path.exists():
        raise FileNotFoundError(f"Missing: {events_path}")

    gift_summary = defaultdict(lambda: {
        "counted_events": 0,
        "total_quantity": 0,
        "diamond_per_gift_values": set(),
        "diamonds": 0,
    })
    gifter_summary = defaultdict(lambda: {
        "nickname": None,
        "counted_events": 0,
        "diamonds": 0,
    })

    total_gift_events_raw = 0
    total_counted_events = 0
    total_uncounted_streak_events = 0
    total_diamonds = 0

    suspicious = []

    with events_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            e = json.loads(line)
            if e.get("type") != "gift":
                continue

            total_gift_events_raw += 1

            gift_type = e.get("gift_type")
            repeat_end = bool(e.get("repeat_end"))
            repeat_count = e.get("repeat_count", 1) or 1
            diamond_per_gift = e.get("diamond_per_gift", 0) or 0
            counted = bool(e.get("counted"))
            diamond_total = e.get("diamond_total", 0) or 0

            expected_counted = (gift_type != 1) or repeat_end
            expected_total = (
                diamond_per_gift * repeat_count if expected_counted else 0
            )

            if counted != expected_counted or diamond_total != expected_total:
                suspicious.append({
                    "line": lineno,
                    "gift_name": e.get("gift_name"),
                    "unique_id": e.get("unique_id"),
                    "gift_type": gift_type,
                    "repeat_count": repeat_count,
                    "repeat_end": repeat_end,
                    "stored_counted": counted,
                    "expected_counted": expected_counted,
                    "stored_diamond_total": diamond_total,
                    "expected_diamond_total": expected_total,
                })

            if not counted:
                total_uncounted_streak_events += 1
                continue

            total_counted_events += 1
            total_diamonds += diamond_total

            gift_name = e.get("gift_name") or f"gift_{e.get('gift_id')}"
            g = gift_summary[gift_name]
            g["counted_events"] += 1
            g["total_quantity"] += repeat_count
            g["diamond_per_gift_values"].add(diamond_per_gift)
            g["diamonds"] += diamond_total

            uid = e.get("unique_id") or str(e.get("user_id") or "unknown")
            u = gifter_summary[uid]
            u["nickname"] = e.get("nickname")
            u["counted_events"] += 1
            u["diamonds"] += diamond_total

    gift_rows = []
    for gift_name, g in gift_summary.items():
        values = sorted(g["diamond_per_gift_values"])
        gift_rows.append({
            "gift_name": gift_name,
            "counted_events": g["counted_events"],
            "total_quantity": g["total_quantity"],
            "diamond_per_gift": ",".join(map(str, values)),
            "total_diamonds": g["diamonds"],
        })

    gift_rows.sort(
        key=lambda x: (-x["total_diamonds"], x["gift_name"])
    )

    gifter_rows = []
    for uid, u in gifter_summary.items():
        gifter_rows.append({
            "unique_id": uid,
            "nickname": u["nickname"],
            "counted_events": u["counted_events"],
            "total_diamonds": u["diamonds"],
        })

    gifter_rows.sort(
        key=lambda x: (-x["total_diamonds"], x["unique_id"])
    )

    gifts_csv = session_dir / "gift_summary.csv"
    with gifts_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "gift_name",
                "counted_events",
                "total_quantity",
                "diamond_per_gift",
                "total_diamonds",
            ],
        )
        writer.writeheader()
        writer.writerows(gift_rows)

    gifters_csv = session_dir / "gifter_summary.csv"
    with gifters_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "unique_id",
                "nickname",
                "counted_events",
                "total_diamonds",
            ],
        )
        writer.writeheader()
        writer.writerows(gifter_rows)

    validation = {
        "raw_gift_events": total_gift_events_raw,
        "counted_gift_events": total_counted_events,
        "uncounted_streak_events": total_uncounted_streak_events,
        "total_diamonds": total_diamonds,
        "consistency_errors": len(suspicious),
        "errors": suspicious,
    }

    validation_path = session_dir / "gift_validation.json"
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("[gift validation]")
    print(f"Raw gift events:          {total_gift_events_raw}")
    print(f"Counted gift events:      {total_counted_events}")
    print(f"Uncounted streak events:  {total_uncounted_streak_events}")
    print(f"Total diamonds:           {total_diamonds}")
    print(f"Consistency errors:       {len(suspicious)}")
    print()

    print("[top gifts]")
    for row in gift_rows[:10]:
        print(
            f"{row['gift_name']}: "
            f"{row['total_quantity']} item(s), "
            f"{row['total_diamonds']} diamonds"
        )

    print()
    print("[top gifters]")
    for row in gifter_rows[:10]:
        print(
            f"{row['unique_id']}: "
            f"{row['total_diamonds']} diamonds"
        )

    print()
    print(f"[csv]  {gifts_csv}")
    print(f"[csv]  {gifters_csv}")
    print(f"[json] {validation_path}")


if __name__ == "__main__":
    main()
