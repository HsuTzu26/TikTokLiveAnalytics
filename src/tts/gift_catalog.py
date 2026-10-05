"""ID-keyed Gift metadata kept separate from TikTok's original event values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).with_name("gift_catalog.json")


def _load_catalog() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    gifts = data.get("gifts", {}) if isinstance(data, dict) else {}
    if not isinstance(gifts, dict):
        return {}
    return {
        str(gift_id): value
        for gift_id, value in gifts.items()
        if isinstance(value, dict)
    }


GIFT_CATALOG = _load_catalog()


def gift_metadata_for(gift_id: object, raw_name: object = None) -> dict[str, Any]:
    """Return a copy of ID metadata without changing the supplied raw name.

    Unknown gifts stay usable as raw text and are explicitly marked for catalog
    review. The returned ``original_name`` is the exact event value when one is
    supplied; whitespace and Unicode normalization belong only in display/TTS.
    """
    key = str(gift_id or "").strip()
    raw = raw_name if isinstance(raw_name, str) else None
    known = GIFT_CATALOG.get(key)
    if known is None:
        return {
            "gift_id": key or None,
            "original_name": raw,
            "original_names_seen": [raw] if raw else [],
            "name_en": None,
            "name_zh_display": None,
            "name_zh_tts": None,
            "diamond_count": None,
            "diamond_count_scope": None,
            "sources": {},
            "mapping_status": "needs_review",
            "pending_fields": ["name_en", "name_zh_display", "name_zh_tts"],
        }

    result = dict(known)
    result["original_names_seen"] = list(known.get("original_names_seen") or [])
    result["pending_fields"] = list(known.get("pending_fields") or [])
    result["sources"] = dict(known.get("sources") or {})
    if raw is not None:
        result["original_name"] = raw
    return result


# Backwards-compatible mappings used by the existing TTS UI and pipeline.
OBSERVED_GIFTS: dict[str, tuple[str, str]] = {
    gift_id: (
        str(metadata.get("original_name") or ""),
        str(metadata.get("name_zh_tts") or metadata.get("original_name") or ""),
    )
    for gift_id, metadata in GIFT_CATALOG.items()
    if metadata.get("sources", {}).get("original_name", {}).get("kind")
    == "observed_event"
}
DEFAULT_GIFT_SPEECH_NAMES = {
    gift_id: str(metadata["name_zh_tts"])
    for gift_id, metadata in GIFT_CATALOG.items()
    if metadata.get("name_zh_tts")
}
DEFAULT_GIFT_SPEECH_NAMES_BY_RAW = {
    raw_name.casefold(): str(metadata["name_zh_tts"])
    for metadata in GIFT_CATALOG.values()
    if metadata.get("name_zh_tts")
    for raw_name in (
        list(metadata.get("original_names_seen", []))
        + ([metadata["name_en"]] if metadata.get("name_en") else [])
    )
    if isinstance(raw_name, str)
    and raw_name.isascii()
    and raw_name.casefold() != str(metadata["name_zh_tts"]).casefold()
}
