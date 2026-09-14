
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path


SOURCE_NAME_RE = re.compile(r"^(?P<date>\d{8})_(?P<time>\d{6})_(?P<username>.+)$")
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
NDJSON_FILES = ("events.ndjson", "raw_events.ndjson", "diagnostics.ndjson", "users.ndjson")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def iter_ndjson(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line_no, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError:
                yield line_no, None


def source_dirs(raw_root: Path, include_archive: bool = False):
    roots = [raw_root]
    if include_archive:
        archive_root = raw_root / "archive"
        if archive_root.exists():
            roots.extend(sorted((item for item in archive_root.iterdir() if item.is_dir()), key=lambda item: item.name))
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if not path.is_dir():
                continue
            if path in seen:
                continue
            seen.add(path)
            match = SOURCE_NAME_RE.match(path.name)
            if not match or not (path / "session.json").exists():
                continue
            meta = read_json(path / "session.json", {})
            username = str(meta.get("username") or match.group("username")).lstrip("@")
            yield path, match.group("date"), username, meta


def enrich(record, source_id, merged_id, sequence=None):
    record = dict(record)
    record["source_session_id"] = source_id
    record["merged_session_id"] = merged_id
    if sequence is not None:
        record["source_seq"] = record.get("seq")
        record["seq"] = sequence
    record["session_id"] = merged_id
    return record


def merge_group(raw_root: Path, date: str, username: str, sources: list[tuple[Path, dict]], archive: bool, refresh: bool = False):
    merged_id = f"{date}_{username}"
    final_target = raw_root / merged_id
    if final_target.exists() and not refresh:
        raise FileExistsError(f"Target already exists: {final_target}; use --refresh to rebuild it")
    target = final_target
    temporary_target = None
    if final_target.exists():
        temporary_target = raw_root / f".{merged_id}.merge_tmp"
        if temporary_target.exists():
            shutil.rmtree(temporary_target)
        target = temporary_target
    target.mkdir(parents=True)
    source_ids = [path.name for path, _ in sources]
    all_counts = Counter()
    raw_event_count = 0
    bad_lines = Counter()
    first_event_ms = None
    last_event_ms = None
    room_ids = set()
    statuses = []
    connection_history = []
    connection_offset = 0
    total_collector = 0.0
    total_connected = 0.0
    total_gap = 0.0
    total_duplicates = 0
    total_sdk_errors = 0
    started_utc = []
    ended_utc = []
    started_local = []
    ended_local = []

    for _, meta in sources:
        if meta.get("room_id"):
            room_ids.add(str(meta["room_id"]))
        statuses.append(meta.get("status"))
        if meta.get("collector_started_at_utc"):
            started_utc.append(meta["collector_started_at_utc"])
        if meta.get("collector_ended_at_utc"):
            ended_utc.append(meta["collector_ended_at_utc"])
        if meta.get("collector_started_at_local"):
            started_local.append(meta["collector_started_at_local"])
        if meta.get("collector_ended_at_local"):
            ended_local.append(meta["collector_ended_at_local"])
        quality = meta.get("data_quality") or {}
        total_collector += float(quality.get("collector_total_seconds") or 0)
        total_connected += float(quality.get("socket_connected_seconds") or 0)
        total_gap += float(quality.get("socket_gap_seconds") or 0)
        total_duplicates += int(quality.get("duplicate_events_dropped") or 0)
        total_sdk_errors += int(quality.get("sdk_error_events") or 0)
        for item in meta.get("connection_history") or []:
            entry = dict(item)
            entry["source_session_id"] = meta.get("session_id")
            entry["connection_id"] = connection_offset + 1
            connection_offset += 1
            connection_history.append(entry)
        for room in meta.get("room_ids") or []:
            room_ids.add(str(room))

    output_counts = Counter()
    output_raw_count = 0
    output_user_count = 0
    event_seq = 0

    handles = {}
    try:
        for name in NDJSON_FILES:
            handles[name] = (target / name).open("w", encoding="utf-8", buffering=1)

        for source_path, meta in sources:
            source_id = meta.get("session_id") or source_path.name
            for name in NDJSON_FILES:
                input_path = source_path / name
                for line_no, record in iter_ndjson(input_path):
                    if record is None:
                        bad_lines[name] += 1
                        continue
                    if name == "events.ndjson":
                        event_seq += 1
                        record = enrich(record, source_id, merged_id, event_seq)
                        output_counts[record.get("type") or "unknown"] += 1
                        timestamp = record.get("timestamp_ms") or record.get("received_at_ms")
                        if isinstance(timestamp, (int, float)):
                            first_event_ms = timestamp if first_event_ms is None else min(first_event_ms, timestamp)
                            last_event_ms = timestamp if last_event_ms is None else max(last_event_ms, timestamp)
                    else:
                        record = enrich(record, source_id, merged_id)
                    if name == "raw_events.ndjson":
                        output_raw_count += 1
                    if name == "users.ndjson":
                        output_user_count += 1
                    handles[name].write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        for fp in handles.values():
            fp.close()

    if first_event_ms is not None:
        first_local = datetime.fromtimestamp(first_event_ms / 1000, tz=timezone.utc).astimezone(TAIPEI_TZ).isoformat()
    else:
        first_local = None
    if last_event_ms is not None:
        last_local = datetime.fromtimestamp(last_event_ms / 1000, tz=timezone.utc).astimezone(TAIPEI_TZ).isoformat()
    else:
        last_local = None

    # Event timestamps already contain explicit local fields; use them when available.
    for name in ("events.ndjson",):
        for _, record in iter_ndjson(target / name):
            if not record:
                continue
            value = record.get("timestamp_local") or record.get("received_at_local")
            if value:
                first_local = value if first_local is None else min(first_local, value)
                last_local = value if last_local is None else max(last_local, value)

    session = {
        "schema_version": "0.3",
        "collector_version": "0.3",
        "session_id": merged_id,
        "merged_session": True,
        "username": username,
        "timezone": "Asia/Taipei",
        "source_session_ids": source_ids,
        "segment_count": len(source_ids),
        "segment_statuses": statuses,
        "room_id": next(iter(room_ids)) if len(room_ids) == 1 else None,
        "room_ids": sorted(room_ids),
        "collector_started_at_utc": min(started_utc) if started_utc else None,
        "collector_ended_at_utc": max(ended_utc) if ended_utc else None,
        "collector_started_at_local": min(started_local) if started_local else first_local,
        "collector_ended_at_local": max(ended_local) if ended_local else last_local,
        "first_event_local": first_local,
        "last_event_local": last_local,
        "status": "aggregated",
        "connection_count": sum(int(meta.get("connection_count") or 0) for _, meta in sources),
        "reconnect_count": sum(int(meta.get("reconnect_count") or 0) for _, meta in sources),
        "disconnect_count": sum(int(meta.get("disconnect_count") or 0) for _, meta in sources),
        "connection_history": connection_history,
        "event_counts": dict(output_counts),
        "raw_event_count": output_raw_count,
        "user_record_count": output_user_count,
        "data_quality": {
            "collector_total_seconds": round(total_collector, 3),
            "socket_connected_seconds": round(total_connected, 3),
            "socket_uptime_ratio": round(total_connected / total_collector, 4) if total_collector else None,
            "socket_gap_seconds": round(total_gap, 3),
            "duplicate_events_dropped": total_duplicates,
            "sdk_error_events": total_sdk_errors,
            "bad_ndjson_lines": dict(bad_lines),
        },
        "duplicate_event_counts": {},
        "merge_notes": {
            "source_order": "collector_started_at_local",
            "raw_payloads_preserved": True,
            "records_resequenced": True,
            "original_source_session_id_preserved": True,
        },
    }
    session_path = target / "session.json"
    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_path = target / "collector.log"
    with log_path.open("w", encoding="utf-8") as output:
        for source_path, _ in sources:
            source_log = source_path / "collector.log"
            if not source_log.exists():
                continue
            output.write(f"\n===== source_session={source_path.name} =====\n")
            output.write(source_log.read_text(encoding="utf-8", errors="replace"))

    if temporary_target is not None:
        final_target.mkdir(parents=True, exist_ok=True)
        for child in temporary_target.iterdir():
            destination = final_target / child.name
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(child), str(destination))
        temporary_target.rmdir()

    if archive:
        archive_root = raw_root / "archive" / date
        archive_root.mkdir(parents=True, exist_ok=True)
        for source_path, _ in sources:
            if source_path.parent == archive_root:
                continue
            destination = archive_root / source_path.name
            if destination.exists():
                raise FileExistsError(f"Archive target already exists: {destination}")
            shutil.move(str(source_path), str(destination))

    print(json.dumps({
        "merged_session": merged_id,
        "sources": len(source_ids),
        "events": event_seq,
        "raw_events": output_raw_count,
        "users": output_user_count,
        "event_counts": dict(output_counts),
        "archive": archive,
        "refresh": refresh,
        "bad_lines": dict(bad_lines),
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Merge daily TikTok LIVE sessions by date and streamer.")
    parser.add_argument("--raw-root", default="data/raw")
    parser.add_argument("--date", help="Merge one date in YYYYMMDD.")
    parser.add_argument("--before", help="Merge all dates before YYYYMMDD.")
    parser.add_argument("--username", help="Only merge one streamer username.")
    parser.add_argument("--archive", action="store_true", help="Move source folders under data/raw/archive after a successful merge.")
    parser.add_argument("--refresh", action="store_true", help="Rebuild an existing daily aggregate from direct and archived source sessions.")
    args = parser.parse_args()

    raw_root = Path(args.raw_root).resolve()
    groups = {}
    for path, date, username, meta in source_dirs(raw_root, include_archive=args.refresh):
        if args.date and date != args.date:
            continue
        if args.before and date >= args.before:
            continue
        if args.username and username != args.username.lstrip("@"):
            continue
        groups.setdefault((date, username), []).append((path, meta))

    if not groups:
        raise SystemExit("No mergeable source sessions found.")

    for (date, username), sources in sorted(groups.items()):
        merge_group(raw_root, date, username, sources, args.archive, args.refresh)


if __name__ == "__main__":
    main()
