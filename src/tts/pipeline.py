"""Pure chat preparation and guardrails for the optional TTS worker."""
from __future__ import annotations

import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import asdict, dataclass

from .gift_catalog import (
    DEFAULT_GIFT_SPEECH_NAMES,
    DEFAULT_GIFT_SPEECH_NAMES_BY_RAW,
    gift_metadata_for,
)


URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
WHITESPACE_PATTERN = re.compile(r"\s+")
REPEATED_CHARACTER_PATTERN = re.compile(r"(.)\1{5,}")
TEXT_EMOTICON_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:\^[_\- ]*\^|[:;=8xX][-^']?[)(/DPp\\|]|<3|xD)(?![A-Za-z0-9])"
)
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
LATIN_KANA_BRIDGE_PATTERN = re.compile(
    r"(?<=[A-Za-z])[\u3040-\u30FF]+(?=[A-Za-z])"
)
TIKTOK_EMOJI_TEXT = {
    "[laugh]": "笑翻",
    "[laughing]": "笑翻",
}
TIKTOK_EMOJI_TEXT_PATTERN = re.compile(
    "|".join(re.escape(token) for token in sorted(TIKTOK_EMOJI_TEXT, key=len, reverse=True)),
    re.IGNORECASE,
)
COMMON_EMOJI_SPEECH = {
    "😂": ("笑哭", "face with tears of joy"),
    "🤣": ("笑翻", "rolling with laughter"),
    "❤️": ("愛心", "heart"),
    "❤": ("愛心", "heart"),
    "💕": ("愛心", "hearts"),
    "💖": ("愛心", "sparkling heart"),
    "😍": ("喜歡", "love it"),
    "🥰": ("喜歡", "feeling loved"),
    "😭": ("哭哭", "crying"),
    "😢": ("難過", "sad"),
    "👍": ("讚", "thumbs up"),
    "👏": ("拍手", "applause"),
    "🙏": ("感謝", "thank you"),
    "👋": ("揮手", "waving"),
    "🔥": ("好熱烈", "fire"),
    "🎉": ("恭喜", "congratulations"),
    "🎂": ("生日快樂", "happy birthday"),
    "🌹": ("玫瑰", "rose"),
    "💐": ("花束", "bouquet"),
    "💪": ("加油", "keep it up"),
    "🤔": ("思考", "thinking"),
    "✨": ("閃亮", "sparkles"),
}


@dataclass(frozen=True)
class TTSSettings:
    zh_voice: str = "zh-TW-HsiaoChenNeural"
    en_voice: str = "en-US-AriaNeural"
    rate_percent: int = 35
    volume: int = 75
    max_text_length: int = 80
    queue_size: int = 500
    chat_ttl_seconds: float = 15.0
    user_cooldown_seconds: float = 0.0
    duplicate_window_seconds: float = 0.0
    min_request_interval_seconds: float = 0.5
    ignore_emoji: bool = True
    speak_common_emoji: bool = True
    gift_followup_audio_path: str = ""
    gift_sound_by_id: tuple[tuple[str, str], ...] = ()
    gift_name_by_id: tuple[tuple[str, str], ...] = ()
    chat_tts_filter: str = "all"
    chat_tts_user_allowlist: tuple[str, ...] = ()
    member_entry_sound_path: str = ""
    member_entry_sound_by_user: tuple[tuple[str, str], ...] = ()
    fan_club_entry_sound_path: str = ""
    fan_club_entry_min_level: int = 10
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
        gift_sound_by_id = value.get("gift_sound_by_id", {})
        if isinstance(gift_sound_by_id, dict):
            gift_sound_by_id = tuple(
                (str(gift_id).strip()[:64], str(path).strip()[:1000])
                for gift_id, path in gift_sound_by_id.items()
                if str(gift_id).strip() and str(path).strip()
            )[:500]
        else:
            gift_sound_by_id = ()
        gift_name_by_id = value.get("gift_name_by_id", {})
        if isinstance(gift_name_by_id, dict):
            gift_name_by_id = tuple(
                (str(gift_id).strip()[:64], str(name).strip()[:80])
                for gift_id, name in gift_name_by_id.items()
                if str(gift_id).strip() and str(name).strip()
            )[:500]
        else:
            gift_name_by_id = ()

        chat_filter = str(value.get("chat_tts_filter") or "all").strip().casefold()
        if chat_filter not in {"all", "fan_club_only", "selected_users"}:
            chat_filter = "all"
        chat_allowlist = value.get("chat_tts_user_allowlist", ())
        if isinstance(chat_allowlist, str):
            chat_allowlist = chat_allowlist.splitlines()
        if not isinstance(chat_allowlist, (list, tuple, set)):
            chat_allowlist = ()
        member_sound_by_user = value.get("member_entry_sound_by_user", {})
        if isinstance(member_sound_by_user, dict):
            member_sound_by_user = tuple(
                (str(user).strip().lstrip("@").casefold()[:128], str(path).strip()[:1000])
                for user, path in member_sound_by_user.items()
                if str(user).strip() and str(path).strip()
            )[:500]
        else:
            member_sound_by_user = ()

        if "chat_ttl_seconds" not in value and "max_queue_age_seconds" in value:
            value = {**value, "chat_ttl_seconds": value["max_queue_age_seconds"]}

        return cls(
            zh_voice=str(value.get("zh_voice") or cls.zh_voice).strip()[:100],
            en_voice=str(value.get("en_voice") or cls.en_voice).strip()[:100],
            rate_percent=bounded_int("rate_percent", 35, -50, 100),
            volume=bounded_int("volume", 75, 0, 100),
            max_text_length=bounded_int("max_text_length", 80, 1, 200),
            queue_size=bounded_int("queue_size", 500, 1, 5000),
            # Read the previous setting name so an existing local config keeps
            # its chosen Chat age when upgrading to the reliability-first rule.
            chat_ttl_seconds=bounded_float(
                "chat_ttl_seconds", 15.0, 5.0, 15.0
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
            speak_common_emoji=bool(value.get("speak_common_emoji", True)),
            gift_followup_audio_path=str(value.get("gift_followup_audio_path") or "").strip()[:1000],
            gift_sound_by_id=gift_sound_by_id,
            gift_name_by_id=gift_name_by_id,
            chat_tts_filter=chat_filter,
            chat_tts_user_allowlist=tuple(
                dict.fromkeys(
                    str(user).strip().lstrip("@").casefold()[:128]
                    for user in chat_allowlist
                    if str(user).strip()
                )
            )[:500],
            member_entry_sound_path=str(value.get("member_entry_sound_path") or "").strip()[:1000],
            member_entry_sound_by_user=member_sound_by_user,
            fan_club_entry_sound_path=str(value.get("fan_club_entry_sound_path") or "").strip()[:1000],
            fan_club_entry_min_level=bounded_int("fan_club_entry_min_level", 10, 1, 50),
            blacklist_terms=tuple(
                str(term).strip()[:80]
                for term in blacklist
                if str(term).strip()
            )[:200],
        )

    def to_mapping(self) -> dict:
        result = asdict(self)
        result["blacklist_terms"] = list(self.blacklist_terms)
        result["gift_sound_by_id"] = dict(self.gift_sound_by_id)
        result["gift_name_by_id"] = dict(self.gift_name_by_id)
        result["chat_tts_user_allowlist"] = list(self.chat_tts_user_allowlist)
        result["member_entry_sound_by_user"] = dict(self.member_entry_sound_by_user)
        return result


def is_fan_club_member(event: dict) -> bool:
    try:
        if int(event.get("fan_club_level") or 0) > 0:
            return True
    except (TypeError, ValueError, OverflowError):
        pass
    if event.get("is_subscriber_of_anchor") is True:
        return True
    badges = event.get("badges") or ()
    return any(
        isinstance(badge, dict)
        and str(badge.get("scene") or "").casefold() == "fans"
        for badge in badges
    )


def should_speak_chat(event: dict, settings: TTSSettings) -> bool:
    """Apply the optional sender filter to Chat only; Gifts remain unaffected."""
    if settings.chat_tts_filter == "all":
        return True
    if settings.chat_tts_filter == "fan_club_only":
        return is_fan_club_member(event)
    if settings.chat_tts_filter == "selected_users":
        allowed = set(settings.chat_tts_user_allowlist)
        if not allowed:
            return False
        candidates = {
            str(event.get(field) or "").strip().lstrip("@").casefold()
            for field in ("unique_id", "user_id")
        }
        return bool(allowed & candidates)
    return True


def member_entry_sound(event: dict, settings: TTSSettings) -> str | None:
    """Choose a member-entry sound, preferring a user mapping over fan-club rules."""
    try:
        action = int(event.get("action_code"))
    except (TypeError, ValueError, OverflowError):
        action = None
    if action not in (None, 1):
        return None

    sounds = dict(settings.member_entry_sound_by_user)
    for field in ("unique_id", "user_id"):
        identity = str(event.get(field) or "").strip().lstrip("@").casefold()
        if identity and identity in sounds:
            return sounds[identity]

    if settings.fan_club_entry_sound_path:
        try:
            fan_level = int(event.get("fan_club_level") or 0)
        except (TypeError, ValueError, OverflowError):
            fan_level = 0
        if fan_level >= settings.fan_club_entry_min_level:
            return settings.fan_club_entry_sound_path
    return settings.member_entry_sound_path or None


def spoken_gift_name(
    gift_id: object,
    gift_name: object,
    overrides: tuple[tuple[str, str], ...] = (),
    *,
    tts_name: object = None,
) -> str:
    """Return a configured/localized name for speech while raw events stay intact."""
    key = str(gift_id or "").strip()
    raw_name = str(gift_name or "").strip()
    configured = dict(overrides).get(key)
    localized_raw = DEFAULT_GIFT_SPEECH_NAMES_BY_RAW.get(raw_name.casefold())
    catalog_name = gift_metadata_for(key, gift_name).get("name_zh_tts")
    return str(
        configured
        or tts_name
        or DEFAULT_GIFT_SPEECH_NAMES.get(key)
        or catalog_name
        or localized_raw
        or raw_name
        or "未知禮物"
    ).strip()


@dataclass(frozen=True)
class PreparedChat:
    user_key: str | None
    text: str
    voice: str
    received_at_ms: int | None
    segments: tuple[tuple[str, str], ...] = ()
    followup_audio_path: str | None = None
    event_id: str | None = None
    gift_id: str | None = None
    retry_attempts: int = 0
    source_session_dir: str | None = None
    session_id: str | None = None
    gift_name: str | None = None
    spoken_gift_name: str | None = None
    gift_quantity: int | None = None
    gift_diamond_total: int | None = None
    gift_name_en: str | None = None
    gift_name_zh_display: str | None = None
    gift_name_zh_tts: str | None = None
    gift_name_original: str | None = None
    gift_catalog_status: str | None = None
    gift_name_resolution_ms: float | None = None
    speech_preparation_ms: float | None = None
    speech_segmentation_fallback: bool = False
    processing_timings_ms: tuple[tuple[str, float], ...] = ()


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


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x20000 <= codepoint <= 0x2FA1F
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _keep_speech_compatible_characters(text: str) -> str:
    """Keep English/CJK text and discard decorative scripts unsupported by voices."""
    result: list[str] = []
    for character in text:
        codepoint = ord(character)
        if (
            character.isascii()
            or _is_cjk(character)
            or 0x3000 <= codepoint <= 0x303F  # CJK punctuation
        ):
            result.append(character)
            continue

        # Preserve accented Latin names as readable English. NFKC above folds
        # mathematical/script alphabets to ordinary Latin characters first.
        ascii_base = "".join(
            item
            for item in unicodedata.normalize("NFKD", character)
            if item.isascii() and item.isalnum()
        )
        result.append(ascii_base or " ")
    return "".join(result)


def _replace_common_emoji(text: str) -> str:
    value = text
    # Replace longer graphemes first so variation-selector forms stay together.
    for emoji in sorted(COMMON_EMOJI_SPEECH, key=len, reverse=True):
        value = value.replace(emoji, f" {COMMON_EMOJI_SPEECH[emoji][0]} ")
    value = TIKTOK_EMOJI_TEXT_PATTERN.sub(
        lambda match: f" {TIKTOK_EMOJI_TEXT[match.group(0).lower()]} ",
        value,
    )
    return value


def clean_chat_text(
    text: object,
    *,
    max_length: int = 80,
    ignore_emoji: bool = True,
    speak_common_emoji: bool = False,
) -> str:
    if not isinstance(text, str):
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = URL_PATTERN.sub(" ", value)
    value = TEXT_EMOTICON_PATTERN.sub(" ", value)
    value = value.replace("@", " at ")
    value = ACRONYM_PATTERN.sub(
        lambda match: ACRONYM_SPOKEN_FORMS[match.group(0).upper()],
        value,
    )
    if speak_common_emoji:
        value = _replace_common_emoji(value)
    value = "".join(
        " " if unicodedata.category(char) == "Cc" else char
        for char in value
        if unicodedata.category(char) != "Cf"
    )
    if ignore_emoji:
        value = "".join(char for char in value if not _is_emoji_like(char))
    value = _keep_speech_compatible_characters(value)
    # Kana embedded between Latin letters is usually part of a stylized Latin
    # handle. Treat it as a word boundary so one mention stays one English
    # segment instead of forcing an extra remote Chinese voice request.
    value = LATIN_KANA_BRIDGE_PATTERN.sub(" ", value)
    value = REPEATED_CHARACTER_PATTERN.sub(lambda match: match.group(1) * 3, value)
    value = WHITESPACE_PATTERN.sub(" ", value).strip()
    return value[: max(1, int(max_length))].rstrip()


def dominant_language(text: str) -> str:
    """Return the MVP whole-message language route: zh-TW or en-US."""
    cjk_count = sum(
        _is_cjk(char)
        for char in text
    )
    latin_count = sum(char.isascii() and char.isalpha() for char in text)
    return "zh-TW" if cjk_count and cjk_count >= latin_count else "en-US"


def speech_segments(
    text: str, *, zh_voice: str, en_voice: str
) -> tuple[tuple[str, str], ...]:
    """Split bilingual text at script changes while keeping punctuation attached."""
    if not text:
        return ()
    default_language = dominant_language(text)
    current_language: str | None = None
    current_text: list[str] = []
    segments: list[tuple[str, str]] = []
    leading: list[str] = []

    def flush() -> None:
        nonlocal current_text, current_language, leading
        if current_text and current_language:
            content = "".join(leading + current_text).strip()
            if content:
                voice = zh_voice if current_language == "zh-TW" else en_voice
                segments.append((content, voice))
        current_text = []
        current_language = None
        leading = []

    for character in text:
        if _is_cjk(character):
            language = "zh-TW"
        elif character.isalpha() and character.isascii():
            language = "en-US"
        elif character.isalpha():
            language = "en-US"
        elif character.isdigit():
            language = current_language or default_language
        else:
            language = None

        if language is None:
            if current_language is None:
                leading.append(character)
            else:
                current_text.append(character)
            continue

        if current_language is not None and language != current_language:
            flush()
        if current_language is None:
            current_language = language
        current_text.append(character)

    flush()
    return tuple(segments)


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
        preparation_started = time.perf_counter()
        now = time.monotonic() if now is None else now
        text = clean_chat_text(
            event.get("comment"),
            max_length=self.settings.max_text_length,
            ignore_emoji=self.settings.ignore_emoji,
            speak_common_emoji=self.settings.speak_common_emoji,
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

        segments = speech_segments(
            text,
            zh_voice=self.settings.zh_voice,
            en_voice=self.settings.en_voice,
        )
        segmentation_fallback = len(segments) > 4
        if segmentation_fallback:
            fallback_voice = (
                self.settings.zh_voice
                if dominant_language(text) == "zh-TW"
                else self.settings.en_voice
            )
            # Rapid script alternation can turn one short line into many remote
            # synthesis calls. Keep the full text and use one voice in that case.
            segments = ((text, fallback_voice),)
        voice = segments[0][1] if segments else self.settings.en_voice
        received_at_ms = event.get("received_at_ms")
        try:
            received_at_ms = int(received_at_ms) if received_at_ms is not None else None
        except (TypeError, ValueError):
            received_at_ms = None
        return PreparedChat(
            user_key=user_key,
            text=text,
            voice=voice,
            received_at_ms=received_at_ms,
            segments=segments,
            speech_preparation_ms=round(
                (time.perf_counter() - preparation_started) * 1000, 3
            ),
            speech_segmentation_fallback=segmentation_fallback,
        ), None
