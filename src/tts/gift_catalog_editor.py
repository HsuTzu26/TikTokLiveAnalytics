"""Pure helpers for applying manually reviewed gift-name translations."""

from __future__ import annotations

import copy
from typing import Any, Iterable


TRANSLATION_FIELDS = {
    "中文顯示名": "name_zh_display",
    "TTS 發音名": "name_zh_tts",
}
EDITOR_COLUMNS = [
    "圖片",
    "Gift ID",
    "Gift Name",
    "直播原名",
    "Coin",
    "資料來源",
    "中文顯示名",
    "TTS 發音名",
    "狀態",
]


def catalog_statistics(catalog: dict[str, Any]) -> dict[str, int]:
    gifts = catalog.get("gifts", {})
    if not isinstance(gifts, dict):
        return {"total": 0, "saved": 0, "pending": 0, "website": 0, "observed": 0, "event_only": 0}
    saved = sum(
        bool(gift.get("name_zh_display") and gift.get("name_zh_tts"))
        for gift in gifts.values()
    )
    website = observed = event_only = 0
    for gift in gifts.values():
        sources = gift.get("sources") if isinstance(gift.get("sources"), dict) else {}
        in_website = isinstance(sources.get("standout_catalog"), dict)
        original_source = sources.get("original_name")
        was_observed = (
            isinstance(original_source, dict)
            and original_source.get("kind") == "observed_event"
        )
        website += int(in_website)
        observed += int(was_observed)
        event_only += int(was_observed and not in_website)
    total = len(gifts)
    return {
        "total": total,
        "saved": saved,
        "pending": total - saved,
        "website": website,
        "observed": observed,
        "event_only": event_only,
    }


def merge_observed_gifts(
    catalog: dict[str, Any],
    observations: Iterable[dict[str, Any]],
    *,
    updated_at: str,
) -> tuple[dict[str, Any], int]:
    """Add newly observed event names to the catalog without changing translations."""
    updated = copy.deepcopy(catalog)
    gifts = updated.setdefault("gifts", {})
    if not isinstance(gifts, dict):
        raise ValueError("Gift catalog has an invalid gifts mapping.")
    changed_ids: set[str] = set()
    for observation in observations:
        gift_id = str(observation.get("gift_id") or "").strip()
        raw_name = observation.get("raw_name")
        if not gift_id or not isinstance(raw_name, str) or not raw_name:
            continue
        gift = gifts.get(gift_id)
        if not isinstance(gift, dict):
            gift = {
                "gift_id": gift_id,
                "standout_name": None,
                "original_name": raw_name,
                "original_names_seen": [],
                "name_en": raw_name if raw_name.isascii() and any(char.isalpha() for char in raw_name) else None,
                "name_zh_display": None,
                "name_zh_tts": None,
                "diamond_count": None,
                "diamond_count_scope": "observed_event",
                "coin_price": None,
                "image_url": None,
                "sources": {},
            }
            gifts[gift_id] = gift
        names = gift.get("original_names_seen")
        if not isinstance(names, list):
            names = []
            gift["original_names_seen"] = names
        if raw_name not in names:
            names.append(raw_name)
            changed_ids.add(gift_id)
        if not gift.get("original_name"):
            gift["original_name"] = raw_name
            changed_ids.add(gift_id)
        if (
            not gift.get("name_en")
            and raw_name.isascii()
            and any(char.isalpha() for char in raw_name)
        ):
            gift["name_en"] = raw_name
            changed_ids.add(gift_id)

        sources = gift.setdefault("sources", {})
        if not isinstance(sources, dict):
            sources = {}
            gift["sources"] = sources
        if "original_name" not in sources:
            sources["original_name"] = {
                "kind": "observed_event",
                "username": observation.get("username"),
                "session_id": observation.get("session_id"),
                "first_seen_at_local": observation.get("first_seen_at_local"),
            }
            changed_ids.add(gift_id)
        if gift.get("name_en") == raw_name and "name_en" not in sources:
            sources["name_en"] = {
                "kind": "observed_event",
                "session_id": observation.get("session_id"),
            }
            changed_ids.add(gift_id)

        coin = observation.get("diamond_count")
        if gift.get("coin_price") is None and isinstance(coin, (int, float)):
            gift["coin_price"] = int(coin)
            sources.setdefault("coin_price", {
                "kind": "observed_event",
                "field": "diamond_count",
                "session_id": observation.get("session_id"),
            })
            changed_ids.add(gift_id)

    for gift_id in changed_ids:
        gift = gifts[gift_id]
        pending = [
            field
            for field in ("name_en", "name_zh_display", "name_zh_tts")
            if not gift.get(field)
        ]
        gift["pending_fields"] = pending
        gift["mapping_status"] = "mapped" if not pending else "needs_review"
        gift["updated_at"] = updated_at
    if changed_ids:
        updated["updated_at"] = updated_at[:10]
    return updated, len(changed_ids)


def build_editor_rows(
    catalog: dict[str, Any],
    *,
    view: str,
    query: str = "",
    source_scope: str = "全部來源",
) -> list[dict[str, Any]]:
    """Build ID-unique editor rows for saved, pending, or all translations."""
    if view not in {"已儲存", "待補", "全部"}:
        raise ValueError(f"Unknown gift translation view: {view}")
    if source_scope not in {"網站清單", "全部來源", "直播實測追加"}:
        raise ValueError(f"Unknown gift source scope: {source_scope}")
    gifts = catalog.get("gifts", {})
    if not isinstance(gifts, dict):
        return []
    folded_query = query.casefold().strip()
    rows = []
    for gift_id, gift in sorted(gifts.items(), key=lambda pair: int(pair[0])):
        if not isinstance(gift, dict):
            continue
        display_name = str(gift.get("name_zh_display") or "").strip()
        tts_name = str(gift.get("name_zh_tts") or "").strip()
        saved = bool(display_name and tts_name)
        if view == "已儲存" and not saved:
            continue
        if view == "待補" and saved:
            continue

        sources = gift.get("sources") if isinstance(gift.get("sources"), dict) else {}
        in_website = isinstance(sources.get("standout_catalog"), dict)
        original_source = sources.get("original_name")
        observed = (
            isinstance(original_source, dict)
            and original_source.get("kind") == "observed_event"
        )
        if source_scope == "網站清單" and not in_website:
            continue
        if source_scope == "直播實測追加" and (in_website or not observed):
            continue
        if in_website and observed:
            source_label = "網站清單＋直播實測"
        elif in_website:
            source_label = "網站清單"
        else:
            source_label = "直播實測追加"

        gift_name = gift.get("standout_name") or gift.get("name_en") or ""
        original_name = gift.get("original_name") or ""
        if not gift_name:
            gift_name = original_name
            original_name = ""
        elif str(original_name).strip().casefold() == str(gift_name).strip().casefold():
            original_name = ""
        row = {
            "圖片": gift.get("image_url") or "",
            "Gift ID": str(gift_id),
            "Gift Name": gift_name,
            "直播原名": original_name,
            "Coin": gift.get("coin_price"),
            "資料來源": source_label,
            "中文顯示名": display_name,
            "TTS 發音名": tts_name,
            "狀態": "已儲存" if saved else "待補",
        }
        if folded_query and not any(
            folded_query in str(row[key] or "").casefold()
            for key in (
                "Gift ID",
                "Gift Name",
                "直播原名",
                "中文顯示名",
                "TTS 發音名",
            )
        ):
            continue
        rows.append(row)
    return rows


def apply_translation_edits(
    catalog: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    *,
    updated_at: str,
) -> tuple[dict[str, Any], int]:
    """Apply editor rows by Gift ID, leaving event names and values untouched."""
    updated = copy.deepcopy(catalog)
    gifts = updated.get("gifts")
    if not isinstance(gifts, dict):
        raise ValueError("Gift catalog has an invalid gifts mapping.")

    changed = 0
    seen: set[str] = set()
    for row in rows:
        gift_id = str(row.get("Gift ID") or "").strip()
        if not gift_id or gift_id in seen:
            continue
        seen.add(gift_id)
        gift = gifts.get(gift_id)
        if not isinstance(gift, dict):
            continue

        sources = gift.setdefault("sources", {})
        for label, field in TRANSLATION_FIELDS.items():
            value = row.get(label)
            name = str(value).strip() if value is not None else ""
            gift[field] = name or None
            if name:
                sources[field] = {
                    "kind": "manual_translation",
                    "updated_at": updated_at,
                }
            else:
                sources.pop(field, None)
        pending = [
            field
            for field in ("name_en", "name_zh_display", "name_zh_tts")
            if not gift.get(field)
        ]
        gift["pending_fields"] = pending
        gift["mapping_status"] = "mapped" if not pending else "needs_review"
        changed += 1

    if changed:
        updated["updated_at"] = updated_at[:10]
    return updated, changed


def merge_translation_rows(
    catalog: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    *,
    updated_at: str,
    source_file: str,
) -> tuple[dict[str, Any], int]:
    """Validate row identity against the catalog, then merge supplied names."""
    gifts = catalog.get("gifts")
    if not isinstance(gifts, dict):
        raise ValueError("Gift catalog has an invalid gifts mapping.")

    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        gift_id = str(row.get("Gift ID") or "").strip()
        if not gift_id:
            raise ValueError("A translation row has no Gift ID.")
        if gift_id in seen:
            raise ValueError(f"Translation file contains duplicate Gift ID {gift_id}.")
        seen.add(gift_id)
        gift = gifts.get(gift_id)
        if not isinstance(gift, dict):
            raise ValueError(f"Gift ID {gift_id} is not present in the catalog.")

        expected_name = str(
            gift.get("standout_name") or gift.get("name_en") or gift.get("original_name") or ""
        ).strip()
        expected_coin = gift.get("coin_price")
        expected_coin = "" if expected_coin is None else str(expected_coin)
        expected_image = str(gift.get("image_url") or "")
        checks = (
            ("Gift Name", expected_name),
            ("Coin", expected_coin),
            ("Image URL", expected_image),
        )
        for column, expected in checks:
            supplied = str(row.get(column) or "").strip()
            if supplied != expected:
                raise ValueError(
                    f"Gift ID {gift_id} {column} does not match the catalog; "
                    "the row may have been shifted or altered."
                )

        display_name = str(row.get("中文顯示名") or "").strip()
        tts_name = str(row.get("TTS 發音名") or "").strip()
        if not display_name or not tts_name:
            raise ValueError(f"Gift ID {gift_id} is missing one of its Chinese names.")
        validated.append({
            "Gift ID": gift_id,
            "中文顯示名": display_name,
            "TTS 發音名": tts_name,
        })

    updated, count = apply_translation_edits(
        catalog, validated, updated_at=updated_at
    )
    for row in validated:
        gift = updated["gifts"][row["Gift ID"]]
        for field in TRANSLATION_FIELDS.values():
            gift["sources"][field]["source_file"] = source_file
    return updated, count
