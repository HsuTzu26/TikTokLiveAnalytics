from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from zoneinfo import ZoneInfo


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
API_ROOT = "https://api.tik.tools"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"


def _now_fields():
    now = datetime.now(timezone.utc)
    return {
        "captured_at_utc": now.isoformat(),
        "captured_at_local": now.astimezone(TAIPEI_TZ).isoformat(),
    }


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def public_live_snapshot(username: str, timeout: int = 15):
    url = f"https://www.tiktok.com/@{urllib.parse.quote(username)}/live"
    request = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "accept-language": "zh-TW,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        page = response.read().decode("utf-8", errors="replace")
    match = re.search(r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>', page, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError("SIGI_STATE not found")
    payload = json.loads(unescape(match.group(1)))
    candidate = None
    for value in _walk(payload):
        if not isinstance(value, dict):
            continue
        user = value.get("user")
        if isinstance(user, dict) and str(user.get("uniqueId") or "") == username and isinstance(value.get("liveRoom"), dict):
            candidate = value
            break
    if candidate is None:
        raise ValueError("LIVE room snapshot not found")
    user = candidate["user"]
    live_room = candidate.get("liveRoom") or {}
    stats = live_room.get("liveRoomStats") or {}
    return {
        "source": "public_live_page",
        "username": username,
        "room_id": str(user.get("roomId") or "") or None,
        "live": user.get("status") == 2 and live_room.get("status") == 2,
        "title": live_room.get("title"),
        "start_time": live_room.get("startTime"),
        "enter_count": stats.get("enterCount"),
        "viewer_count": stats.get("userCount") or stats.get("viewerCount"),
        "like_count": stats.get("likeCount"),
        "share_count": stats.get("shareCount"),
        **_now_fields(),
    }


class TikToolSnapshots:
    def __init__(self, api_key: str | None = None, cookie_header: str | None = None, timeout: int = 15):
        self.api_key = (api_key or os.environ.get("TIKTOOL_API_KEY") or "").strip()
        self.cookie_header = (cookie_header or os.environ.get("TIKTOK_COOKIE_HEADER") or "").strip()
        self.timeout = timeout

    @property
    def enabled(self):
        return bool(self.api_key)

    def _request(self, path, *, params=None, body=None):
        query = {**(params or {}), "apiKey": self.api_key}
        url = f"{API_ROOT}{path}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"user-agent": USER_AGENT, "accept": "application/json"}
        if body is not None:
            headers["content-type"] = "application/json"
        if self.cookie_header:
            headers["x-cookie-header"] = self.cookie_header
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())
        if payload.get("status_code") not in (None, 0):
            raise RuntimeError(payload.get("message") or f"status_code={payload.get('status_code')}")
        return payload

    def room_info(self, username: str, room_id: str | None = None):
        payload = self._request("/webcast/room_info", body={"room_id": room_id} if room_id else {"unique_id": username})
        return {"source": "tiktool_room_info", "username": username, **_now_fields(), "data": payload.get("data") or {}}

    def rankings(self, username: str, room_id: str | None = None):
        payload = self._request("/webcast/rankings", params={"room_id": room_id} if room_id else {"unique_id": username})
        return {"source": "tiktool_rankings", "username": username, **_now_fields(), "authenticated": bool(self.cookie_header), "data": payload.get("data") or {}, "signed_url_available": bool(payload.get("signed_url"))}

    def gift_info(self, username: str, room_id: str | None = None):
        payload = self._request("/webcast/gift_info", params={"room_id": room_id} if room_id else {"unique_id": username})
        return {"source": "tiktool_gift_info", "username": username, **_now_fields(), "data": payload.get("data") or {}}
