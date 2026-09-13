import argparse
import csv
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt


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
    plt.figure(figsize=(11, 5))
    if marker:
        plt.plot(x, y, marker="o")
    else:
        plt.plot(x, y)
    plt.title(title)
    plt.xlabel("Time")
    plt.ylabel(ylabel)
    plt.xticks(rotation=45, ha="right")
    plt.grid(True, alpha=0.25)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Plot TikTok LIVE session time series."
    )
    parser.add_argument(
        "session_dir",
        help="Session directory containing timeseries_30s.csv",
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
        dt = datetime.fromisoformat(r["window_start_utc"])
        times.append(dt.strftime("%H:%M:%S"))

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
