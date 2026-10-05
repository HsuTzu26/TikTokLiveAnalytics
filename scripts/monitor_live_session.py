"""Passively record Browser LIVE and TTS health without reading chat text."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_ROOT = ROOT / "data" / "browser_ws_probe"
TTS_STATE_PATH = ROOT / "data" / "tts" / "state.json"
TTS_LOG_ROOT = ROOT / "data" / "tts" / "logs"
ISSUE_STAGES = {
    "offline_fallback_failed",
    "synthesis_failed",
    "playback_failed",
    "delivery_failed",
    "replay_unavailable",
    "synthesis_retry_queued",
    "playback_attempt_failed",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()


def summarize_event_lines(lines) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    gift_missing: Counter[str] = Counter()
    malformed = 0
    latest_viewer: int | None = None
    likes_total: int | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        event_type = str(event.get("type") or "unknown")
        counts[event_type] += 1
        if event_type == "gift" and event.get("counted") is not False:
            for key in ("gift_id", "gift_name", "nickname", "repeat_count", "diamond_total"):
                if event.get(key) in (None, ""):
                    gift_missing[key] += 1
        elif event_type == "viewer":
            value = event.get("viewer_count")
            if isinstance(value, (int, float)):
                latest_viewer = int(value)
        elif event_type == "like":
            value = event.get("total_likes")
            if isinstance(value, (int, float)):
                likes_total = int(value)
    return {
        "event_counts": dict(counts),
        "counted_gift_missing_fields": dict(gift_missing),
        "malformed_event_lines": malformed,
        "latest_viewer_count": latest_viewer,
        "latest_like_total": likes_total,
    }


def scan_event_summary(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return summarize_event_lines(handle)
    except OSError:
        return summarize_event_lines(())


def session_disk_bytes(session_dir: Path, include_frames: bool) -> dict[str, int]:
    names = ("events.ndjson", "tts_delivery.ndjson", "chat_tts_delivery.ndjson", "session.json")
    sizes: dict[str, int] = {}
    for name in names:
        try:
            sizes[name] = (session_dir / name).stat().st_size
        except OSError:
            sizes[name] = 0
    if include_frames:
        total = 0
        try:
            for path in (session_dir / "frames").glob("*.bin"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        sizes["frames_bytes"] = total
    return sizes


def pick_tts_log(username: str, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    candidates = list(TTS_LOG_ROOT.glob(f"browser_{username}_*.out.log"))
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def tts_snapshot(session_id: str) -> dict[str, Any]:
    state = read_json(TTS_STATE_PATH)
    if state.get("active_session") != session_id:
        summary = read_json(DEFAULT_SESSION_ROOT / session_id / "tts_summary.json")
        metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
        status = summary.get("status") or state.get("status")
    else:
        metrics = state.get("metrics") if isinstance(state.get("metrics"), dict) else {}
        status = state.get("status")
    skipped = metrics.get("skipped") if isinstance(metrics.get("skipped"), dict) else {}
    return {
        "status": status,
        "queue_depth": metrics.get("queue_depth"),
        "queue_capacity": metrics.get("queue_capacity"),
        "queue_peak": metrics.get("max_queue_depth"),
        "queue_full_waits": metrics.get("queue_full_waits"),
        "oldest_queue_age_seconds": metrics.get("oldest_queue_age_seconds"),
        "p50_event_to_playback_ms": metrics.get("p50_event_to_playback_ms"),
        "p95_event_to_playback_ms": metrics.get("p95_event_to_playback_ms"),
        "p50_queue_wait_ms": metrics.get("p50_queue_wait_ms"),
        "p95_queue_wait_ms": metrics.get("p95_queue_wait_ms"),
        "p50_synthesis_ms": metrics.get("p50_synthesis_ms"),
        "p95_synthesis_ms": metrics.get("p95_synthesis_ms"),
        "p50_audio_playback_ms": metrics.get("p50_audio_playback_ms"),
        "p95_audio_playback_ms": metrics.get("p95_audio_playback_ms"),
        "chat_events_seen": metrics.get("chat_events_seen"),
        "chat_queued": metrics.get("chat_queued"),
        "chat_spoken": metrics.get("chat_spoken"),
        "gift_events_seen": metrics.get("gift_events_seen"),
        "gift_queued": metrics.get("gift_queued"),
        "gift_spoken": metrics.get("gift_spoken"),
        "delivery_failures": metrics.get("delivery_failures"),
        "gift_delivery_failures": metrics.get("gift_delivery_failures"),
        "gift_offline_fallback_succeeded": metrics.get("gift_offline_fallback_succeeded"),
        "chat_offline_fallback_succeeded": metrics.get("chat_offline_fallback_succeeded"),
        "expired_chat": skipped.get("expired_chat", 0),
        "last_error": metrics.get("last_error"),
    }


def read_appended(path: Path, offset: int) -> tuple[int, list[str]]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if size < offset:
                offset = 0
            handle.seek(offset)
            payload = handle.read()
    except OSError:
        return offset, []
    newline = payload.rfind(b"\n")
    if newline < 0:
        return offset, []
    payload = payload[: newline + 1]
    next_offset = offset + len(payload)
    lines = payload.decode("utf-8", errors="replace").splitlines()
    return next_offset, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--sample-seconds", type=float, default=30.0)
    parser.add_argument("--tts-log", type=Path)
    parser.add_argument("--once", action="store_true", help="Write one sample and exit")
    parser.add_argument("--until-midnight", action="store_true", help="Stop at local midnight or after the session ends")
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.session_id):
        parser.error("invalid session id")
    session_dir = DEFAULT_SESSION_ROOT / args.session_id
    if not session_dir.is_dir():
        parser.error(f"session directory does not exist: {session_dir}")

    metadata_path = session_dir / "session.json"
    metadata = read_json(metadata_path)
    username = str(metadata.get("username") or metadata.get("streamer_username") or "")
    diagnostics_dir = session_dir / "diagnostics"
    health_path = diagnostics_dir / "health.ndjson"
    issues_path = diagnostics_dir / "issues.ndjson"
    baseline_path = diagnostics_dir / "baseline.json"
    event_summary = scan_event_summary(session_dir / "events.ndjson")
    baseline = {
        "started_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_id": args.session_id,
        "username": username,
        "live_preflight": metadata.get("live_preflight"),
        "status": metadata.get("status"),
        "target_websocket_connection_count": metadata.get("target_websocket_connection_count"),
        "captured_binary_frame_count": metadata.get("captured_binary_frame_count"),
        "detected_method_counts": metadata.get("detected_method_counts"),
        "normalized_event_counts": metadata.get("normalized_event_counts"),
        "decoding_errors": metadata.get("decoding_errors"),
        "event_audit": event_summary,
        "tts": tts_snapshot(args.session_id),
        "file_bytes": session_disk_bytes(session_dir, include_frames=True),
    }
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[MONITOR] session={args.session_id} output={diagnostics_dir}", flush=True)
    print(f"[BASELINE] events={event_summary['event_counts']} gifts_missing={event_summary['counted_gift_missing_fields']}", flush=True)

    tts_log = pick_tts_log(username, args.tts_log)
    log_offset = 0
    gift_log_offset = 0
    chat_log_offset = 0
    previous_tts = tts_snapshot(args.session_id)
    event_counts_total = Counter(event_summary["event_counts"])
    last_sample_event_counts = Counter(event_counts_total)
    event_path = session_dir / "events.ndjson"
    try:
        event_offset = event_path.stat().st_size
    except OSError:
        event_offset = 0
    gift_missing = Counter(event_summary["counted_gift_missing_fields"])
    malformed_event_lines = int(event_summary["malformed_event_lines"])
    latest_viewer_count = event_summary["latest_viewer_count"]
    latest_like_total = event_summary["latest_like_total"]
    finished_seen_at: float | None = None
    stop_at = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
    sample_index = 0

    while True:
        now = datetime.now().astimezone()
        metadata = read_json(metadata_path)
        tts = tts_snapshot(args.session_id)
        event_offset, new_event_lines = read_appended(event_path, event_offset)
        event_delta_summary = summarize_event_lines(new_event_lines)
        event_counts_total.update(event_delta_summary["event_counts"])
        gift_missing.update(event_delta_summary["counted_gift_missing_fields"])
        malformed_event_lines += event_delta_summary["malformed_event_lines"]
        if event_delta_summary["latest_viewer_count"] is not None:
            latest_viewer_count = event_delta_summary["latest_viewer_count"]
        if event_delta_summary["latest_like_total"] is not None:
            latest_like_total = event_delta_summary["latest_like_total"]
        current_summary = {
            "event_counts": dict(event_counts_total),
            "counted_gift_missing_fields": dict(gift_missing),
            "malformed_event_lines": malformed_event_lines,
            "latest_viewer_count": latest_viewer_count,
            "latest_like_total": latest_like_total,
        }
        current_counts = Counter(current_summary["event_counts"])
        deltas = {
            key: current_counts[key] - last_sample_event_counts[key]
            for key in sorted(set(current_counts) | set(last_sample_event_counts))
            if current_counts[key] != last_sample_event_counts[key]
        }
        include_frames = sample_index % 10 == 0
        file_bytes = session_disk_bytes(session_dir, include_frames=include_frames)
        try:
            event_mtime = datetime.fromtimestamp((session_dir / "events.ndjson").stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            event_mtime = None

        sample = {
            "timestamp_local": now.isoformat(timespec="seconds"),
            "session_status": metadata.get("status"),
            "live_preflight": metadata.get("live_preflight"),
            "target_websocket_connection_count": metadata.get("target_websocket_connection_count"),
            "captured_binary_frame_count": metadata.get("captured_binary_frame_count"),
            "detected_method_counts": metadata.get("detected_method_counts"),
            "normalized_event_counts": current_summary["event_counts"],
            "event_delta_since_previous_sample": deltas,
            "malformed_event_lines": current_summary["malformed_event_lines"],
            "counted_gift_missing_fields": current_summary["counted_gift_missing_fields"],
            "latest_viewer_count": current_summary["latest_viewer_count"],
            "latest_like_total": current_summary["latest_like_total"],
            "events_file_updated_at": event_mtime,
            "tts": tts,
            "file_bytes": file_bytes,
        }
        append_jsonl(health_path, sample)

        expired_delta = int(tts.get("expired_chat") or 0) - int(previous_tts.get("expired_chat") or 0)
        event_p95 = int(tts.get("p95_event_to_playback_ms") or 0)
        queue_p95 = int(tts.get("p95_queue_wait_ms") or 0)
        if expired_delta > 0:
            append_jsonl(issues_path, {
                "timestamp_local": now.isoformat(timespec="seconds"),
                "kind": "chat_expired",
                "count_delta": expired_delta,
                "expired_chat_total": tts.get("expired_chat"),
                "p95_event_to_playback_ms": tts.get("p95_event_to_playback_ms"),
                "p95_queue_wait_ms": tts.get("p95_queue_wait_ms"),
            })
        if event_p95 >= 10000 or queue_p95 >= 10000:
            append_jsonl(issues_path, {
                "timestamp_local": now.isoformat(timespec="seconds"),
                "kind": "latency_spike",
                "p95_event_to_playback_ms": tts.get("p95_event_to_playback_ms"),
                "p95_queue_wait_ms": tts.get("p95_queue_wait_ms"),
                "p95_synthesis_ms": tts.get("p95_synthesis_ms"),
                "queue_depth": tts.get("queue_depth"),
            })
        if tts.get("last_error") and tts.get("last_error") != previous_tts.get("last_error"):
            append_jsonl(issues_path, {
                "timestamp_local": now.isoformat(timespec="seconds"),
                "kind": "tts_error_state",
                "error": tts.get("last_error"),
                "queue_depth": tts.get("queue_depth"),
            })

        if tts_log is not None:
            log_offset, lines = read_appended(tts_log, log_offset)
            for line in lines:
                match = re.search(r"\[tts-error\]\s+([A-Za-z0-9_]+)(?:;\s*(.*))?", line)
                if match:
                    append_jsonl(issues_path, {
                        "timestamp_local": now.isoformat(timespec="seconds"),
                        "kind": "edge_or_playback_error",
                        "error": match.group(1),
                        "retry": (match.group(2) or "")[:48],
                    })

        for name, offset in (("tts_delivery.ndjson", gift_log_offset), ("chat_tts_delivery.ndjson", chat_log_offset)):
            log_path = session_dir / name
            next_offset, lines = read_appended(log_path, offset)
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stage = str(record.get("stage") or "")
                if stage in ISSUE_STAGES:
                    issue = {
                        "timestamp_local": record.get("timestamp_local") or now.isoformat(timespec="seconds"),
                        "kind": "tts_delivery_stage",
                        "stream": "gift" if name == "tts_delivery.ndjson" else "chat",
                        "stage": stage,
                        "error": record.get("error"),
                        "duration_ms": record.get("duration_ms"),
                    }
                    if name == "tts_delivery.ndjson":
                        issue["gift_id"] = record.get("gift_id")
                        issue["event_id"] = record.get("event_id")
                    append_jsonl(issues_path, issue)
            if name == "tts_delivery.ndjson":
                gift_log_offset = next_offset
            else:
                chat_log_offset = next_offset

        browser_active = metadata.get("status") == "running"
        worker_attached = read_json(TTS_STATE_PATH).get("active_session") == args.session_id
        if not browser_active:
            if finished_seen_at is None:
                finished_seen_at = time.monotonic()
            if not worker_attached or time.monotonic() - finished_seen_at >= 180:
                break
        else:
            finished_seen_at = None
        if args.once:
            break
        if args.until_midnight and datetime.now() >= stop_at:
            break

        previous_tts = tts
        last_sample_event_counts = current_counts
        sample_index += 1
        # Keep idle monitoring unobtrusive; all runtime reads are shared/read-only.
        time.sleep(max(5.0, float(args.sample_seconds)))

    final = {
        "ended_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_id": args.session_id,
        "samples_written": sum(1 for _ in health_path.open("r", encoding="utf-8")) if health_path.exists() else 0,
        "issues_written": sum(1 for _ in issues_path.open("r", encoding="utf-8")) if issues_path.exists() else 0,
        "final_session_status": read_json(metadata_path).get("status"),
        "final_tts": tts_snapshot(args.session_id),
        "final_event_audit": scan_event_summary(session_dir / "events.ndjson"),
        "final_file_bytes": session_disk_bytes(session_dir, include_frames=True),
        "tts_log": str(tts_log) if tts_log else None,
    }
    (diagnostics_dir / "final_summary.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[MONITOR STOPPED] samples={final['samples_written']} issues={final['issues_written']} summary={diagnostics_dir / 'final_summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
