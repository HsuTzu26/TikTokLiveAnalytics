import argparse
import csv
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
from pathlib import Path

import matplotlib.pyplot as plt


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def load_rows(csv_path):
    rows = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def parse_float(value):
    if value in (None, "", "None"):
        return None
    return float(value)


def parse_int(value):
    if value in (None, "", "None"):
        return 0
    return int(float(value))


def save_line(x, y, title, ylabel, out_path, marker=True):
    figure, axis = plt.subplots(figsize=(12, 5))
    if marker:
        axis.plot(x, y, marker="o", markersize=3)
    else:
        axis.plot(x, y)
    axis.set_title(title)
    axis.set_xlabel("Taiwan time (Asia/Taipei)")
    axis.set_ylabel(ylabel)
    axis.xaxis.set_major_locator(mdates.AutoDateLocator())
    axis.xaxis.set_major_formatter(
        mdates.DateFormatter("%m-%d %H:%M", tz=TAIPEI_TZ)
    )
    figure.autofmt_xdate(rotation=35, ha="right")
    axis.grid(True, alpha=0.25)
    figure.tight_layout()
    figure.savefig(out_path, dpi=150)
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(
        description="Plot TikTok LIVE session time series in Taiwan time."
    )
    parser.add_argument(
        "session_dir",
        help="Session directory containing the analyzer timeseries CSV.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Window size used by analyzer (default: 30)",
    )
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    csv_path = session_dir / f"timeseries_{args.window}s.csv"

    if not csv_path.exists():
        raise FileNotFoundError(f"Missing: {csv_path}")

    rows = load_rows(csv_path)
    if not rows:
        raise RuntimeError("No rows found in timeseries CSV.")

    times = []
    viewer = []
    chat = []
    likes = []
    diamonds = []
    cumulative_diamonds = []

    running_diamonds = 0

    for r in rows:
        dt = datetime.fromisoformat(
            r.get("window_start_local") or r["window_start_utc"]
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIPEI_TZ)
        times.append(dt.astimezone(TAIPEI_TZ))

        v = parse_float(r["viewer_last"])
        viewer.append(v)

        chat.append(parse_int(r["chat_count"]))
        likes.append(parse_int(r["like_count"]))

        d = parse_int(r["diamonds"])
        diamonds.append(d)

        running_diamonds += d
        cumulative_diamonds.append(running_diamonds)

    out_dir = session_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Viewer may contain missing windows; matplotlib will break the line at None.
    save_line(
        times,
        viewer,
        "TikTok LIVE Viewer Count",
        "Observed viewers",
        out_dir / "viewer_timeseries.png",
    )

    save_line(
        times,
        chat,
        "TikTok LIVE Chat Activity",
        f"Chat messages / {args.window}s",
        out_dir / "chat_timeseries.png",
    )

    save_line(
        times,
        likes,
        "TikTok LIVE Likes",
        f"Likes / {args.window}s",
        out_dir / "likes_timeseries.png",
    )

    save_line(
        times,
        diamonds,
        "TikTok LIVE Diamonds",
        f"Diamonds / {args.window}s",
        out_dir / "diamonds_timeseries.png",
    )

    save_line(
        times,
        cumulative_diamonds,
        "TikTok LIVE Cumulative Diamonds",
        "Cumulative diamonds",
        out_dir / "cumulative_diamonds.png",
    )

    print(f"[done] plots saved to: {out_dir}")
    for p in sorted(out_dir.glob("*.png")):
        print(f"[plot] {p}")


if __name__ == "__main__":
    main()
