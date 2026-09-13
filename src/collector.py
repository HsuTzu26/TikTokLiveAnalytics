import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from tiktok_live_events import TikTokLive


def utc_iso(timestamp_ms):
    if timestamp_ms is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def user_fields(event):
    user = event.get("user") or {}
    return {
        "user_id": user.get("id"),
        "unique_id": user.get("uniqueId"),
        "nickname": user.get("nickname"),
    }


def main():
    parser = argparse.ArgumentParser(description="Minimal TikTok LIVE analytics collector")
    parser.add_argument("username", help="TikTok LIVE username, with or without @")
    parser.add_argument(
        "--output-root",
        default="data/raw",
        help="Root directory for captured sessions (default: data/raw)",
    )
    args = parser.parse_args()

    username = args.username.lstrip("@")
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{username}"
    session_dir = Path(args.output_root) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    events_path = session_dir / "events.ndjson"
    session_path = session_dir / "session.json"

    session_meta = {
        "session_id": session_id,
        "username": username,
        "collector_version": "0.1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "event_types": ["chat", "like", "gift", "roomUserSeq"],
    }

    session_path.write_text(
        json.dumps(session_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    live = TikTokLive(username)

    def write_event(record):
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @live.on("connected")
    def on_connected(_):
        print(f"[connected] @{username}")
        print(f"[output]    {events_path}")

    @live.on("chat")
    def on_chat(e):
        record = {
            "type": "chat",
            "timestamp_ms": e.get("timestamp"),
            "timestamp_utc": utc_iso(e.get("timestamp")),
            "msg_id": e.get("msgId"),
            **user_fields(e),
            "comment": e.get("comment"),
            "language": e.get("language"),
            "message_uuid": e.get("messageUuid"),
        }
        write_event(record)
        print(f"[chat]   {record['unique_id']}: {record['comment']}")

    @live.on("like")
    def on_like(e):
        record = {
            "type": "like",
            "timestamp_ms": e.get("timestamp"),
            "timestamp_utc": utc_iso(e.get("timestamp")),
            "msg_id": e.get("msgId"),
            **user_fields(e),
            "like_count": e.get("likeCount", 0),
            "total_likes": e.get("totalLikes"),
        }
        write_event(record)
        print(
            f"[like]   {record['unique_id']} +{record['like_count']} "
            f"(total={record['total_likes']})"
        )

    @live.on("gift")
    def on_gift(e):
        gift_type = e.get("giftType")
        repeat_end = bool(e.get("repeatEnd"))
        repeat_count = e.get("repeatCount", 1) or 1
        diamond_count = e.get("diamondCount", 0) or 0

        # TikTok giftType == 1 is streakable.
        # Count only the final event of a streak.
        # Non-streakable gifts are counted immediately.
        count_this_event = (gift_type != 1) or repeat_end
        diamond_total = (
            diamond_count * repeat_count if count_this_event else 0
        )

        record = {
            "type": "gift",
            "timestamp_ms": e.get("timestamp"),
            "timestamp_utc": utc_iso(e.get("timestamp")),
            "msg_id": e.get("msgId"),
            **user_fields(e),
            "gift_id": e.get("giftId"),
            "gift_name": e.get("giftName"),
            "gift_type": gift_type,
            "diamond_per_gift": diamond_count,
            "repeat_count": repeat_count,
            "repeat_end": repeat_end,
            "group_id": e.get("groupId"),
            "transaction_id": e.get("transactionId"),
            "counted": count_this_event,
            "diamond_total": diamond_total,
        }
        write_event(record)

        state = "COUNT" if count_this_event else "STREAK"
        print(
            f"[gift:{state}] {record['unique_id']} -> "
            f"{record['gift_name']} x{repeat_count} "
            f"({diamond_total} diamonds)"
        )

    @live.on("roomUserSeq")
    def on_viewer(e):
        record = {
            "type": "viewer",
            "timestamp_ms": e.get("timestamp"),
            "timestamp_utc": utc_iso(e.get("timestamp")),
            "msg_id": e.get("msgId"),
            "viewer_count": e.get("viewerCount"),
        }
        write_event(record)
        print(f"[viewer] {record['viewer_count']}")

    try:
        asyncio.run(live.run())
    except KeyboardInterrupt:
        pass
    finally:
        session_meta["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        session_path.write_text(
            json.dumps(session_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[stopped] data saved to {session_dir}")


if __name__ == "__main__":
    main()
