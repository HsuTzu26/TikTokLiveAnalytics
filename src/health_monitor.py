from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from analytics import write_health_report


SESSION_RE = re.compile(r"^\d{8}_\d{6}_.+$")


def session_dirs(raw_root: Path):
    if not raw_root.exists():
        return []
    return sorted(
        path for path in raw_root.iterdir()
        if path.is_dir()
        and SESSION_RE.match(path.name)
        and (path / "session.json").exists()
    )


def main():
    parser = argparse.ArgumentParser(description="Write periodic TikTok LIVE session health reports.")
    parser.add_argument("--raw-root", default="data/raw")
    parser.add_argument("--session", help="One session directory name.")
    parser.add_argument("--all", action="store_true", help="Check all direct source sessions.")
    args = parser.parse_args()

    raw_root = Path(args.raw_root).resolve()
    if args.session:
        candidate = raw_root / args.session
        if not candidate.exists():
            matches = list(raw_root.rglob(args.session))
            candidate = matches[0] if matches else candidate
        paths = [candidate]
    elif args.all:
        paths = session_dirs(raw_root)
    else:
        raise SystemExit("Specify --session or --all.")

    reports = []
    for path in paths:
        if not (path / "session.json").exists():
            continue
        report = write_health_report(path)
        reports.append({
            "session_id": report["session_id"],
            "status": report["status"],
            "event_count": report["event_count"],
            "socket_uptime_ratio": report["socket_uptime_ratio"],
            "sdk_error_events": report["sdk_error_events"],
            "unknown_event_count": report["unknown_event_count"],
        })
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
