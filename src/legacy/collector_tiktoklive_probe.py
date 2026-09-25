import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    CommentEvent,
    ConnectEvent,
    DisconnectEvent,
    EmoteChatEvent,
    FollowEvent,
    GiftEvent,
    JoinEvent,
    LikeEvent,
    LiveEndEvent,
    RoomUserSeqEvent,
    ShareEvent,
)


def now_ms():
    return time.time_ns() // 1_000_000


def iso_utc(ms=None):
    if ms is None:
        ms = now_ms()
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc
    ).isoformat()


def safe_get(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def user_fields(user):
    if user is None:
        return {
            "user_id": None,
            "unique_id": None,
            "nickname": None,
        }
    return {
        "user_id": safe_get(user, "id", "user_id"),
        "unique_id": safe_get(user, "unique_id", "uniqueId"),
        "nickname": safe_get(user, "nickname"),
    }


EMOTE_IMAGE_HOSTS = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "ibytedtos.com",
    "byteoversea.com",
    "ibyteimg.com",
)


def normalized_emotes(values):
    """Keep compact sticker IDs and HTTPS image URLs from TikTok CDNs."""
    if not isinstance(values, (list, tuple)):
        return []

    result = []
    for value in values[:8]:
        emote = safe_get(value, "emote", default=value)
        if emote is None:
            continue
        emote_id = safe_get(emote, "emote_id", "uuid", "id")
        image = safe_get(emote, "image")
        urls = safe_get(image, "url_list", "urls", default=[])
        if isinstance(urls, str):
            urls = [urls]

        image_url = None
        for candidate in urls if isinstance(urls, (list, tuple)) else []:
            if not isinstance(candidate, str):
                continue
            try:
                parsed = urlsplit(candidate)
                host = (parsed.hostname or "").casefold()
            except ValueError:
                continue
            if parsed.scheme == "https" and any(
                host == suffix or host.endswith(f".{suffix}")
                for suffix in EMOTE_IMAGE_HOSTS
            ):
                image_url = candidate[:2048]
                break

        if emote_id is None and image_url is None:
            continue
        result.append({
            "emote_id": str(emote_id)[:160] if emote_id is not None else None,
            "image_url": image_url,
        })
    return result


def main():
    parser = argparse.ArgumentParser(
        description="TikTokLive A/B reliability probe"
    )
    parser.add_argument("username")
    parser.add_argument(
        "--output-root",
        default="data/ab_tiktoklive",
    )
    args = parser.parse_args()

    username = args.username.lstrip("@")
    session_id = (
        datetime.now()
        .strftime("%Y%m%d_%H%M%S")
        + f"_{username}"
    )

    session_dir = Path(args.output_root) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    events_path = session_dir / "events.ndjson"
    summary_path = session_dir / "summary.json"

    fp = events_path.open(
        "a", encoding="utf-8", buffering=1
    )

    state = {
        "started_ms": now_ms(),
        "ended_ms": None,
        "connected_since_ms": None,
        "connected_seconds": 0.0,
        "connection_count": 0,
        "disconnect_count": 0,
        "counts": Counter(),
        "room_id": None,
        "live_ended": False,
    }

    client = TikTokLiveClient(
        unique_id=f"@{username}"
    )

    def write_event(event_type, payload=None):
        state["counts"][event_type] += 1
        row = {
            "type": event_type,
            "received_at_ms": now_ms(),
            "received_at_utc": iso_utc(),
            "room_id": (
                str(client.room_id)
                if client.room_id is not None
                else None
            ),
        }
        if payload:
            row.update(payload)

        fp.write(
            json.dumps(row, ensure_ascii=False)
            + "\n"
        )
        fp.flush()

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        ms = now_ms()
        state["connection_count"] += 1
        state["connected_since_ms"] = ms
        state["room_id"] = (
            str(client.room_id)
            if client.room_id is not None
            else None
        )

        write_event(
            "system",
            {
                "system_event": "connected",
                "unique_id": safe_get(
                    event, "unique_id"
                ),
            },
        )
        print(
            f"[connected] @{username} "
            f"room={state['room_id']}"
        )

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        ms = now_ms()
        state["disconnect_count"] += 1

        if state["connected_since_ms"] is not None:
            state["connected_seconds"] += (
                ms - state["connected_since_ms"]
            ) / 1000.0
            state["connected_since_ms"] = None

        write_event(
            "system",
            {"system_event": "disconnected"},
        )
        print("[disconnected]")

    @client.on(LiveEndEvent)
    async def on_live_end(event: LiveEndEvent):
        state["live_ended"] = True
        write_event(
            "system",
            {"system_event": "live_end"},
        )
        print("[live_end]")

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        record = {
            **user_fields(
                safe_get(event, "user")
            ),
            "comment": safe_get(
                event, "comment"
            ),
        }
        emotes = normalized_emotes(safe_get(event, "emotes", default=[]))
        if emotes:
            record["emotes"] = emotes
        write_event("chat", record)
        print(
            f"[chat] {record['unique_id']}: "
            f"{record['comment']}"
        )

    @client.on(EmoteChatEvent)
    async def on_emote_chat(event: EmoteChatEvent):
        record = {
            **user_fields(safe_get(event, "user")),
            "comment": None,
            "message_kind": "emote",
            "emotes": normalized_emotes(
                safe_get(event, "emote_list", default=[])
            ),
        }
        write_event("chat", record)
        print(
            f"[emote] {record['unique_id']}: "
            f"{len(record['emotes'])} sticker(s)"
        )

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        record = {
            **user_fields(
                safe_get(event, "user")
            ),
            "like_count": safe_get(
                event, "count", "like_count",
                default=0,
            ),
            "total_likes": safe_get(
                event, "total", "total_likes"
            ),
        }
        write_event("like", record)

    @client.on(JoinEvent)
    async def on_join(event: JoinEvent):
        record = {
            **user_fields(
                safe_get(event, "user")
            ),
        }
        write_event("member", record)

    @client.on(FollowEvent)
    async def on_follow(event: FollowEvent):
        write_event(
            "follow",
            user_fields(
                safe_get(event, "user")
            ),
        )

    @client.on(ShareEvent)
    async def on_share(event: ShareEvent):
        write_event(
            "share",
            user_fields(
                safe_get(event, "user")
            ),
        )

    @client.on(RoomUserSeqEvent)
    async def on_viewers(
        event: RoomUserSeqEvent
    ):
        viewer_count = safe_get(
            event,
            "viewer_count",
            "viewerCount",
            "total",
        )
        write_event(
            "viewer",
            {"viewer_count": viewer_count},
        )
        print(
            f"[viewer] {viewer_count}"
        )

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        gift = safe_get(event, "gift")
        if gift is None:
            return

        gift_type = safe_get(
            gift, "type", "gift_type"
        )
        repeat_count = safe_get(
            event,
            "repeat_count",
            default=1,
        )
        repeat_end = safe_get(
            event,
            "repeat_end",
            default=None,
        )
        streaking = safe_get(
            event,
            "streaking",
            default=False,
        )

        # Canonical TikTokLive logic:
        # gift type 1 = streakable.
        counted = (
            gift_type != 1
            or not streaking
        )

        diamond_per_gift = safe_get(
            gift,
            "diamond_count",
            "diamondCount",
            default=None,
        )

        diamond_total = None
        if (
            counted
            and diamond_per_gift is not None
        ):
            diamond_total = (
                diamond_per_gift
                * (repeat_count or 1)
            )

        record = {
            **user_fields(
                safe_get(event, "user")
            ),
            "gift_id": safe_get(
                gift, "id", "gift_id"
            ),
            "gift_name": safe_get(
                gift, "name", "gift_name"
            ),
            "gift_type": gift_type,
            "repeat_count": repeat_count,
            "repeat_end": repeat_end,
            "streaking": bool(streaking),
            "diamond_per_gift": (
                diamond_per_gift
            ),
            "counted": counted,
            "diamond_total": diamond_total,
        }
        write_event("gift", record)

        state_name = (
            "COUNT" if counted else "STREAK"
        )
        print(
            f"[gift:{state_name}] "
            f"{record['unique_id']} -> "
            f"{record['gift_name']} "
            f"x{repeat_count}"
        )

    exit_status = "stopped"

    try:
        client.run(
            fetch_room_info=True,
            fetch_gift_info=True,
        )
    except KeyboardInterrupt:
        exit_status = "stopped_by_user"
    except Exception as exc:
        exit_status = "error"
        print(
            f"[fatal] "
            f"{type(exc).__name__}: {exc}"
        )
        raise
    finally:
        state["ended_ms"] = now_ms()

        # Close any currently-open connected interval.
        if state["connected_since_ms"] is not None:
            state["connected_seconds"] += (
                state["ended_ms"]
                - state["connected_since_ms"]
            ) / 1000.0
            state["connected_since_ms"] = None

        total_seconds = (
            state["ended_ms"]
            - state["started_ms"]
        ) / 1000.0

        uptime = (
            state["connected_seconds"]
            / total_seconds
            if total_seconds > 0
            else None
        )

        summary = {
            "backend": "TikTokLive",
            "username": username,
            "room_id": state["room_id"],
            "status": exit_status,
            "started_at_utc": iso_utc(
                state["started_ms"]
            ),
            "ended_at_utc": iso_utc(
                state["ended_ms"]
            ),
            "collector_total_seconds": round(
                total_seconds, 3
            ),
            "socket_connected_seconds": round(
                state["connected_seconds"], 3
            ),
            "socket_uptime_ratio": (
                round(uptime, 4)
                if uptime is not None
                else None
            ),
            "socket_gap_seconds": round(
                max(
                    0.0,
                    total_seconds
                    - state["connected_seconds"],
                ),
                3,
            ),
            "connection_count": (
                state["connection_count"]
            ),
            "disconnect_count": (
                state["disconnect_count"]
            ),
            "live_ended": (
                state["live_ended"]
            ),
            "event_counts": dict(
                state["counts"]
            ),
        }

        fp.close()
        summary_path.write_text(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print()
        print(
            f"[summary] {summary_path}"
        )
        print(
            f"[uptime]  "
            f"{summary['socket_uptime_ratio']}"
        )
        print(
            f"[counts]  "
            f"{summary['event_counts']}"
        )


if __name__ == "__main__":
    main()
