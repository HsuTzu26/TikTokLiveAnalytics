"""Pure chat preparation and guardrails for the optional TTS worker."""
from __future__ import annotations

import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import asdict, dataclass


URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
WHITESPACE_PATTERN = re.compile(r"\s+")
REPEATED_CHARACTER_PATTERN = re.compile(r"(.)\1{5,}")
ACRONYM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:IG|FB|YT|DM|VIP)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
ACRONYM_SPOKEN_FORMS = {
    "IG": "I G",
    "FB": "F B",
    "YT": "Y T",
    "DM": "D M",
    "VIP": "V I P",
}


@dataclass(frozen=True)
class TTSSettings:
    zh_voice: str = "zh-TW-HsiaoChenNeural"
    en_voice: str = "en-US-AriaNeural"
    rate_percent: int = 5
    volume: int = 75
    max_text_length: int = 80
    queue_size: int = 500
    max_queue_age_seconds: float = 5.0
    user_cooldown_seconds: float = 0.0
    duplicate_window_seconds: float = 0.0
    min_request_interval_seconds: float = 0.5
    ignore_emoji: bool = True
    blacklist_terms: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> "TTSSettings":
        value = value if isinstance(value, dict) else {}

        def bounded_int(key: str, default: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(value.get(key, default))))
            except (TypeError, ValueError):
                return default

        def bounded_float(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(value.get(key, default))))
            except (TypeError, ValueError):
                return default

        blacklist = value.get("blacklist_terms", ())
        if isinstance(blacklist, str):
            blacklist = blacklist.splitlines()
        if not isinstance(blacklist, (list, tuple)):
            blacklist = ()

        return cls(
            zh_voice=str(value.get("zh_voice") or cls.zh_voice).strip()[:100],
            en_voice=str(value.get("en_voice") or cls.en_voice).strip()[:100],
            rate_percent=bounded_int("rate_percent", 5, -50, 100),
            volume=bounded_int("volume", 75, 0, 100),
            max_text_length=bounded_int("max_text_length", 80, 1, 200),
            queue_size=bounded_int("queue_size", 500, 1, 5000),
            max_queue_age_seconds=bounded_float(
                "max_queue_age_seconds", 5.0, 0.0, 300.0
            ),
            user_cooldown_seconds=bounded_float(
                "user_cooldown_seconds", 0.0, 0.0, 300.0
            ),
            duplicate_window_seconds=bounded_float(
                "duplicate_window_seconds", 0.0, 0.0, 600.0
            ),
            min_request_interval_seconds=bounded_float(
                "min_request_interval_seconds", 0.5, 0.1, 30.0
            ),
            ignore_emoji=bool(value.get("ignore_emoji", True)),
            blacklist_terms=tuple(
                str(term).strip()[:80]
                for term in blacklist
                if str(term).strip()
            )[:200],
        )

    def to_mapping(self) -> dict:
        result = asdict(self)
        result["blacklist_terms"] = list(self.blacklist_terms)
        return result


@dataclass(frozen=True)
class PreparedChat:
    user_key: str | None
    text: str
    voice: str
    received_at_ms: int | None


def _is_emoji_like(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x1F1E6 <= codepoint <= 0x1F1FF
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or codepoint == 0x200D
    )


def clean_chat_text(text: object, *, max_length: int = 80, ignore_emoji: bool = True) -> str:
    if not isinstance(text, str):
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = URL_PATTERN.sub(" ", value)
    value = value.replace("@", " at ")
    value = ACRONYM_PATTERN.sub(
        lambda match: ACRONYM_SPOKEN_FORMS[match.group(0).upper()],
        value,
    )
    value = "".join(
        " " if unicodedata.category(char) == "Cc" else char
        for char in value
        if unicodedata.category(char) != "Cf"
    )
    if ignore_emoji:
        value = "".join(char for char in value if not _is_emoji_like(char))
    value = REPEATED_CHARACTER_PATTERN.sub(lambda match: match.group(1) * 3, value)
    value = WHITESPACE_PATTERN.sub(" ", value).strip()
    return value[: max(1, int(max_length))].rstrip()


def dominant_language(text: str) -> str:
    """Return the MVP whole-message language route: zh-TW or en-US."""
    cjk_count = sum(
        0x3400 <= ord(char) <= 0x4DBF
        or 0x4E00 <= ord(char) <= 0x9FFF
        or 0x20000 <= ord(char) <= 0x2FA1F
        for char in text
    )
    latin_count = sum(char.isascii() and char.isalpha() for char in text)
    return "zh-TW" if cjk_count and cjk_count >= latin_count else "en-US"


class ChatProcessor:
    """Filters duplicate/spammy chat and routes accepted messages to a voice."""

    def __init__(self, settings: TTSSettings):
        self.settings = settings
        self._last_by_user: dict[str, float] = {}
        self._recent_text: OrderedDict[str, float] = OrderedDict()

    def prepare(
        self,
        event: dict,
        *,
        now: float | None = None,
    ) -> tuple[PreparedChat | None, str | None]:
        now = time.monotonic() if now is None else now
        text = clean_chat_text(
            event.get("comment"),
            max_length=self.settings.max_text_length,
            ignore_emoji=self.settings.ignore_emoji,
        )
        if not text:
            return None, "empty_after_cleaning"

        folded = text.casefold()
        if any(term.casefold() in folded for term in self.settings.blacklist_terms):
            return None, "moderation_blacklist"

        user_key = next(
            (
                str(event.get(field)).strip()
                for field in ("unique_id", "user_id", "nickname")
                if event.get(field) is not None and str(event.get(field)).strip()
            ),
            None,
        )
        cooldown = self.settings.user_cooldown_seconds
        if user_key and cooldown > 0:
            previous = self._last_by_user.get(user_key)
            if previous is not None and now - previous < cooldown:
                return None, "user_cooldown"

        is_sticker_message = (
            event.get("message_kind") == "emote" or bool(event.get("emotes"))
        )
        duplicate_key = (
            f"{folded}|{user_key.casefold()}"
            if is_sticker_message and user_key
            else folded
        )
        window = self.settings.duplicate_window_seconds
        if window > 0:
            previous = self._recent_text.get(duplicate_key)
            if previous is not None and now - previous < window:
                return None, "duplicate_text"

        if user_key:
            self._last_by_user[user_key] = now
        if window > 0:
            self._recent_text[duplicate_key] = now
            self._recent_text.move_to_end(duplicate_key)
            while len(self._recent_text) > 10_000:
                self._recent_text.popitem(last=False)

        language = dominant_language(text)
        voice = self.settings.zh_voice if language == "zh-TW" else self.settings.en_voice
        received_at_ms = event.get("received_at_ms")
        try:
            received_at_ms = int(received_at_ms) if received_at_ms is not None else None
        except (TypeError, ValueError):
            received_at_ms = None
        return PreparedChat(user_key, text, voice, received_at_ms), None
