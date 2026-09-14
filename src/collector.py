import argparse
import asyncio
import hashlib
import json
import platform
import re
import signal
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from zoneinfo import ZoneInfo

from tiktok_live_events import TikTokLive


COLLECTOR_VERSION = "0.3"
SCHEMA_VERSION = "0.3"
DEFAULT_TIMEZONE = "Asia/Taipei"


class TeeStream:
    """Mirror collector stdout/stderr to the per-session log file."""

    def __init__(self, primary, log_fp):
        self.primary = primary
        self.log_fp = log_fp

    def write(self, data):
        self.primary.write(data)
        self.log_fp.write(data)
        self.flush()

    def flush(self):
        self.primary.flush()
        self.log_fp.flush()



def now_ms():
    return time.time_ns() // 1_000_000


def iso_utc_from_ms(timestamp_ms):
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).isoformat()


def iso_local_from_ms(timestamp_ms, tz):
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).astimezone(tz).isoformat()


def package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def first_present(mapping, *keys):
    for key in keys:
        if mapping.get(key) is not None:
            return mapping.get(key)
    return None


def compact_badges(user):
    raw = user.get("badges") or user.get("userBadges") or []
    if not isinstance(raw, list):
        return []

    result = []

    for badge in raw[:20]:
        if isinstance(badge, str):
            result.append({
                "name": badge,
                "kind": None,
                "level": None,
                "url": None,
            })
            continue

        if not isinstance(badge, dict):
            continue

        url = badge.get("url")
        name = badge.get("name")
        kind = None
        level = None

        haystack = f"{name or ''} {url or ''}".lower()

        fan_match = re.search(
            r"(?:fans_badge_icon|fan_badge_icon)_lv(\d+)",
            haystack,
        )
        gifter_match = re.search(
            r"grade_badge_icon(?:_lite)?_lv(\d+)",
            haystack,
        )

        if fan_match:
            kind = "fan"
            level = int(fan_match.group(1))
        elif gifter_match:
            kind = "gifter"
            level = int(gifter_match.group(1))
        elif "moderater_badge_icon" in haystack or "moderator" in haystack:
            kind = "moderator"
        elif "top_gifter" in haystack:
            kind = "top_gifter"
        elif "subscriber" in haystack or "subscription" in haystack:
            kind = "subscriber"

        item = {
            "name": name,
            "kind": kind,
            "level": level,
            "url": url,
        }

        # Keep a few scalar fields if the backend exposes them.
        for source_key, target_key in [
            ("type", "type"),
            ("displayType", "display_type"),
            ("badgeSceneType", "scene_type"),
        ]:
            value = badge.get(source_key)
            if value is not None and isinstance(
                value, (str, int, float, bool)
            ):
                item[target_key] = value

        result.append(item)

    return result


def compact_user_snapshot(user):
    if not isinstance(user, dict) or not user:
        return None

    follow_info = user.get("followInfo")
    if not isinstance(follow_info, dict):
        follow_info = {}

    fan_info = user.get("fanClub")
    if not isinstance(fan_info, dict):
        fan_info = {}

    badges = compact_badges(user)

    badge_fan_levels = [
        b["level"]
        for b in badges
        if b.get("kind") == "fan" and b.get("level") is not None
    ]
    badge_gifter_levels = [
        b["level"]
        for b in badges
        if b.get("kind") == "gifter" and b.get("level") is not None
    ]

    explicit_moderator = first_present(
        user, "isModerator", "is_moderator"
    )
    explicit_subscriber = first_present(
        user, "isSubscriber", "is_subscriber"
    )

    inferred_moderator = any(
        b.get("kind") == "moderator" for b in badges
    )
    inferred_subscriber = any(
        b.get("kind") == "subscriber" for b in badges
    )

    fan_level = (
        first_present(
            user,
            "fanLevel",
            "fan_level",
            "fanClubLevel",
        )
        or first_present(fan_info, "level", "fanLevel")
    )
    if fan_level is None and badge_fan_levels:
        fan_level = max(badge_fan_levels)

    gifter_level = first_present(
        user, "gifterLevel", "giftLevel", "gifter_level"
    )
    if gifter_level is None and badge_gifter_levels:
        gifter_level = max(badge_gifter_levels)

    return {
        "user_id": first_present(user, "id", "userId"),
        "unique_id": first_present(user, "uniqueId", "displayId"),
        "nickname": user.get("nickname"),
        "is_moderator": (
            explicit_moderator
            if explicit_moderator is not None
            else inferred_moderator
        ),
        "is_subscriber": (
            explicit_subscriber
            if explicit_subscriber is not None
            else inferred_subscriber
        ),
        "is_verified": first_present(
            user, "isVerified", "is_verified"
        ),
        "follow_role": first_present(
            user, "followRole", "follow_role"
        ),
        "gifter_level": gifter_level,
        "fan_level": fan_level,
        "follower_count": first_present(
            follow_info, "followerCount", "follower_count"
        ),
        "following_count": first_present(
            follow_info, "followingCount", "following_count"
        ),
        "follow_status": first_present(
            follow_info, "followStatus", "follow_status"
        ),
        "badges": badges,
    }


def event_user_fields(event):
    user = event.get("user") or {}
    return {
        "user_id": first_present(user, "id", "userId"),
        "unique_id": first_present(user, "uniqueId", "displayId"),
        "nickname": user.get("nickname"),
    }


def compact_emotes(event):
    raw = event.get("emotes") or []
    if not isinstance(raw, list):
        return []

    result = []
    for emote in raw[:30]:
        if isinstance(emote, str):
            result.append({"name": emote})
            continue
        if not isinstance(emote, dict):
            continue

        item = {}
        for source_key, target_key in [
            ("id", "id"),
            ("emoteId", "id"),
            ("name", "name"),
            ("emoteName", "name"),
            ("placeInComment", "place_in_comment"),
        ]:
            value = emote.get(source_key)
            if value is not None and isinstance(value, (str, int, float, bool)):
                item[target_key] = value

        if item:
            result.append(item)

    return result


def scalar_payload(event, excluded=None):
    """Keep only lightweight scalar fields from less-structured events."""
    excluded = set(excluded or [])
    excluded.update({
        "raw",
        "extras",
        "user",
        "replyToUser",
        "emotes",
        "teams",
        "hosts",
        "relationship",
    })

    payload = {}
    for key, value in event.items():
        if key in excluded:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
    return payload


def infer_social_action(event):
    text = " ".join(
        str(event.get(k) or "")
        for k in ("displayType", "label", "action", "type")
    ).lower()

    if "follow" in text:
        return "follow"
    if "share" in text:
        return "share"
    return "other"


def main():
    parser = argparse.ArgumentParser(
        description="TikTok LIVE long-session analytics collector v0.3"
    )
    parser.add_argument(
        "username",
        help="TikTok LIVE username, with or without @",
    )
    parser.add_argument(
        "--output-root",
        default="data/raw",
        help="Root directory for captured sessions (default: data/raw)",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Display timezone metadata (default: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--max-reconnect-attempts",
        type=int,
        default=20,
        help="Automatic reconnect attempts (default: 20)",
    )
    parser.add_argument(
        "--offline-confirmations",
        type=int,
        default=3,
        help="End the session after this many consecutive authoritative offline responses (default: 3)",
    )
    args = parser.parse_args()

    username = args.username.lstrip("@")
    local_tz = ZoneInfo(args.timezone)

    local_now = datetime.now(local_tz)
    session_id = local_now.strftime("%Y%m%d_%H%M%S") + f"_{username}"

    session_dir = Path(args.output_root) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    events_path = session_dir / "events.ndjson"
    raw_events_path = session_dir / "raw_events.ndjson"
    users_path = session_dir / "users.ndjson"
    diagnostics_path = session_dir / "diagnostics.ndjson"
    session_path = session_dir / "session.json"
    collector_log_path = session_dir / "collector.log"

    events_fp = events_path.open(
        "a", encoding="utf-8", buffering=1
    )
    raw_events_fp = raw_events_path.open(
        "a", encoding="utf-8", buffering=1
    )
    users_fp = users_path.open(
        "a", encoding="utf-8", buffering=1
    )
    diagnostics_fp = diagnostics_path.open(
        "a", encoding="utf-8", buffering=1
    )
    collector_log_fp = collector_log_path.open(
        "a", encoding="utf-8", buffering=1
    )
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TeeStream(original_stdout, collector_log_fp)
    sys.stderr = TeeStream(original_stderr, collector_log_fp)

    state = {
        "seq": 0,
        "connection_id": 0,
        "room_id": None,
        "event_counts": Counter(),
        "user_hashes": {},
        "last_checkpoint_monotonic": time.monotonic(),
        "last_received_at_utc": None,
        "seen_event_keys": set(),
        "seen_event_order": deque(),
        "dedupe_limit": 250000,
        "duplicate_event_counts": Counter(),
        "rate_limited": False,
        "error_event_count": 0,
        "offline_misses": 0,
        "offline_confirmed": False,
        "live_end_detected": False,
        "raw_event_count": 0,
    }

    session_meta = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "session_id": session_id,
        "username": username,
        "room_id": None,
        "status": "running",
        "timezone": args.timezone,
        "collector_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "collector_started_at_local": datetime.now(local_tz).isoformat(),
        "collector_ended_at_utc": None,
        "collector_ended_at_local": None,
        "room_info": {
            "ws_host": None,
            "cluster_region": None,
            "connected_at": None,
        },
        "connection_count": 0,
        "reconnect_count": 0,
        "disconnect_count": 0,
        "connection_history": [],
        "reconnect_strategy": {
            "owner": "collector",
            "base_delay_seconds": 1,
            "max_delay_seconds": 60,
            "sdk_auto_reconnect": False
        },
        "duplicate_event_counts": {},
        "data_quality": {
            "collector_total_seconds": None,
            "socket_connected_seconds": None,
            "socket_uptime_ratio": None,
            "socket_gap_seconds": None
        },
        "event_types": [
            "chat",
            "like",
            "gift",
            "roomUserSeq",
            "member",
            "social",
            "subscribe",
            "control",
            "room",
        ],
        "event_counts": {},
        "raw_event_file": raw_events_path.name,
        "raw_event_count": 0,
        "collector_log_file": collector_log_path.name,
        "live_end_detected": False,
        "offline_confirmed": False,
        "offline_miss_count": 0,
        "offline_confirmation_threshold": args.offline_confirmations,
        "last_received_at_utc": None,
        "last_received_at_local": None,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "tiktok_live_events": package_version("tiktok-live-events"),
            "websockets": package_version("websockets"),
        },
        "notes": {
            "timestamp_ms": "TikTok/event timestamp when available.",
            "received_at_ms": "Local collector wall-clock receive timestamp.",
            "viewer_count": "Observed periodic room viewer sample, not official ACU.",
            "diamond_total": "Counted only when non-streakable or streak completed.",
        },
    }

    def atomic_save_session():
        session_meta["room_id"] = state["room_id"]
        session_meta["event_counts"] = dict(state["event_counts"])
        session_meta["last_received_at_utc"] = state["last_received_at_utc"]
        if state["last_received_at_utc"]:
            session_meta["last_received_at_local"] = iso_local_from_ms(
                int(datetime.fromisoformat(
                    state["last_received_at_utc"]
                ).timestamp() * 1000),
                local_tz,
            )
        session_meta["offline_confirmed"] = state["offline_confirmed"]
        session_meta["offline_miss_count"] = state["offline_misses"]
        session_meta["duplicate_event_counts"] = dict(
            state["duplicate_event_counts"]
        )
        session_meta["raw_event_count"] = state["raw_event_count"]

        tmp = session_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(session_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(session_path)

    def maybe_checkpoint(force=False):
        now_mono = time.monotonic()
        if force or now_mono - state["last_checkpoint_monotonic"] >= 30:
            atomic_save_session()
            state["last_checkpoint_monotonic"] = now_mono

    def capture_user(user, observed_at_ms):
        snapshot = compact_user_snapshot(user)
        if not snapshot:
            return

        key = snapshot.get("user_id") or snapshot.get("unique_id")
        if not key:
            return

        canonical = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()

        if state["user_hashes"].get(str(key)) == digest:
            return

        state["user_hashes"][str(key)] = digest

        row = {
            "session_id": session_id,
            "room_id": state["room_id"],
            "observed_at_ms": observed_at_ms,
            "observed_at_utc": iso_utc_from_ms(observed_at_ms),
            "observed_at_local": iso_local_from_ms(
                observed_at_ms, local_tz
            ),
            **snapshot,
        }
        users_fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        users_fp.flush()

    def event_dedupe_key(record):
        event_type = record.get("type")

        if event_type in ("system", "room", "control"):
            return None

        if event_type == "gift":
            transaction_id = record.get("transaction_id")
            if transaction_id:
                return f"gift:tx:{transaction_id}"

        if event_type == "chat":
            message_uuid = record.get("message_uuid")
            if message_uuid:
                return f"chat:uuid:{message_uuid}"

        msg_id = record.get("msg_id")
        if msg_id:
            return f"{event_type}:msg:{msg_id}"

        return None

    def is_duplicate(record):
        key = event_dedupe_key(record)
        if key is None:
            return False

        seen = state["seen_event_keys"]
        order = state["seen_event_order"]

        if key in seen:
            state["duplicate_event_counts"][
                record.get("type", "unknown")
            ] += 1
            return True

        seen.add(key)
        order.append(key)

        while len(order) > state["dedupe_limit"]:
            old = order.popleft()
            seen.discard(old)

        return False

    def write_event(record, source_event=None):
        if is_duplicate(record):
            maybe_checkpoint()
            return False

        received = record.get("received_at_ms") or now_ms()
        record["received_at_ms"] = received
        record["received_at_utc"] = iso_utc_from_ms(received)
        record["received_at_local"] = iso_local_from_ms(
            received, local_tz
        )

        record.setdefault("schema_version", SCHEMA_VERSION)
        record.setdefault("session_id", session_id)
        record.setdefault("room_id", state["room_id"])
        record.setdefault("connection_id", state["connection_id"])

        state["seq"] += 1
        record["seq"] = state["seq"]

        state["last_received_at_utc"] = record["received_at_utc"]

        event_type = record.get("type", "unknown")
        state["event_counts"][event_type] += 1

        events_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        events_fp.flush()

        if source_event:
            capture_user(source_event.get("user"), received)

        maybe_checkpoint()
        return True

    def write_raw_event(event):
        """Persist every SDK event before type-specific normalization."""
        if not isinstance(event, dict):
            return

        received = now_ms()
        row = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "room_id": state["room_id"],
            "connection_id": state["connection_id"],
            "received_at_ms": received,
            "received_at_utc": iso_utc_from_ms(received),
            "received_at_local": iso_local_from_ms(
                received, local_tz
            ),
            "event_type": event.get("type"),
            "timestamp_ms": event.get("timestamp"),
            "msg_id": event.get("msgId"),
            "payload": event,
        }

        raw_events_fp.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )
        raw_events_fp.flush()
        state["raw_event_count"] += 1

    def base_record(event, event_type):
        received = now_ms()
        timestamp_ms = event.get("timestamp") if isinstance(event, dict) else None

        return {
            "type": event_type,
            "timestamp_ms": timestamp_ms,
            "timestamp_utc": iso_utc_from_ms(timestamp_ms),
            "timestamp_local": iso_local_from_ms(
                timestamp_ms, local_tz
            ),
            "received_at_ms": received,
            "msg_id": event.get("msgId") if isinstance(event, dict) else None,
            "proto_version": (
                event.get("protoVersion")
                if isinstance(event, dict)
                else None
            ),
        }

    atomic_save_session()

    live = TikTokLive(
        username,
        auto_reconnect=False,
        max_reconnect_attempts=0,
    )

    @live.on("connected")
    def on_connected(e):
        state["offline_misses"] = 0
        state["offline_confirmed"] = False
        session_meta["offline_confirmed"] = False
        received = now_ms()

        state["connection_id"] += 1
        session_meta["connection_count"] += 1
        session_meta["reconnect_count"] = max(
            0, session_meta["connection_count"] - 1
        )

        connection = {
            "connection_id": state["connection_id"],
            "connected_at_utc": iso_utc_from_ms(received),
            "connected_at_local": iso_local_from_ms(
                received, local_tz
            ),
            "disconnected_at_utc": None,
            "disconnected_at_local": None,
            "close_code": None,
            "close_reason": None,
        }
        session_meta["connection_history"].append(connection)

        write_event({
            "type": "system",
            "system_event": (
                "connected"
                if state["connection_id"] == 1
                else "reconnected"
            ),
            "timestamp_ms": None,
            "timestamp_utc": None,
            "timestamp_local": None,
            "received_at_ms": received,
        })

        maybe_checkpoint(force=True)

        if state["connection_id"] == 1:
            print(f"[connected] @{username}")
        else:
            print(
                f"[reconnected] @{username} "
                f"(connection {state['connection_id']})"
            )
        print(f"[output]    {events_path}")

    @live.on("disconnected")
    def on_disconnected(e):
        e = e or {}
        received = now_ms()

        session_meta["disconnect_count"] += 1

        close_code = first_present(
            e, "code", "closeCode", "close_code"
        )
        close_reason = first_present(
            e, "reason", "closeReason", "close_reason"
        )

        for item in reversed(session_meta["connection_history"]):
            if item["disconnected_at_utc"] is None:
                item["disconnected_at_utc"] = iso_utc_from_ms(received)
                item["disconnected_at_local"] = iso_local_from_ms(
                    received, local_tz
                )
                item["close_code"] = close_code
                item["close_reason"] = close_reason
                break

        write_event({
            "type": "system",
            "system_event": "disconnected",
            "timestamp_ms": e.get("timestamp"),
            "timestamp_utc": iso_utc_from_ms(e.get("timestamp")),
            "timestamp_local": iso_local_from_ms(
                e.get("timestamp"), local_tz
            ),
            "received_at_ms": received,
            "close_code": close_code,
            "close_reason": close_reason,
        })

        maybe_checkpoint(force=True)

        print(
            f"[disconnected] code={close_code} "
            f"reason={close_reason}"
        )

    @live.on("roomInfo")
    def on_room_info(e):
        e = e or {}
        received = now_ms()

        room_id = first_present(e, "roomId", "room_id")
        if room_id is not None:
            state["room_id"] = str(room_id)

        session_meta["room_info"] = {
            "ws_host": first_present(e, "wsHost", "ws_host"),
            "cluster_region": first_present(
                e, "clusterRegion", "cluster_region"
            ),
            "connected_at": first_present(
                e, "connectedAt", "connected_at"
            ),
        }

        write_event({
            "type": "system",
            "system_event": "room_info",
            "timestamp_ms": None,
            "timestamp_utc": None,
            "timestamp_local": None,
            "received_at_ms": received,
            "room_id": state["room_id"],
            "ws_host": session_meta["room_info"]["ws_host"],
            "cluster_region": session_meta["room_info"]["cluster_region"],
            "connected_at": session_meta["room_info"]["connected_at"],
        })

        maybe_checkpoint(force=True)
        print(
            f"[room]      room_id={state['room_id']} "
            f"region={session_meta['room_info']['cluster_region']}"
        )

    @live.on("chat")
    def on_chat(e):
        record = {
            **base_record(e, "chat"),
            **event_user_fields(e),
            "comment": e.get("comment"),
            "language": e.get("language"),
            "message_uuid": e.get("messageUuid"),
            "starred": e.get("starred"),
            "emotes": compact_emotes(e),
        }

        reply_user = e.get("replyToUser")
        if isinstance(reply_user, dict) and reply_user:
            record["reply_to"] = {
                "user_id": first_present(reply_user, "id", "userId"),
                "unique_id": first_present(
                    reply_user, "uniqueId", "displayId"
                ),
                "nickname": reply_user.get("nickname"),
            }
        else:
            record["reply_to"] = None

        if not write_event(record, e):
            return

        print(
            f"[chat]   {record['unique_id']}: "
            f"{record['comment'] or '[emote]'}"
        )

    @live.on("like")
    def on_like(e):
        record = {
            **base_record(e, "like"),
            **event_user_fields(e),
            "like_count": e.get("likeCount", 0),
            "total_likes": e.get("totalLikes"),
        }

        if not write_event(record, e):
            return

        print(
            f"[like]   {record['unique_id']} "
            f"+{record['like_count']} "
            f"(total={record['total_likes']})"
        )

    @live.on("gift")
    def on_gift(e):
        gift_type = e.get("giftType")
        repeat_end = bool(e.get("repeatEnd"))
        repeat_count = e.get("repeatCount", 1) or 1
        diamond_count = e.get("diamondCount", 0) or 0

        count_this_event = (gift_type != 1) or repeat_end
        diamond_total = (
            diamond_count * repeat_count if count_this_event else 0
        )

        relationship = e.get("relationship")
        if not isinstance(relationship, dict):
            relationship = {}

        record = {
            **base_record(e, "gift"),
            **event_user_fields(e),
            "sender_user_id": e.get("senderUserId"),
            "gift_id": e.get("giftId"),
            "gift_name": e.get("giftName"),
            "gift_type": gift_type,
            "diamond_per_gift": diamond_count,
            "repeat_count": repeat_count,
            "repeat_end": repeat_end,
            "group_id": e.get("groupId"),
            "transaction_id": e.get("transactionId"),
            "join_day_number": relationship.get("joinDayNumber"),
            "counted": count_this_event,
            "diamond_total": diamond_total,
        }

        if not write_event(record, e):
            return

        state_name = "COUNT" if count_this_event else "STREAK"
        print(
            f"[gift:{state_name}] {record['unique_id']} -> "
            f"{record['gift_name']} x{repeat_count} "
            f"({diamond_total} diamonds)"
        )

    @live.on("roomUserSeq")
    def on_viewer(e):
        record = {
            **base_record(e, "viewer"),
            "viewer_count": e.get("viewerCount"),
        }

        if not write_event(record):
            return
        print(f"[viewer] {record['viewer_count']}")

    @live.on("member")
    def on_member(e):
        record = {
            **base_record(e, "member"),
            **event_user_fields(e),
            "action_code": e.get("actionCode"),
            "entry_source": e.get("entrySource"),
            "entry_action": e.get("entryAction"),
            "entry_type": e.get("entryType"),
        }

        if not write_event(record, e):
            return

        source = record["entry_source"] or "unknown"
        print(
            f"[join]   {record['unique_id']} "
            f"(source={source})"
        )

    @live.on("social")
    def on_social(e):
        record = {
            **base_record(e, "social"),
            **event_user_fields(e),
            "social_action": infer_social_action(e),
            "display_type": e.get("displayType"),
            "label": e.get("label"),
        }

        if not write_event(record, e):
            return

        print(
            f"[social:{record['social_action']}] "
            f"{record['unique_id']}"
        )

    @live.on("subscribe")
    def on_subscribe(e):
        record = {
            **base_record(e, "subscribe"),
            **event_user_fields(e),
            "payload": scalar_payload(
                e,
                excluded={
                    "type",
                    "timestamp",
                    "msgId",
                    "protoVersion",
                },
            ),
        }

        if not write_event(record, e):
            return
        print(f"[subscribe] {record['unique_id']}")

    @live.on("control")
    def on_control(e):
        record = {
            **base_record(e, "control"),
            "payload": scalar_payload(
                e,
                excluded={
                    "type",
                    "timestamp",
                    "msgId",
                    "protoVersion",
                },
            ),
        }
        write_event(record)
        print(f"[control] {record['payload']}")

    @live.on("room")
    def on_room(e):
        record = {
            **base_record(e, "room"),
            "payload": scalar_payload(
                e,
                excluded={
                    "type",
                    "timestamp",
                    "msgId",
                    "protoVersion",
                },
            ),
        }
        write_event(record)

    @live.on("event")
    def on_any_event(e):
        write_raw_event(e)

    @live.on("live_end")
    def on_live_end(e):
        event = e or {}
        state["live_end_detected"] = True
        session_meta["live_end_detected"] = True
        write_event({
            "type": "system",
            "system_event": "live_end",
            "timestamp_ms": event.get("timestamp"),
            "timestamp_utc": iso_utc_from_ms(event.get("timestamp")),
            "timestamp_local": iso_local_from_ms(
                event.get("timestamp"), local_tz
            ),
            "received_at_ms": now_ms(),
        })
        live.stop()

    @live.on("error")
    def on_error(e):
        e = e or {}
        state["error_event_count"] += 1
        error_text = str(e.get("error") or e)
        if "is not currently live" in error_text.lower():
            state["offline_misses"] += 1
        else:
            # 429, timeout, and transport errors break an authoritative
            # offline sequence and must not close a captured session.
            state["offline_misses"] = 0
        session_meta["offline_miss_count"] = state["offline_misses"]
        received = now_ms()

        row = {
            "session_id": session_id,
            "room_id": state["room_id"],
            "connection_id": state["connection_id"],
            "received_at_ms": received,
            "received_at_utc": iso_utc_from_ms(received),
            "kind": "sdk_error",
            "payload": scalar_payload(e),
        }
        diagnostics_fp.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )
        diagnostics_fp.flush()

        print(f"[error] {e.get('error') or e}")

    @live.on("rate_limited")
    def on_rate_limited(e):
        state["rate_limited"] = True
        received = now_ms()

        row = {
            "session_id": session_id,
            "room_id": state["room_id"],
            "connection_id": state["connection_id"],
            "received_at_ms": received,
            "received_at_utc": iso_utc_from_ms(received),
            "kind": "rate_limited",
            "payload": scalar_payload(e),
        }
        diagnostics_fp.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )
        diagnostics_fp.flush()

        print(f"[rate_limited] {e}")

    @live.on("unknown")
    def on_unknown(e):
        e = e or {}
        received = now_ms()

        row = {
            "session_id": session_id,
            "room_id": state["room_id"],
            "connection_id": state["connection_id"],
            "received_at_ms": received,
            "received_at_utc": iso_utc_from_ms(received),
            "kind": "unknown_event",
            "event_type": e.get("type"),
            "timestamp_ms": e.get("timestamp"),
            "msg_id": e.get("msgId"),
            "proto_version": e.get("protoVersion"),
            "scalar_payload": scalar_payload(e),
            "extra_keys": sorted(
                list((e.get("extras") or {}).keys())
            ) if isinstance(e.get("extras"), dict) else [],
        }

        diagnostics_fp.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )
        diagnostics_fp.flush()

    # Allow watcher.py to stop this process gracefully.
    # On Windows, watcher sends CTRL_BREAK_EVENT to the collector's
    # dedicated process group; map SIGBREAK to KeyboardInterrupt so the
    # normal finally block writes the final session metadata.
    def _graceful_signal_handler(signum, frame):
        raise KeyboardInterrupt

    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _graceful_signal_handler)

    signal.signal(signal.SIGTERM, _graceful_signal_handler)

    exit_status = "stopped"
    error_text = None

    async def run_resilient():
        reconnect_delay = 1

        while True:
            if state["rate_limited"]:
                return

            started = time.monotonic()

            await live.run()

            if state["live_end_detected"]:
                return

            if state["rate_limited"]:
                return

            if state["offline_misses"] >= args.offline_confirmations:
                state["offline_confirmed"] = True
                session_meta["offline_confirmed"] = True
                print(
                    f"[offline] {args.offline_confirmations} consecutive "
                    "authoritative offline responses"
                )
                return

            connected_cycle_seconds = time.monotonic() - started

            # A reasonably healthy cycle resets backoff. Very short failures
            # back off progressively to avoid hammering the endpoint.
            if connected_cycle_seconds >= 20:
                reconnect_delay = 1
            else:
                reconnect_delay = min(reconnect_delay * 2, 60)

            print(
                f"[retry] reconnecting in {reconnect_delay}s ..."
            )
            await asyncio.sleep(reconnect_delay)

    try:
        asyncio.run(run_resilient())
        if state["live_end_detected"]:
            exit_status = "live_end"
        elif state["offline_confirmed"]:
            exit_status = "offline_confirmed"
        elif state["rate_limited"]:
            exit_status = "rate_limited"
    except KeyboardInterrupt:
        exit_status = "stopped_by_user"
    except Exception as exc:
        exit_status = "error"
        error_text = f"{type(exc).__name__}: {exc}"
        print(f"[fatal] {error_text}")
        raise
    finally:
        ended_ms = now_ms()

        session_meta["status"] = exit_status
        session_meta["error"] = error_text
        session_meta["collector_ended_at_utc"] = iso_utc_from_ms(
            ended_ms
        )
        session_meta["collector_ended_at_local"] = iso_local_from_ms(
            ended_ms, local_tz
        )

        # If Ctrl+C occurs before a disconnected callback, close the open
        # connection interval locally for bookkeeping.
        for item in reversed(session_meta["connection_history"]):
            if item["disconnected_at_utc"] is None:
                item["disconnected_at_utc"] = iso_utc_from_ms(ended_ms)
                item["disconnected_at_local"] = iso_local_from_ms(
                    ended_ms, local_tz
                )
                item["close_reason"] = (
                    item["close_reason"] or exit_status
                )
                break

        # Compute socket-coverage diagnostics.
        collector_start = datetime.fromisoformat(
            session_meta["collector_started_at_utc"]
        )
        collector_end = datetime.fromisoformat(
            session_meta["collector_ended_at_utc"]
        )
        collector_total = max(
            0.0, (collector_end - collector_start).total_seconds()
        )

        connected_seconds = 0.0
        for item in session_meta["connection_history"]:
            if not item.get("connected_at_utc"):
                continue
            connection_start = datetime.fromisoformat(
                item["connected_at_utc"]
            )
            connection_end_text = item.get("disconnected_at_utc")
            if not connection_end_text:
                continue
            connection_end = datetime.fromisoformat(
                connection_end_text
            )
            connected_seconds += max(
                0.0,
                (connection_end - connection_start).total_seconds(),
            )

        gap_seconds = max(
            0.0, collector_total - connected_seconds
        )

        session_meta["data_quality"] = {
            "collector_total_seconds": round(collector_total, 3),
            "socket_connected_seconds": round(
                connected_seconds, 3
            ),
            "socket_uptime_ratio": (
                round(connected_seconds / collector_total, 4)
                if collector_total > 0
                else None
            ),
            "socket_gap_seconds": round(gap_seconds, 3),
            "duplicate_events_dropped": int(
                sum(state["duplicate_event_counts"].values())
            ),
            "sdk_error_events": state["error_event_count"],
        }

        maybe_checkpoint(force=True)

        events_fp.close()
        raw_events_fp.close()
        users_fp.close()
        diagnostics_fp.close()

        print()
        print(f"[stopped] status={exit_status}")
        print(f"[session] {session_dir}")
        print(f"[counts]  {dict(state['event_counts'])}")
        print(
            f"[dedupe]  "
            f"{dict(state['duplicate_event_counts'])}"
        )
        print(
            f"[uptime]  "
            f"{session_meta['data_quality']['socket_uptime_ratio']}"
        )

        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        collector_log_fp.close()



if __name__ == "__main__":
    main()
