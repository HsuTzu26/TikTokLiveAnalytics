"""Passively observe TikTok LIVE WebSocket frames received by Chromium.

This experiment never opens a WebSocket itself. Chromium owns the page
connection; the probe subscribes to its Chrome DevTools Protocol events.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import gzip
import json
import os
import re
import time
import zlib
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from src.live_summary import write_live_summary
from src.providers.base import BaseEventProvider, NormalizedEventBus
from src.tts.gift_catalog import gift_metadata_for


TARGET_HOST = "webcast-ws.tiktok.com"
LIVE_ENDPOINT_PATHS = ("/api-live/user/room", "/webcast/room/check_alive/")
GZIP_MAGIC = b"\x1f\x8b"
METHOD_RE = re.compile(rb"Webcast[A-Za-z0-9_]+Message")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
VERIFICATION_RE = re.compile(
    r"captcha|human verification|verify to continue|security check|"
    r"drag the slider|verification required",
    re.IGNORECASE,
)
LOGIN_REQUIRED_RE = re.compile(
    r"log in to continue|sign in to continue|log in to watch|log in to view|"
    r"you need to log in",
    re.IGNORECASE,
)

METHOD_LABELS = {
    "WebcastChatMessage": "CHAT",
    "WebcastGiftMessage": "GIFT",
    "WebcastRoomUserSeqMessage": "VIEWER",
    "WebcastLikeMessage": "LIKE",
    "WebcastMemberMessage": "MEMBER",
    "WebcastControlMessage": "CONTROL",
}
LIVE_EVENT_METHODS = frozenset(METHOD_LABELS)


class ProtobufDecodeError(ValueError):
    """Raised when a protobuf wire value is truncated or invalid."""


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sanitize_url(url: str) -> str:
    """Remove credentials, query parameters, and fragments from a URL."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if not hostname:
            return ""
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return ""


def is_target_websocket(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").casefold() == TARGET_HOST
    except ValueError:
        return False


def is_candidate_websocket(url: str) -> bool:
    """Classify likely TikTok LIVE sockets without assuming one host forever."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    tiktok_host = (
        hostname == "tiktok.com"
        or hostname.endswith(".tiktok.com")
        or "tiktok" in hostname
    )
    return is_target_websocket(url) or (
        tiktok_host
        and (
            "webcast" in hostname
            or "/webcast/" in path
            or re.search(r"(?:^|[.-])ws(?:[.-]|$)", hostname) is not None
        )
    )


def is_live_candidate_websocket(url: str) -> bool:
    """Identify Webcast transport sockets while permitting TikTok host changes."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    tiktok_host = (
        hostname == "tiktok.com"
        or hostname.endswith(".tiktok.com")
        or "tiktok" in hostname
    )
    return is_target_websocket(url) or (
        tiktok_host and ("webcast" in hostname or "/webcast/" in path)
    )


def websocket_hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def parse_live_preflight(payload: object) -> dict[str, Any]:
    """Extract only explicit LIVE booleans/text and room IDs from observed JSON."""
    room_id: str | None = None
    live_signal: bool | None = None
    username: str | None = None
    source_timestamp: str | int | float | None = None
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                normalized_key = str(key).casefold().replace("_", "")
                if normalized_key in {"roomid", "room_id"} and value not in (None, "", 0, "0"):
                    room_id = room_id or str(value)
                elif normalized_key in {"uniqueid", "username", "unique_id"} and isinstance(value, str):
                    username = username or value.lstrip("@").strip()
                elif normalized_key in {"islive", "isliving", "hasliveroom"}:
                    if value is True or value is False:
                        live_signal = value
                    elif isinstance(value, str):
                        state = value.strip().casefold()
                        if state in {"true", "live", "live_now", "online"}:
                            live_signal = True
                        elif state in {"false", "offline", "not_live"}:
                            live_signal = False
                elif normalized_key in {"livestatus", "roomstatus"} and isinstance(value, str):
                    state = value.strip().casefold()
                    if state in {"live", "live_now", "online"}:
                        live_signal = True
                    elif state in {"offline", "not_live"}:
                        live_signal = False
                elif normalized_key in {"timestamp", "createtime", "updatetime", "updatedat"}:
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        source_timestamp = source_timestamp or value
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    status = "UNKNOWN"
    if live_signal is True and room_id:
        status = "LIVE_CONFIRMED"
    elif live_signal is False:
        status = "OFFLINE"
    return {
        "status": status,
        "room_id": room_id,
        "username": username,
        "source_timestamp": source_timestamp,
    }


def confirm_live_from_event(
    session: dict[str, Any],
    methods: Iterable[str],
    room_id: str | None,
    *,
    observed_at: str | None = None,
) -> bool:
    """Confirm a room from a decoded room-scoped event on a candidate socket."""
    if not room_id or not LIVE_EVENT_METHODS.intersection(methods):
        return False
    preflight = session.setdefault("live_preflight", {})
    if preflight.get("status") == "LIVE_CONFIRMED":
        return False
    preflight.update({
        "username": session.get("username") or session.get("streamer_username"),
        "status": "LIVE_CONFIRMED",
        "room_id": str(room_id),
        "confirmation_source": "webcast_event",
        "observed_at": observed_at or timestamp_utc(),
    })
    session["room_id"] = str(room_id)
    return True


def decode_cdp_payload(opcode: int, payload_data: str) -> bytes:
    """Convert CDP's payloadData to bytes (binary is base64 encoded by CDP)."""
    if opcode == 2:
        if not isinstance(payload_data, str):
            raise ValueError("binary CDP payloadData must be a string")
        padded = payload_data + ("=" * (-len(payload_data) % 4))
        try:
            return base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 in binary CDP payloadData") from exc
    if opcode == 1:
        if not isinstance(payload_data, str):
            raise ValueError("text CDP payloadData must be a string")
        return payload_data.encode("utf-8")
    raise ValueError(f"unsupported WebSocket data opcode: {opcode}")


def try_gzip_decompress(data: bytes) -> bytes | None:
    """Return decompressed bytes for a gzip member, or None when invalid."""
    if not data.startswith(GZIP_MAGIC):
        return None
    try:
        return gzip.decompress(data)
    except (EOFError, OSError, gzip.BadGzipFile, zlib.error):
        return None


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ProtobufDecodeError("unterminated or oversized varint")


def iter_protobuf_fields(data: bytes) -> Iterable[tuple[int, int, int | bytes]]:
    """Read protobuf wire fields without decoding application messages."""
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire_type = tag >> 3, tag & 0x07
        if number == 0:
            raise ProtobufDecodeError("field number zero is invalid")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed64 field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ProtobufDecodeError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtobufDecodeError("truncated fixed32 field")
            value = data[offset:end]
            offset = end
        else:
            raise ProtobufDecodeError(f"unsupported protobuf wire type {wire_type}")
        yield number, wire_type, value


def _methods_in_bytes(data: bytes) -> list[str]:
    return [match.decode("ascii") for match in METHOD_RE.findall(data)]


def _methods_from_response(data: bytes) -> list[str]:
    """Read WebcastResponse.messages[].method using the SDK schema layout."""
    methods: list[str] = []
    for number, wire_type, nested in iter_protobuf_fields(data):
        if number != 1 or wire_type != 2 or not isinstance(nested, bytes):
            continue
        try:
            message_fields = iter_protobuf_fields(nested)
            for message_field, message_wire, method_bytes in message_fields:
                if message_field != 1 or message_wire != 2 or not isinstance(method_bytes, bytes):
                    continue
                try:
                    method = method_bytes.decode("ascii")
                except UnicodeDecodeError:
                    continue
                if METHOD_RE.fullmatch(method.encode("ascii")):
                    methods.append(method)
        except ProtobufDecodeError:
            continue
    return methods


def _sdk_decode_response(frame: bytes) -> tuple[Any, list[Any]]:
    """Use TikTokLive's PushFrame and gzip-aware response definitions."""
    try:
        from TikTokLive.proto import WebcastPushFrame
        from TikTokLive.client.ws.ws_utils import extract_webcast_response_message
    except (ImportError, ModuleNotFoundError):
        return None, []

    try:
        push = WebcastPushFrame().parse(frame)
        if getattr(push, "payload_type", None) != "msg":
            return push, []
        response = extract_webcast_response_message(push)
        return push, list(getattr(response, "messages", []))
    except Exception:
        # The SDK schema evolves. The wire-only path below is intentionally
        # small and still allows saved frames to reveal the envelope structure.
        return None, []


def _sdk_decode_methods(frame: bytes) -> list[str]:
    _, messages = _sdk_decode_response(frame)
    return [str(message.method) for message in messages if getattr(message, "method", None)]


def _get_field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if value.get(name) is not None:
                return value[name]
    else:
        for name in names:
            item = getattr(value, name, None)
            if item is not None:
                return item
    return default


def _identifier(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _first_identifier(value: Any, *names: str) -> str | None:
    for name in names:
        item = _get_field(value, name)
        if item is None or item == "" or item == 0:
            continue
        return str(item)
    return None


def _level_number(value: Any) -> int | str | None:
    if value is None or value == "":
        return None
    try:
        level = int(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    return level if level > 0 else None


def _normalized_badges(user: Any) -> list[dict[str, Any]]:
    raw_badges = _get_field(user, "badge_list", "badges", "user_badges") or []
    if isinstance(raw_badges, dict):
        raw_badges = list(raw_badges.values())
    if not isinstance(raw_badges, (list, tuple)):
        return []

    badges: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for badge in raw_badges:
        scene_value = _get_field(badge, "scene_type", "badge_scene", "scene")
        scene = getattr(scene_value, "name", None) or str(scene_value or "")
        scene = scene.rsplit(".", 1)[-1]
        for prefix in ("BADGE_SCENE_TYPE_", "SCENE_TYPE_"):
            if scene.startswith(prefix):
                scene = scene[len(prefix):]
        if not scene or scene in {"0", "UNKNOWN"}:
            continue

        extra = _get_field(badge, "privilege_log_extra", "log_extra")
        level = _level_number(_get_field(extra, "level"))
        dedupe_key = (scene, str(level) if level is not None else None)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        item: dict[str, Any] = {"scene": scene}
        if level is not None:
            item["level"] = level
        badges.append(item)
    return badges


def _normalized_user_details(user: Any, *, identity: Any = None) -> dict[str, Any]:
    if user is None:
        return {}
    details: dict[str, Any] = {}
    user_id = _first_identifier(user, "id_str", "id", "user_id")
    unique_id = _first_identifier(user, "unique_id", "display_id", "uniqueId")
    nickname = _get_field(user, "nickname")
    if user_id:
        details["user_id"] = user_id
    if unique_id:
        details["unique_id"] = unique_id
    if nickname:
        details["nickname"] = str(nickname)

    badges = _normalized_badges(user)
    if badges:
        details["badges"] = badges
    fan_level = next(
        (badge["level"] for badge in badges if badge["scene"] == "FANS" and badge.get("level") is not None),
        None,
    )
    if fan_level is None:
        fan_info = _get_field(user, "fans_club_info")
        fan_level = _level_number(_get_field(fan_info, "fans_level", "level"))
    if fan_level is None:
        club = _get_field(user, "fans_club")
        club_data = _get_field(club, "data")
        fan_level = _level_number(_get_field(club_data, "level"))
    if fan_level is not None:
        details["fan_club_level"] = fan_level

    user_grade = next(
        (
            badge["level"]
            for badge in badges
            if badge["scene"] == "USER_GRADE" and badge.get("level") is not None
        ),
        None,
    )
    if user_grade is None:
        user_grade = _level_number(_get_field(_get_field(user, "pay_grade"), "level"))
    if user_grade is not None:
        details["user_grade_level"] = user_grade

    subscriber_flag = _get_field(identity, "is_subscriber_of_anchor")
    if subscriber_flag is not None:
        details["is_subscriber_of_anchor"] = bool(subscriber_flag)
    return details


def _normalized_ranks(proto_event: Any) -> list[dict[str, Any]]:
    contributors = _get_field(proto_event, "ranks", default=[]) or []
    if not isinstance(contributors, (list, tuple)):
        return []
    ranks: list[dict[str, Any]] = []
    for contributor in contributors:
        item: dict[str, Any] = {}
        for field_name in ("rank", "score", "delta"):
            value = _get_field(contributor, field_name)
            if value is not None:
                try:
                    item[field_name] = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
        user = _get_field(contributor, "user")
        details = _normalized_user_details(user)
        if details:
            if details.get("unique_id"):
                details["display_id"] = details["unique_id"]
            item["user"] = details
        if item:
            ranks.append(item)
    return ranks


def _iso_from_ms(value: int | None, tz: timezone) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=tz).isoformat()
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def normalize_proto_event(
    method: str,
    proto_event: Any,
    *,
    received_at_ms: int | None = None,
) -> dict[str, Any] | None:
    """Normalize supported SDK-decoded methods to the Collector NDJSON shape."""
    normalization_started = time.perf_counter()
    if method == "WebcastControlMessage":
        action = _get_field(proto_event, "action")
        action_name = getattr(action, "name", None)
        if action_name is None:
            action_name = str(action).rsplit(".", 1)[-1]
        if action_name != "STREAM_ENDED":
            return None
        event_type = "live_ended"
    else:
        event_type = {
            "WebcastChatMessage": "chat",
            "WebcastGiftMessage": "gift",
            "WebcastRoomUserSeqMessage": "viewer",
            "WebcastMemberMessage": "member",
            "WebcastLikeMessage": "like",
        }.get(method)
    if event_type is None:
        return None

    received = int(received_at_ms if received_at_ms is not None else time.time_ns() // 1_000_000)
    common = _get_field(proto_event, "common")
    created_at = _get_field(common, "create_time")
    try:
        created_at = int(created_at) if created_at is not None and int(created_at) > 0 else None
    except (TypeError, ValueError, OverflowError):
        created_at = None
    message_id = _identifier(_get_field(common, "msg_id"))
    user = _get_field(proto_event, "user")
    identity = _get_field(proto_event, "user_identity")
    user_details = _normalized_user_details(user, identity=identity)
    user_id = user_details.get("user_id")
    if user_id is None and event_type == "member":
        user_id = _first_identifier(proto_event, "user_id")
    unique_id = user_details.get("unique_id")
    nickname = user_details.get("nickname")
    record: dict[str, Any] = {
        "type": event_type,
        "timestamp_ms": created_at,
        "timestamp_utc": _iso_from_ms(created_at, timezone.utc),
        "timestamp_local": _iso_from_ms(created_at, timezone(timedelta(hours=8), "Asia/Taipei")),
        "received_at_ms": received,
        "received_at_utc": _iso_from_ms(received, timezone.utc),
        "received_at_local": _iso_from_ms(received, timezone(timedelta(hours=8), "Asia/Taipei")),
        "msg_id": message_id,
        "room_id": _identifier(_get_field(common, "room_id")),
        "source": "browser_network",
        "source_method": method,
        "user_id": user_id,
        "unique_id": str(unique_id) if unique_id else None,
        "nickname": str(nickname) if nickname else None,
    }
    for field_name in (
        "badges",
        "fan_club_level",
        "user_grade_level",
        "is_subscriber_of_anchor",
    ):
        if field_name in user_details:
            record[field_name] = user_details[field_name]

    if event_type == "live_ended":
        record["action"] = "STREAM_ENDED"
    elif event_type == "chat":
        record["comment"] = _get_field(proto_event, "content", "comment") or None
        record["message_uuid"] = message_id
        record["language"] = _get_field(proto_event, "content_language") or None
    elif event_type == "gift":
        gift = _get_field(proto_event, "gift")
        gift_type = _get_field(gift, "type")
        repeat_count = _get_field(proto_event, "repeat_count") or 1
        repeat_end = bool(_get_field(proto_event, "repeat_end"))
        try:
            repeat_count = max(1, int(repeat_count))
        except (TypeError, ValueError, OverflowError):
            repeat_count = 1
        try:
            gift_type = int(gift_type) if gift_type is not None else None
        except (TypeError, ValueError, OverflowError):
            gift_type = None
        is_streakable = gift_type == 1
        counted = (not is_streakable) or repeat_end
        gift_id = _get_field(proto_event, "gift_id")
        if gift_id is None:
            gift_id = _get_field(gift, "id")
        diamond_count = _get_field(gift, "diamond_count")
        try:
            diamond_count = max(0, int(diamond_count)) if diamond_count is not None else None
        except (TypeError, ValueError, OverflowError):
            diamond_count = None
        order_id = _get_field(proto_event, "order_id")
        raw_gift_name = _get_field(gift, "name", "gift_name")
        gift_name_lookup_started = time.perf_counter()
        gift_metadata = gift_metadata_for(gift_id, raw_gift_name)
        gift_name_lookup_ms = (time.perf_counter() - gift_name_lookup_started) * 1000
        record.update({
            "gift_id": _identifier(gift_id),
            # Keep the WebcastGiftMessage value byte-for-byte at the text level;
            # localized labels are separate metadata and never replace it.
            "gift_name": raw_gift_name,
            "gift_name_original": raw_gift_name,
            "gift_name_en": gift_metadata.get("name_en"),
            "gift_name_zh_display": gift_metadata.get("name_zh_display"),
            "gift_name_zh_tts": gift_metadata.get("name_zh_tts"),
            "gift_catalog_status": gift_metadata.get("mapping_status"),
            "gift_catalog_pending_fields": gift_metadata.get("pending_fields", []),
            "gift_catalog_source": "src/tts/gift_catalog.json",
            "gift_catalog_diamond_count": gift_metadata.get("diamond_count"),
            "gift_type": gift_type,
            "diamond_per_gift": diamond_count,
            "diamond_count": diamond_count,
            "repeat_count": repeat_count,
            "repeat_end": repeat_end,
            "counted": counted,
            "diamond_total": (diamond_count or 0) * repeat_count if counted else 0,
            # Avoid a shared order key on intermediate streak frames: the
            # completed event must remain visible to the existing deduper.
            "transaction_id": _identifier(order_id) if counted else None,
            "group_id": _identifier(_get_field(proto_event, "group_id")),
        })
        record.setdefault("processing_timings_ms", {})[
            "gift_name_resolution_ms"
        ] = round(gift_name_lookup_ms, 3)
    elif event_type == "viewer":
        # The current v3 schema uses field 3 (`total`) for the room's current
        # viewer sample. `total_user` is a separate cumulative room-user count.
        # Older SDK schemas call field 3 `viewer_count`.
        viewer_count = _get_field(proto_event, "viewer_count")
        if viewer_count is None:
            viewer_count = _get_field(proto_event, "total")
        if viewer_count is None:
            viewer_count = _get_field(proto_event, "total_user")
        try:
            viewer_count = int(viewer_count) if viewer_count is not None else None
        except (TypeError, ValueError, OverflowError):
            viewer_count = None
        record["viewer_count"] = viewer_count
        total_user_count = _get_field(proto_event, "total_user")
        try:
            if total_user_count is not None:
                record["total_user_count"] = int(total_user_count)
        except (TypeError, ValueError, OverflowError):
            pass
        ranks = _normalized_ranks(proto_event)
        if ranks:
            record["ranks"] = ranks
    elif event_type == "member":
        record.update({
            "action_code": _get_field(proto_event, "action"),
            "entry_source": _get_field(proto_event, "client_enter_source", "client_enter_type") or None,
            "entry_action": _get_field(proto_event, "enter_type"),
            "entry_type": _get_field(proto_event, "action_description") or None,
        })
        for field_name in ("is_top_user", "is_set_to_admin"):
            value = _get_field(proto_event, field_name)
            if value is not None:
                record[field_name] = bool(value)
        for field_name in ("rank_score", "top_user_no"):
            value = _get_field(proto_event, field_name)
            if value is not None:
                try:
                    record[field_name] = int(value)
                except (TypeError, ValueError, OverflowError):
                    pass
    elif event_type == "like":
        record["like_count"] = _get_field(proto_event, "count") or 0
        record["total_likes"] = _get_field(proto_event, "total")
    record.setdefault("processing_timings_ms", {})[
        "event_normalization_ms"
    ] = round((time.perf_counter() - normalization_started) * 1000, 3)
    return record


def decode_webcast_frame(
    frame: bytes,
    *,
    received_at_ms: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode the SDK PushFrame/response and normalize supported payloads."""
    envelope_started = time.perf_counter()
    push, messages = _sdk_decode_response(frame)
    envelope_ms = (time.perf_counter() - envelope_started) * 1000
    if messages:
        try:
            from TikTokLive.events.proto_events import EVENT_MAPPINGS
        except (ImportError, ModuleNotFoundError):
            EVENT_MAPPINGS = {}
        result = []
        errors = []
        for message in messages:
            method = str(_get_field(message, "method") or "")
            if not METHOD_RE.fullmatch(method.encode("ascii", errors="ignore")):
                continue
            event_class = EVENT_MAPPINGS.get(method)
            normalized = None
            event_payload_started = time.perf_counter()
            if event_class is not None:
                try:
                    proto_event = event_class().parse(
                        bytes(_get_field(message, "payload") or b"")
                    )
                    protobuf_event_ms = (
                        time.perf_counter() - event_payload_started
                    ) * 1000
                    normalized = normalize_proto_event(
                        method, proto_event, received_at_ms=received_at_ms
                    )
                    if normalized is not None:
                        timings = normalized.setdefault("processing_timings_ms", {})
                        timings["protobuf_envelope_ms"] = round(envelope_ms, 3)
                        timings["protobuf_event_ms"] = round(protobuf_event_ms, 3)
                except Exception as exc:
                    errors.append(f"{method}:{type(exc).__name__}")
            result.append({
                "method": method,
                "msg_id": _identifier(_get_field(message, "msg_id")),
                "normalized": normalized,
            })
        return result, errors

    methods, errors = inspect_event_frame(frame)
    fallback = [{"method": method, "msg_id": None, "normalized": None} for method in methods]
    return fallback, errors


def inspect_event_frame(data: bytes) -> tuple[list[str], list[str]]:
    """Extract event methods and lightweight decoder diagnostics from a frame."""
    direct = _methods_in_bytes(data)
    if direct:
        return direct, []

    sdk_methods = _sdk_decode_methods(data)
    if sdk_methods:
        return sdk_methods, []

    errors: list[str] = []
    candidates: list[bytes] = []
    if data.startswith(GZIP_MAGIC):
        decoded = try_gzip_decompress(data)
        if decoded is None:
            errors.append("gzip")
        else:
            candidates.append(decoded)

    try:
        # TikTokLive's WebcastPushFrame definition names field 8 `payload`.
        for number, wire_type, value in iter_protobuf_fields(data):
            if number == 8 and wire_type == 2 and isinstance(value, bytes):
                candidates.append(value)
    except ProtobufDecodeError:
        errors.append("protobuf")

    for candidate in candidates:
        if candidate.startswith(GZIP_MAGIC):
            decoded = try_gzip_decompress(candidate)
            if decoded is None:
                errors.append("gzip")
                continue
            candidate = decoded
        visible = _methods_in_bytes(candidate)
        if visible:
            return visible, errors
        try:
            methods = _methods_from_response(candidate)
        except ProtobufDecodeError:
            errors.append("protobuf")
            continue
        if methods:
            return methods, errors

    return [], errors


class FrameRecorder:
    """Decode all observed frames and optionally save a bounded raw sample."""

    def __init__(
        self,
        session_dir: Path,
        max_frames: int,
        save_raw_frames: bool = False,
        session_id: str | None = None,
        event_bus: NormalizedEventBus | None = None,
    ) -> None:
        self.session_dir = session_dir
        self.session_id = session_id or session_dir.name
        self.frames_dir = session_dir / "frames"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.save_raw_frames = save_raw_frames
        if self.save_raw_frames:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.max_frames = max_frames
        self.event_bus = event_bus
        self.last_frame_methods: tuple[str, ...] = ()
        self.last_frame_live_room_id: str | None = None
        self.frame_count = 0
        self.binary_frame_count = 0
        self.saved_raw_frame_count = 0
        self.event_count = 0
        self.method_counts: Counter[str] = Counter()
        self.category_counts: Counter[str] = Counter()
        self.event_counts: Counter[str] = Counter()
        self.decoded_field_counts: Counter[str] = Counter()
        self.live_end_event: dict[str, Any] | None = None
        self.ranking_snapshot_count = 0
        self.decoding_errors: Counter[str] = Counter()
        self.duplicate_event_counts: Counter[str] = Counter()
        self.seen_event_keys: set[str] = set()
        self.seen_event_order: deque[str] = deque()
        self.dedupe_limit = 250_000
        self.frames_file = (
            (session_dir / "frames.ndjson").open(
                "a", encoding="utf-8", buffering=1
            )
            if self.save_raw_frames
            else None
        )
        self.methods_file = (session_dir / "methods.ndjson").open(
            "a", encoding="utf-8", buffering=1
        )
        self.sockets_file = (session_dir / "sockets.ndjson").open(
            "a", encoding="utf-8", buffering=1
        )
        self.events_file = (session_dir / "events.ndjson").open(
            "a", encoding="utf-8", buffering=1
        )

    def record_socket(
        self,
        *,
        request_id: str,
        url: str,
        state: str,
        candidate: bool,
        live_candidate: bool = False,
        status: int | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        row = {
            "timestamp": timestamp_utc(),
            "request_id": request_id,
            "hostname": websocket_hostname(url),
            "url": sanitize_url(url),
            "state": state,
            "candidate": candidate,
            "live_candidate": live_candidate,
            "status": status,
            "duration_seconds": duration_seconds,
        }
        self.sockets_file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record(
        self,
        request_id: str,
        url: str,
        opcode: int,
        payload_data: str,
        connection_id: int = 1,
    ) -> bool:
        """Decode every frame and optionally retain a bounded raw-frame sample."""
        self.frame_count += 1
        seq = self.frame_count
        timestamp = timestamp_utc()
        sanitized_url = sanitize_url(url)
        raw: bytes | None
        try:
            raw = decode_cdp_payload(opcode, payload_data)
        except ValueError:
            raw = None
            self.decoding_errors["cdp_payload"] += 1

        relative_file: str | None = None
        size: int | None = len(raw) if raw is not None else None
        if raw is not None and opcode == 2:
            self.binary_frame_count += 1
        save_frame = (
            self.save_raw_frames
            and self.frames_file is not None
            and seq <= self.max_frames
        )
        if save_frame and raw is not None and opcode == 2:
            filename = f"frame_{seq:06d}.bin"
            (self.frames_dir / filename).write_bytes(raw)
            relative_file = f"frames/{filename}"
            self.saved_raw_frame_count += 1

        messages: list[dict[str, Any]] = []
        if raw is not None:
            if opcode == 2:
                messages, errors = decode_webcast_frame(raw)
            else:
                messages = [
                    {"method": method, "msg_id": None, "normalized": None}
                    for method in _methods_in_bytes(raw)
                ]
                errors = []
            for error in errors:
                self.decoding_errors[error] += 1
        methods = [str(message.get("method")) for message in messages if message.get("method")]
        self.last_frame_methods = tuple(methods)
        self.last_frame_live_room_id = None

        if save_frame and self.frames_file is not None:
            frame_row = {
                "seq": seq,
                "timestamp": timestamp,
                "request_id": request_id,
                "hostname": websocket_hostname(url),
                "opcode": opcode,
                "payload_size": size,
                "url": sanitized_url,
                "file": relative_file,
            }
            self.frames_file.write(json.dumps(frame_row, ensure_ascii=False) + "\n")

        if methods:
            method_row = {
                "seq": seq,
                "timestamp": timestamp,
                "request_id": request_id,
                "hostname": websocket_hostname(url),
                "url": sanitized_url,
                "methods": methods,
            }
            self.methods_file.write(json.dumps(method_row, ensure_ascii=False) + "\n")
            print(
                f"[FRAME {seq:06d}] bytes={size} methods={','.join(methods)}",
                flush=True,
            )
            for method in methods:
                self.method_counts[method] += 1
                label = METHOD_LABELS.get(method)
                if label is None:
                    self.category_counts["Other"] += 1
                    print(f"[EVENT] {method}", flush=True)
                else:
                    self.category_counts[label] += 1
                    print(f"[{label}] {method}", flush=True)

        for message in messages:
            normalized = message.get("normalized")
            if not isinstance(normalized, dict):
                continue
            if message.get("method") in LIVE_EVENT_METHODS:
                self.last_frame_live_room_id = (
                    self.last_frame_live_room_id
                    or _identifier(normalized.get("room_id"))
                )
            duplicate_key = self._event_dedupe_key(normalized)
            if duplicate_key is not None:
                if duplicate_key in self.seen_event_keys:
                    self.duplicate_event_counts[str(normalized.get("type") or "unknown")] += 1
                    continue
                self.seen_event_keys.add(duplicate_key)
                self.seen_event_order.append(duplicate_key)
                if len(self.seen_event_order) > self.dedupe_limit:
                    self.seen_event_keys.discard(self.seen_event_order.popleft())
            self.event_count += 1
            normalized.update({
                "schema_version": "0.3",
                "session_id": self.session_id,
                "connection_id": connection_id,
                "seq": self.event_count,
                "frame_seq": seq,
                "source": "browser_network",
            })
            self.events_file.write(
                json.dumps(normalized, ensure_ascii=False) + "\n"
            )
            if self.event_bus is not None:
                try:
                    self.event_bus.publish(normalized)
                except Exception:
                    # Optional consumers must never interrupt durable capture.
                    self.decoding_errors["event_bus"] += 1
            event_type = str(normalized.get("type") or "unknown")
            self.event_counts[event_type] += 1
            if event_type == "live_ended":
                if self.live_end_event is None:
                    self.live_end_event = dict(normalized)
                    print(
                        "[LIVE ENDED] "
                        f"room_id={normalized.get('room_id') or 'unknown'} "
                        f"received_at={normalized.get('received_at_utc') or 'unknown'}",
                        flush=True,
                    )
            elif event_type == "viewer" and normalized.get("ranks"):
                self.ranking_snapshot_count += 1
            elif event_type == "chat" and normalized.get("comment"):
                self.decoded_field_counts["chat"] += 1
            elif event_type == "gift":
                if (
                    normalized.get("gift_name")
                    and normalized.get("repeat_count") is not None
                    and (normalized.get("unique_id") or normalized.get("user_id"))
                ):
                    self.decoded_field_counts["gift"] += 1
            elif event_type in {"viewer", "member", "like"}:
                self.decoded_field_counts[event_type] += 1

        return self.save_raw_frames and self.frame_count >= self.max_frames

    @staticmethod
    def _event_dedupe_key(event: dict[str, Any]) -> str | None:
        event_type = str(event.get("type") or "unknown")
        if event_type in {"system", "room", "control"}:
            return None
        if event_type == "live_ended":
            room_id = event.get("room_id")
            return f"live_ended:room:{room_id}" if room_id else "live_ended"
        if event_type == "gift" and event.get("transaction_id"):
            return f"gift:tx:{event['transaction_id']}"
        if event_type == "chat" and event.get("message_uuid"):
            return f"chat:uuid:{event['message_uuid']}"
        message_id = event.get("msg_id")
        return f"{event_type}:msg:{message_id}" if message_id else None

    def close(self) -> None:
        if self.frames_file is not None:
            self.frames_file.close()
        self.methods_file.close()
        self.sockets_file.close()
        self.events_file.close()


def _session_id(username: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return f"{stamp}_{username or 'unknown'}"


def _resolve_user_and_url(username: str | None, url: str | None) -> tuple[str, str]:
    clean_username = (username or "").strip().lstrip("@")
    if clean_username and not USERNAME_RE.fullmatch(clean_username):
        raise ValueError("username may contain only letters, digits, dot, underscore, and dash")
    if url:
        return clean_username or "unknown", url
    if not clean_username:
        raise ValueError("provide --username or --url")
    return clean_username, f"https://www.tiktok.com/@{clean_username}/live"


async def _verification_visible(page: Any) -> bool:
    try:
        visible_text = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return False
    return bool(VERIFICATION_RE.search(visible_text[:12000]))


async def _login_required_visible(page: Any) -> bool:
    try:
        visible_text = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return False
    return bool(LOGIN_REQUIRED_RE.search(visible_text[:12000]))


def _live_endpoint_path(url: str) -> str | None:
    try:
        path = urlsplit(url).path.casefold()
    except ValueError:
        return None
    return next((candidate for candidate in LIVE_ENDPOINT_PATHS if candidate in path), None)


def _page_is_tiktok_live(page: Any, username: str) -> bool:
    try:
        parsed = urlsplit(str(page.url))
    except (AttributeError, ValueError):
        return False
    host = (parsed.hostname or "").casefold()
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        return False
    match = re.search(r"/@([^/]+)/live(?:/|$)", parsed.path, re.IGNORECASE)
    if not match:
        return False
    return username == "unknown" or match.group(1).casefold() == username.casefold()


def _local_cdp_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").casefold() in {
        "127.0.0.1", "localhost", "::1"
    }


async def clear_browser_http_cache(cdp_session: Any) -> tuple[bool, str | None]:
    """Clear Chromium's disposable HTTP cache without touching cookies or site data."""
    try:
        await cdp_session.send("Network.clearBrowserCache")
    except Exception as exc:
        return False, type(exc).__name__
    return True, None


def _uses_everyday_chrome_profile(profile_dir: str | Path) -> bool:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return False
    everyday_profile = (Path(local_app_data) / "Google" / "Chrome" / "User Data").resolve()
    candidate = Path(profile_dir).expanduser().resolve()
    try:
        return os.path.commonpath((str(everyday_profile), str(candidate))).casefold() == str(
            everyday_profile
        ).casefold()
    except ValueError:
        return False


def _feasibility_report(session: dict[str, Any], recorder: FrameRecorder) -> dict[str, Any]:
    preflight = session["live_preflight"]["status"]
    socket_open = session.get("target_live_websocket_open_count", 0) > 0
    binary_frame_count = session.get("target_binary_frame_count", 0)
    method_counts = session.get("target_method_counts", {})
    decoded_field_counts = session.get("target_decoded_field_counts", {})
    methods_seen = bool(method_counts)
    chat_seen = decoded_field_counts.get("chat", 0) > 0
    gift_seen = decoded_field_counts.get("gift", 0) > 0
    viewer_member_seen = bool(
        decoded_field_counts.get("viewer", 0) or decoded_field_counts.get("member", 0)
    )
    tts_passed = session.get("tts_integration_status") == "PASS"
    duration_seconds = float(session.get("duration_seconds") or 0)
    active_seconds = float(session.get("target_live_websocket_active_seconds") or 0)
    coverage_ratio = min(1.0, active_seconds / duration_seconds) if duration_seconds > 0 else 0.0
    stable_transport = coverage_ratio >= 0.95
    live_transport = (
        preflight == "LIVE_CONFIRMED" and socket_open and binary_frame_count > 0
    )
    stage_a = live_transport and stable_transport and duration_seconds >= 600
    stage_b = live_transport and stable_transport and duration_seconds >= 1800
    stage_c = live_transport and stable_transport and duration_seconds >= 7200
    stage_d = live_transport and stable_transport and duration_seconds >= 14400
    level = 1 if session["page_loaded"] else 0
    if preflight == "LIVE_CONFIRMED" and socket_open:
        level = max(level, 2)
    if level >= 2 and binary_frame_count:
        level = 3
    if level >= 3 and methods_seen:
        level = 4
    if level >= 4 and chat_seen:
        level = 5
    if level >= 5 and gift_seen:
        level = 6
    if level >= 6 and tts_passed:
        level = 7
    if level >= 7 and viewer_member_seen:
        level = 8
    if level >= 8 and stage_c:
        level = 9
    if level >= 9 and stage_d:
        level = 10
    gate_a = "NOT_TESTED" if preflight != "LIVE_CONFIRMED" else (
        "PASS" if socket_open and binary_frame_count else "INCOMPLETE"
    )
    return {
        "highest_level": level,
        "level_1_live_page_loaded": bool(session["page_loaded"]),
        "level_2_live_and_socket": preflight == "LIVE_CONFIRMED" and socket_open,
        "level_3_binary_frames": binary_frame_count > 0,
        "level_4_methods": methods_seen,
        "level_5_chat": chat_seen,
        "level_6_gift": gift_seen,
        "level_7_tts_integration": tts_passed,
        "level_8_viewer_or_member": level >= 8,
        "viewer_member_metadata_decoded": viewer_member_seen,
        "level_9_two_hour_stability": level >= 9,
        "level_10_four_hour_stability": level >= 10,
        "decision_gate_a": gate_a,
        "decision_gate_b": "PASS" if methods_seen else "NOT_TESTED",
        "decision_gate_c": "PASS" if chat_seen else "NOT_TESTED",
        "decision_gate_d": "PASS" if gift_seen else "NOT_TESTED",
        "tts_integration": "PASS" if tts_passed else "NOT_RUNTIME_TESTED",
        "stage_10_15_min_stability": "PASS" if stage_a else "NOT_RUN",
        "stage_30_min_stability": "PASS" if stage_b else "NOT_RUN",
        "stage_2h_stability": "PASS" if stage_c else "NOT_RUN",
        "stage_4h_stability": "PASS" if stage_d else "NOT_RUN",
        "target_transport_coverage_ratio": round(coverage_ratio, 4),
        "target_live_websocket_close_count": session.get("target_live_websocket_close_count", 0),
    }


async def run_probe(args: argparse.Namespace) -> Path:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install the existing project dependency "
            "with `python -m pip install playwright` and run `python -m playwright install chromium`."
        ) from exc

    username, live_url = _resolve_user_and_url(args.username, args.url)
    if args.browser_mode == "cdp" and not _local_cdp_endpoint(args.cdp_url):
        raise ValueError("--cdp-url must use a loopback host such as http://127.0.0.1:9222")
    session_dir = Path(args.output_dir) / _session_id(username)
    session_dir.mkdir(parents=True, exist_ok=False)
    profile_dir = Path(args.profile_dir)
    if args.browser_mode == "launch":
        if _uses_everyday_chrome_profile(args.profile_dir):
            raise ValueError(
                "--profile-dir must be isolated from Chrome's everyday User Data directory"
            )
        profile_dir.mkdir(parents=True, exist_ok=True)
    recorder = FrameRecorder(
        session_dir,
        args.max_frames,
        save_raw_frames=getattr(args, "save_raw_frames", False),
        event_bus=getattr(args, "event_bus", None),
    )
    started_at = timestamp_utc()
    started_monotonic = time.monotonic()
    session: dict[str, Any] = {
        "session_id": session_dir.name,
        "streamer_username": username,
        "username": username,
        "provider": "browser_network",
        "live_url": sanitize_url(live_url),
        "browser_mode": args.browser_mode,
        "browser_cache_clear": {
            "status": "NOT_REQUESTED",
            "error": None,
        },
        "room_id": None,
        "started_at": started_at,
        "collector_started_at_utc": started_at,
        "status": "running",
        "ended_at": None,
        "collector_ended_at_utc": None,
        "chromium_version": None,
        "target_websocket_url": None,
        "target_websocket_urls": [],
        "target_websocket_connection_count": 0,
        "target_live_websocket_open_count": 0,
        "target_live_websocket_close_count": 0,
        "target_live_websocket_active_seconds": 0.0,
        "target_binary_frame_count": 0,
        "target_method_counts": {},
        "target_decoded_field_counts": {},
        "ranking_snapshot_count": 0,
        "live_end_detection": {
            "status": "NOT_OBSERVED",
            "event_count": 0,
            "room_id": None,
            "timestamp_utc": None,
            "received_at_utc": None,
            "msg_id": None,
        },
        "all_websocket_connection_count": 0,
        "candidate_websocket_open_count": 0,
        "discovered_websocket_hostnames": [],
        "observed_live_endpoints": [],
        "live_preflight": {
            "username": username,
            "status": "UNKNOWN",
            "room_id": None,
            "source_timestamp": None,
            "confirmation_source": None,
            "endpoint": None,
            "http_status": None,
            "observed_at": None,
        },
        "page_loaded": False,
        "captured_frame_count": 0,
        "captured_binary_frame_count": 0,
        "saved_raw_frame_count": 0,
        "raw_frame_storage_enabled": recorder.save_raw_frames,
        "detected_method_counts": {},
        "normalized_event_counts": {},
        "decoded_field_counts": {},
        "event_category_counts": {},
        "decoding_errors": {},
        "cdp_frame_errors": 0,
        "page_navigation_status": None,
        "page_navigation_error": None,
    }
    session_path = session_dir / "session.json"

    def persist_session_snapshot() -> None:
        session["captured_frame_count"] = recorder.frame_count
        session["captured_binary_frame_count"] = recorder.binary_frame_count
        session["saved_raw_frame_count"] = recorder.saved_raw_frame_count
        session["target_binary_frame_count"] = recorder.binary_frame_count
        session["detected_method_counts"] = dict(sorted(recorder.method_counts.items()))
        session["target_method_counts"] = dict(sorted(recorder.method_counts.items()))
        session["normalized_event_counts"] = dict(sorted(recorder.event_counts.items()))
        session["target_decoded_field_counts"] = dict(sorted(recorder.decoded_field_counts.items()))
        session["decoded_field_counts"] = dict(sorted(recorder.decoded_field_counts.items()))
        session["ranking_snapshot_count"] = recorder.ranking_snapshot_count
        session["decoding_errors"] = dict(sorted(recorder.decoding_errors.items()))
        session["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
        temporary = session_path.with_name("session.json.tmp")
        try:
            temporary.write_text(
                json.dumps(session, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, session_path)
        except OSError:
            session["session_metadata_write_errors"] = int(
                session.get("session_metadata_write_errors", 0)
            ) + 1

    persist_session_snapshot()
    browser = None
    context = None
    cdp = None
    sockets: dict[str, dict[str, Any]] = {}
    live_http_requests: dict[str, dict[str, Any]] = {}
    stop_event = asyncio.Event()
    verification_was_visible = False
    login_notice_printed = False
    cdp_was_connected = False

    if args.browser_mode == "launch":
        print(
            "[MANUAL CHECK] If TikTok shows login, consent, CAPTCHA, or human verification, "
            "complete it in the open Chromium window. This probe will not automate verification.",
            flush=True,
        )

    try:
        async with async_playwright() as playwright:
            if args.browser_mode == "cdp":
                try:
                    browser = await playwright.chromium.connect_over_cdp(args.cdp_url)
                except Exception as exc:
                    raise RuntimeError(
                        "Could not attach to Chrome. Start the dedicated debug Chrome first; "
                        f"CDP error: {type(exc).__name__}"
                    ) from exc
                cdp_was_connected = True
                contexts = list(browser.contexts)
                page_count = sum(len(item.pages) for item in contexts)
                session["chromium_version"] = browser.version
                print(
                    "[CDP ATTACHED]\n"
                    f"Chrome version={browser.version}\n"
                    f"contexts={len(contexts)}\n"
                    f"pages={page_count}",
                    flush=True,
                )
            else:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=args.headless,
                )
                browser = context.browser
                if browser is not None:
                    session["chromium_version"] = browser.version
                contexts = [context]

            def all_pages() -> list[tuple[Any, Any]]:
                return [(item, page) for item in contexts for page in item.pages]

            if args.browser_mode == "cdp":
                global_page_index = 0
                for context_index, browser_context in enumerate(contexts):
                    for context_page_index, browser_page in enumerate(browser_context.pages):
                        print(
                            f"[CDP PAGE] index={global_page_index} context={context_index} "
                            f"context_page={context_page_index} "
                            f"url={sanitize_url(str(browser_page.url))}",
                            flush=True,
                        )
                        global_page_index += 1

            page = None
            if args.browser_mode == "cdp":
                if args.page_index is not None:
                    pages = all_pages()
                    if args.page_index < 0:
                        raise ValueError("--page-index must be zero or greater")
                    if args.page_index >= len(pages):
                        raise ValueError(
                            f"--page-index {args.page_index} is outside the {len(pages)} open pages"
                        )
                    context, page = pages[args.page_index]
                else:
                    selected = next(
                        (
                            (item, candidate)
                            for item, candidate in all_pages()
                            if _page_is_tiktok_live(candidate, username)
                        ),
                        None,
                    )
                    if selected:
                        context, page = selected
                    elif all_pages():
                        # Attach before navigation so CDP observes the LIVE
                        # socket creation and its first incoming frames.
                        context, page = all_pages()[0]
                    elif contexts:
                        context = contexts[0]
                        page = await context.new_page()
                if page is None:
                    raise RuntimeError("attached Chrome has no browser context to observe")
            else:
                context = contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

            cdp = await context.new_cdp_session(page)

            print(
                f"[PAGE SELECTED] live_url={sanitize_url(str(page.url) if args.browser_mode == 'cdp' else live_url)}",
                flush=True,
            )
            if args.browser_mode == "cdp" and not _page_is_tiktok_live(page, username):
                print(
                    "[WAITING] Open TikTok LIVE manually in Chrome in this selected tab; "
                    "CDP observation is already enabled for this tab.",
                    flush=True,
                )
            session["live_preflight"]["status"] = "UNKNOWN"
            print(
                "[LIVE PREFLIGHT]\n"
                f"username={username}\n"
                "status=UNKNOWN\n"
                "room_id=",
                flush=True,
            )

            def on_websocket_created(event: dict[str, Any]) -> None:
                request_id = str(event.get("requestId", ""))
                url = str(event.get("url", ""))
                if not request_id:
                    return
                safe_url = sanitize_url(url)
                hostname = websocket_hostname(url)
                candidate = is_candidate_websocket(url)
                live_candidate = is_live_candidate_websocket(url)
                sockets[request_id] = {
                    "url": safe_url,
                    "hostname": hostname,
                    "candidate": candidate,
                    "live_candidate": live_candidate,
                    "created_monotonic": time.monotonic(),
                    "opened_monotonic": None,
                }
                session["all_websocket_connection_count"] = session.get("all_websocket_connection_count", 0) + 1
                hostnames = session["discovered_websocket_hostnames"]
                if hostname and hostname not in hostnames:
                    hostnames.append(hostname)
                    print(f"[WS DISCOVERED] host={hostname}", flush=True)
                recorder.record_socket(
                    request_id=request_id,
                    url=url,
                    state="created",
                    candidate=candidate,
                    live_candidate=live_candidate,
                )
                if live_candidate:
                    session["target_websocket_connection_count"] += 1
                    sockets[request_id]["connection_id"] = session["target_websocket_connection_count"]
                    urls = session["target_websocket_urls"]
                    if safe_url not in urls:
                        urls.append(safe_url)
                    session["target_websocket_url"] = safe_url
                    print(f"[WS LIVE CANDIDATE] host={hostname} url={safe_url}", flush=True)
                elif candidate:
                    print(f"[WS CANDIDATE] host={hostname} url={safe_url}", flush=True)

            def on_handshake(event: dict[str, Any]) -> None:
                request_id = str(event.get("requestId", ""))
                socket = sockets.get(request_id)
                if socket is None:
                    return
                response = event.get("response") or {}
                status = response.get("status")
                recorder.record_socket(
                    request_id=request_id,
                    url=socket["url"],
                    state="handshake",
                    candidate=socket["candidate"],
                    live_candidate=socket["live_candidate"],
                    status=status if isinstance(status, int) else None,
                )
                if status == 101 and socket["candidate"] and socket["opened_monotonic"] is None:
                    socket["opened_monotonic"] = time.monotonic()
                    session["candidate_websocket_open_count"] += 1
                    print(
                        "[WS OPEN]\n"
                        f"request_id={request_id}\n"
                        f"url={socket['url']}",
                        flush=True,
                    )
                    if socket["live_candidate"]:
                        session["target_live_websocket_open_count"] += 1

            def on_websocket_closed(event: dict[str, Any]) -> None:
                request_id = str(event.get("requestId", ""))
                socket = sockets.get(request_id)
                if socket is None:
                    return
                opened = socket["opened_monotonic"] or socket["created_monotonic"]
                duration = max(0.0, time.monotonic() - opened)
                socket["closed_monotonic"] = time.monotonic()
                recorder.record_socket(
                    request_id=request_id,
                    url=socket["url"],
                    state="closed",
                    candidate=socket["candidate"],
                    live_candidate=socket["live_candidate"],
                    duration_seconds=round(duration, 3),
                )
                if socket["live_candidate"] and socket["opened_monotonic"] is not None:
                    session["target_live_websocket_close_count"] += 1
                    session["target_live_websocket_active_seconds"] += duration
                if socket["candidate"]:
                    print(
                        "[WS CLOSED]\n"
                        f"request_id={request_id}\n"
                        f"duration={duration:.1f}s",
                        flush=True,
                    )

            def on_frame_received(event: dict[str, Any]) -> None:
                request_id = str(event.get("requestId", ""))
                socket = sockets.get(request_id)
                if socket is None or not socket["candidate"]:
                    return
                response = event.get("response") or {}
                opcode = int(response.get("opcode", -1))
                payload_data = response.get("payloadData", "")
                if not isinstance(payload_data, str):
                    payload_data = ""
                previous_field_counts = dict(recorder.decoded_field_counts)
                raw_limit_reached = recorder.record(
                    request_id,
                    socket["url"],
                    opcode,
                    payload_data,
                    connection_id=int(socket.get("connection_id", 0)),
                )
                if raw_limit_reached and not session.get("raw_frame_limit_reported"):
                    session["raw_frame_limit_reported"] = True
                    print(
                        f"[RAW FRAME LIMIT] sample limit reached at "
                        f"{args.max_frames} observed frames; "
                        "event capture continues",
                        flush=True,
                    )
                frame_methods = recorder.last_frame_methods
                # If TikTok moves LIVE traffic to a new hostname, classify the
                # connection from decoded Webcast evidence rather than its name.
                if not socket["live_candidate"] and LIVE_EVENT_METHODS.intersection(frame_methods):
                    socket["live_candidate"] = True
                    session["target_websocket_connection_count"] += 1
                    socket["connection_id"] = session["target_websocket_connection_count"]
                    safe_url = socket["url"]
                    if safe_url not in session["target_websocket_urls"]:
                        session["target_websocket_urls"].append(safe_url)
                    session["target_websocket_url"] = safe_url
                    if socket["opened_monotonic"] is not None:
                        session["target_live_websocket_open_count"] += 1
                    recorder.record_socket(
                        request_id=request_id,
                        url=safe_url,
                        state="classified_live",
                        candidate=socket["candidate"],
                        live_candidate=True,
                    )
                    print(
                        f"[WS LIVE CANDIDATE] host={socket['hostname']} "
                        f"classified_by=webcast_method url={safe_url}",
                        flush=True,
                    )
                if socket["live_candidate"]:
                    if opcode == 2:
                        try:
                            decode_cdp_payload(opcode, payload_data)
                        except ValueError:
                            pass
                        else:
                            session["target_binary_frame_count"] += 1
                    target_methods = session["target_method_counts"]
                    for method in frame_methods:
                        target_methods[method] = target_methods.get(method, 0) + 1
                    field_counts = session["target_decoded_field_counts"]
                    for field_name, count in recorder.decoded_field_counts.items():
                        delta = count - previous_field_counts.get(field_name, 0)
                        if delta > 0:
                            field_counts[field_name] = field_counts.get(field_name, 0) + delta
                if confirm_live_from_event(
                    session,
                    recorder.last_frame_methods,
                    recorder.last_frame_live_room_id,
                ):
                    preflight = session["live_preflight"]
                    print(
                        "[LIVE PREFLIGHT]\n"
                        f"username={preflight['username']}\n"
                        "status=LIVE_CONFIRMED\n"
                        f"room_id={preflight['room_id']}\n"
                        "source=webcast_event",
                        flush=True,
                    )

            def on_frame_error(event: dict[str, Any]) -> None:
                request_id = str(event.get("requestId", ""))
                socket = sockets.get(request_id)
                if socket is not None:
                    session["cdp_frame_errors"] += 1
                    recorder.record_socket(
                        request_id=request_id,
                        url=socket["url"],
                        state="frame_error",
                        candidate=socket["candidate"],
                        live_candidate=socket["live_candidate"],
                    )
                    if socket["candidate"]:
                        print(f"[WS FRAME ERROR] request_id={request_id}", flush=True)

            def on_request_will_be_sent(event: dict[str, Any]) -> None:
                request = event.get("request") or {}
                endpoint = _live_endpoint_path(str(request.get("url", "")))
                if endpoint and endpoint not in session["observed_live_endpoints"]:
                    session["observed_live_endpoints"].append(endpoint)
                if endpoint:
                    live_http_requests[str(event.get("requestId", ""))] = {
                        "endpoint": endpoint,
                        "http_status": None,
                    }

            def on_response_received(event: dict[str, Any]) -> None:
                response = event.get("response") or {}
                endpoint = _live_endpoint_path(str(response.get("url", "")))
                request_id = str(event.get("requestId", ""))
                if endpoint and request_id:
                    if endpoint not in session["observed_live_endpoints"]:
                        session["observed_live_endpoints"].append(endpoint)
                    metadata = live_http_requests.setdefault(request_id, {})
                    metadata.update({
                        "endpoint": endpoint,
                        "http_status": response.get("status"),
                    })

            def on_loading_finished(event: dict[str, Any]) -> None:
                request_id = str(event.get("requestId", ""))
                metadata = live_http_requests.pop(request_id, None)
                if metadata:
                    asyncio.create_task(read_live_endpoint(request_id, metadata))

            async def read_live_endpoint(request_id: str, metadata: dict[str, Any]) -> None:
                endpoint = str(metadata.get("endpoint", ""))
                if not endpoint:
                    return
                if endpoint not in session["observed_live_endpoints"]:
                    session["observed_live_endpoints"].append(endpoint)
                http_status = metadata.get("http_status")
                result: dict[str, Any] = {
                    "status": "UNKNOWN",
                    "room_id": None,
                    "username": None,
                    "source_timestamp": None,
                }
                if request_id:
                    try:
                        body_result = await cdp.send(
                            "Network.getResponseBody", {"requestId": request_id}
                        )
                        body = body_result.get("body", "")
                        if body_result.get("base64Encoded"):
                            body = base64.b64decode(body).decode("utf-8", errors="replace")
                        decoded = json.loads(body)
                        result = parse_live_preflight(decoded)
                    except Exception:
                        session["preflight_decode_errors"] = session.get("preflight_decode_errors", 0) + 1
                if result["status"] != "UNKNOWN":
                    preflight = session["live_preflight"]
                    preflight.update({
                        "username": result["username"] or username,
                        "status": result["status"],
                        "room_id": result["room_id"],
                        "source_timestamp": result["source_timestamp"],
                        "confirmation_source": "http_endpoint",
                        "endpoint": endpoint,
                        "http_status": http_status,
                        "observed_at": timestamp_utc(),
                    })
                    session["room_id"] = result["room_id"]
                    print(
                        "[LIVE PREFLIGHT]\n"
                        f"username={preflight['username']}\n"
                        f"status={preflight['status']}\n"
                        f"room_id={preflight['room_id'] or ''}\n"
                        f"timestamp={preflight['source_timestamp'] or ''}\n"
                        f"endpoint={endpoint}",
                        flush=True,
                    )
                elif session["live_preflight"].get("endpoint") is None:
                    session["live_preflight"].update({
                        "endpoint": endpoint,
                        "http_status": http_status,
                        "observed_at": timestamp_utc(),
                    })

            cdp.on("Network.webSocketCreated", on_websocket_created)
            cdp.on("Network.webSocketHandshakeResponseReceived", on_handshake)
            cdp.on("Network.webSocketClosed", on_websocket_closed)
            cdp.on("Network.webSocketFrameReceived", on_frame_received)
            cdp.on("Network.webSocketFrameError", on_frame_error)
            cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
            cdp.on("Network.responseReceived", on_response_received)
            cdp.on("Network.loadingFinished", on_loading_finished)
            await cdp.send("Network.enable")

            if getattr(args, "clear_browser_cache_on_start", False):
                cleared, error = await clear_browser_http_cache(cdp)
                session["browser_cache_clear"] = {
                    "status": "CLEARED" if cleared else "FAILED",
                    "error": error,
                }
                if cleared:
                    print(
                        "[CACHE CLEARED] Chromium HTTP cache only; cookies and site data were preserved.",
                        flush=True,
                    )
                else:
                    print(
                        f"[CACHE CLEAR ERROR] {error}; continuing capture.",
                        flush=True,
                    )

            if args.browser_mode == "cdp" and args.reload_live_page:
                if not _page_is_tiktok_live(page, username):
                    raise ValueError(
                        "--reload-live-page requires the selected Chrome tab to already "
                        "show the requested TikTok LIVE page"
                    )
                try:
                    response = await page.reload(wait_until="domcontentloaded")
                except Exception as exc:
                    session["page_navigation_error"] = type(exc).__name__
                    print(
                        f"[PAGE RELOAD ERROR] {type(exc).__name__} "
                        f"live_url={sanitize_url(str(page.url))}",
                        flush=True,
                    )
                else:
                    session["page_navigation_status"] = response.status if response else None
                    session["page_loaded"] = bool(
                        response is None or 200 <= response.status < 400
                    )
                    print(
                        f"[PAGE RELOADED] status={response.status if response else 'unknown'} "
                        f"live_url={sanitize_url(str(page.url))}",
                        flush=True,
                    )

            if args.browser_mode == "launch":
                try:
                    response = await page.goto(live_url, wait_until="domcontentloaded")
                except Exception as exc:
                    session["page_navigation_error"] = type(exc).__name__
                    print(
                        f"[PAGE NAVIGATION ERROR] {type(exc).__name__} "
                        f"live_url={sanitize_url(live_url)}",
                        flush=True,
                    )
                    return session_dir
                session["page_navigation_status"] = response.status if response else None
                session["page_loaded"] = bool(response is None or 200 <= response.status < 400)
                print(
                    f"[PAGE NAVIGATED] status={response.status if response else 'unknown'} "
                    f"live_url={sanitize_url(live_url)}",
                    flush=True,
                )
            else:
                session["page_loaded"] = False
                print("[CAPTURE] attached to existing Chrome page; Chrome remains open on exit", flush=True)
            raw_capture = (
                f"enabled (limit={args.max_frames})"
                if recorder.save_raw_frames
                else "disabled"
            )
            print(
                f"[CAPTURE] session={session_dir} raw_frames={raw_capture}; "
                "event decoding continues until Ctrl+C",
                flush=True,
            )

            last_snapshot_monotonic = time.monotonic()
            while not stop_event.is_set():
                if time.monotonic() - last_snapshot_monotonic >= 5:
                    persist_session_snapshot()
                    last_snapshot_monotonic = time.monotonic()
                if _page_is_tiktok_live(page, username) and not session["page_loaded"]:
                    try:
                        ready_state = await page.evaluate("document.readyState")
                    except Exception:
                        ready_state = "loading"
                    session["page_loaded"] = ready_state in {"interactive", "complete"}
                    if session["page_loaded"]:
                        print(
                            f"[PAGE LOADED] live_url={sanitize_url(str(page.url))}",
                            flush=True,
                        )
                try:
                    page_path = urlsplit(str(page.url)).path.casefold()
                except ValueError:
                    page_path = ""
                if "/login" in page_path and not login_notice_printed:
                    print(
                        "[HUMAN ACTION REQUIRED - LOGIN] Log into TikTok manually in Chrome, then return to the LIVE page.",
                        flush=True,
                    )
                    login_notice_printed = True
                elif not login_notice_printed and await _login_required_visible(page):
                    print(
                        "[HUMAN ACTION REQUIRED - LOGIN] Log into TikTok manually in Chrome; "
                        "the probe will continue observing after login.",
                        flush=True,
                    )
                    login_notice_printed = True
                if (
                    args.duration_seconds is not None
                    and time.monotonic() - started_monotonic >= args.duration_seconds
                ):
                    print(f"[TIME LIMIT] ran for {args.duration_seconds:g}s", flush=True)
                    break
                if not context.pages:
                    print("[PAGE CLOSED] no pages remain in the selected Chrome context", flush=True)
                    break
                if await _verification_visible(page):
                    if not verification_was_visible:
                        print(
                            "[HUMAN ACTION REQUIRED — VERIFICATION] Complete the challenge manually in Chrome; "
                            "frame observation remains active.",
                            flush=True,
                        )
                    verification_was_visible = True
                else:
                    verification_was_visible = False
                try:
                    remaining = (
                        max(0.05, args.duration_seconds - (time.monotonic() - started_monotonic))
                        if args.duration_seconds is not None
                        else 1.0
                    )
                    await asyncio.wait_for(stop_event.wait(), timeout=min(1.0, remaining))
                except asyncio.TimeoutError:
                    continue
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
        if context is not None and not cdp_was_connected:
            try:
                await context.close()
            except Exception:
                pass
        recorder.close()
        session["ended_at"] = timestamp_utc()
        session["collector_ended_at_utc"] = session["ended_at"]
        session["status"] = "stopped"
        session["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
        target_active_seconds = float(session["target_live_websocket_active_seconds"])
        for socket in sockets.values():
            if (
                socket["live_candidate"]
                and socket["opened_monotonic"] is not None
                and socket.get("closed_monotonic") is None
            ):
                target_active_seconds += max(0.0, time.monotonic() - socket["opened_monotonic"])
        session["target_live_websocket_active_seconds"] = round(target_active_seconds, 3)
        session["captured_frame_count"] = recorder.frame_count
        session["captured_binary_frame_count"] = recorder.binary_frame_count
        session["detected_method_counts"] = dict(sorted(recorder.method_counts.items()))
        session["normalized_event_counts"] = dict(sorted(recorder.event_counts.items()))
        session["decoded_field_counts"] = dict(sorted(recorder.decoded_field_counts.items()))
        session["ranking_snapshot_count"] = recorder.ranking_snapshot_count
        end_event = recorder.live_end_event
        session["live_end_detection"] = {
            "status": "LIVE_ENDED" if end_event else "NOT_OBSERVED",
            "event_count": recorder.event_counts.get("live_ended", 0),
            "room_id": end_event.get("room_id") if end_event else None,
            "timestamp_utc": end_event.get("timestamp_utc") if end_event else None,
            "received_at_utc": end_event.get("received_at_utc") if end_event else None,
            "msg_id": end_event.get("msg_id") if end_event else None,
        }
        session["event_category_counts"] = {
            label: recorder.category_counts.get(label, 0)
            for label in (*METHOD_LABELS.values(), "Other")
        }
        session["duplicate_event_counts"] = dict(sorted(recorder.duplicate_event_counts.items()))
        session["decoding_errors"] = dict(sorted(recorder.decoding_errors.items()))
        session["feasibility"] = _feasibility_report(session, recorder)
        if session["live_preflight"]["status"] == "OFFLINE":
            session["transport_result"] = "NOT_TESTED_LIVE_OFFLINE"
            print("[NOT TESTED] Preflight says OFFLINE; this is not a transport failure.", flush=True)
        persist_session_snapshot()
        try:
            report_path = write_live_summary(recorder.session_dir)
            print(f"[REPORT] {report_path}", flush=True)
        except Exception as error:
            print(f"[REPORT ERROR] {type(error).__name__}", flush=True)
        _print_summary(recorder, session)
    return session_dir


def _print_summary(recorder: FrameRecorder, session: dict[str, Any]) -> None:
    print("\n[SUMMARY]", flush=True)
    for method in METHOD_LABELS:
        print(f"{method}: {recorder.method_counts.get(method, 0)}", flush=True)
    print(f"Other: {recorder.category_counts.get('Other', 0)}", flush=True)
    print(f"Binary frames: {recorder.binary_frame_count}", flush=True)
    print(
        f"Raw frames saved: {recorder.saved_raw_frame_count} "
        f"(storage={'enabled' if recorder.save_raw_frames else 'disabled'})",
        flush=True,
    )
    print(
        "LIVE WebSocket connections: "
        f"{session['target_websocket_connection_count']} "
        f"(opened={session['target_live_websocket_open_count']})",
        flush=True,
    )
    print(f"LIVE binary frames: {session['target_binary_frame_count']}", flush=True)
    print(
        "LIVE socket active: "
        f"{session['target_live_websocket_active_seconds']:.1f}s; "
        f"closes={session['target_live_websocket_close_count']}",
        flush=True,
    )
    print(f"Discovered WebSocket hosts: {', '.join(session['discovered_websocket_hostnames']) or 'none'}", flush=True)
    print(f"LIVE preflight: {session['live_preflight']['status']}", flush=True)
    print(
        "LIVE end detection: "
        f"{session.get('live_end_detection', {}).get('status', 'NOT_OBSERVED')}",
        flush=True,
    )
    print(f"Normalized events: {dict(recorder.event_counts)}", flush=True)
    print(f"Highest feasibility level: {session.get('feasibility', {}).get('highest_level', 0)}", flush=True)
    print(f"Decoding errors: {sum(recorder.decoding_errors.values())}", flush=True)
    print(f"Session output: {recorder.session_dir}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Passively capture TikTok LIVE WebSocket frames received by Chromium."
    )
    parser.add_argument("--username", help="TikTok username, with or without leading @")
    parser.add_argument("--url", help="LIVE page URL; defaults to https://www.tiktok.com/@USERNAME/live")
    parser.add_argument(
        "--browser-mode",
        choices=("launch", "cdp"),
        default="launch",
        help="launch persistent Chromium or attach to an existing Chrome instance",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="loopback Chrome remote-debugging endpoint for CDP mode",
    )
    parser.add_argument(
        "--page-index",
        type=int,
        help="zero-based page index shown by CDP context/page enumeration",
    )
    parser.add_argument("--profile-dir", default="data/browser_profile")
    parser.add_argument("--output-dir", default="data/browser_ws_probe")
    parser.add_argument(
        "--save-raw-frames",
        action="store_true",
        help="Save a bounded sample of raw binary WebSocket payloads for debugging",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=5000,
        help="Maximum observed WebSocket frames saved to disk when --save-raw-frames is enabled; never stops event collection",
    )
    parser.add_argument(
        "--reload-live-page",
        action="store_true",
        help="reload the selected matching LIVE tab after CDP attaches so new sockets are observed",
    )
    parser.add_argument(
        "--clear-browser-cache-on-start",
        action="store_true",
        help="clear Chromium's HTTP cache after CDP attaches; cookies and site data are preserved",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        help="Optional run-time limit, useful for short smoke tests",
    )
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window")
    return parser


class BrowserNetworkProvider(BaseEventProvider):
    """Observe the LIVE WebSocket owned by an existing Chrome session via CDP."""

    def __init__(
        self,
        username: str,
        *,
        url: str | None = None,
        browser_mode: str = "cdp",
        cdp_url: str = "http://127.0.0.1:9222",
        page_index: int | None = None,
        profile_dir: str | Path = "data/browser_profile",
        output_dir: str | Path = "data/browser_ws_probe",
        max_frames: int = 5000,
        save_raw_frames: bool = False,
        duration_seconds: float | None = None,
        headless: bool = False,
        reload_live_page: bool = False,
        clear_browser_cache_on_start: bool = False,
        event_bus: NormalizedEventBus | None = None,
    ) -> None:
        super().__init__(event_bus)
        self.args = argparse.Namespace(
            username=username,
            url=url,
            browser_mode=browser_mode,
            cdp_url=cdp_url,
            page_index=page_index,
            profile_dir=str(profile_dir),
            output_dir=str(output_dir),
            max_frames=max_frames,
            save_raw_frames=save_raw_frames,
            duration_seconds=duration_seconds,
            headless=headless,
            reload_live_page=reload_live_page,
            clear_browser_cache_on_start=clear_browser_cache_on_start,
            event_bus=event_bus,
        )

    async def run(self) -> Path:
        return await run_probe(self.args)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.max_frames < 1:
        parser.error("--max-frames must be at least 1")
    if args.duration_seconds is not None and args.duration_seconds <= 0:
        parser.error("--duration-seconds must be greater than 0")
    if args.browser_mode == "cdp" and not _local_cdp_endpoint(args.cdp_url):
        parser.error("--cdp-url must use a loopback host such as http://127.0.0.1:9222")
    if args.browser_mode == "launch" and _uses_everyday_chrome_profile(args.profile_dir):
        parser.error("--profile-dir must be isolated from Chrome's everyday User Data directory")
    if args.page_index is not None and args.page_index < 0:
        parser.error("--page-index must be zero or greater")
    try:
        _resolve_user_and_url(args.username, args.url)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        provider = BrowserNetworkProvider(
            args.username,
            url=args.url,
            browser_mode=args.browser_mode,
            cdp_url=args.cdp_url,
            page_index=args.page_index,
            profile_dir=args.profile_dir,
            output_dir=args.output_dir,
            max_frames=args.max_frames,
            save_raw_frames=args.save_raw_frames,
            duration_seconds=args.duration_seconds,
            headless=args.headless,
            reload_live_page=args.reload_live_page,
            clear_browser_cache_on_start=args.clear_browser_cache_on_start,
        )
        session_dir = asyncio.run(provider.run())
        print(f"[SAVED] {session_dir}", flush=True)
    except KeyboardInterrupt:
        print("\n[STOP] interrupted by user", flush=True)
    except (RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
