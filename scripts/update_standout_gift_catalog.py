"""Import the public STANDOUT Streamers gift list into our ID-keyed catalog."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


SOURCE_URL = "https://standoutstreamers.com.au/tiktok-gift-catalog/"
CATALOG_PATH = Path(__file__).resolve().parents[1] / "src" / "tts" / "gift_catalog.json"


def fetch_source_html() -> str:
    request = Request(
        SOURCE_URL,
        headers={"User-Agent": "TikTokLiveAnalytics gift catalog importer"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def parse_source_html(html: str) -> dict[str, Any]:
    """Extract the page's embedded gift data, rejecting partial or duplicate lists."""
    marker = "window.TTGL_DATA"
    marker_at = html.find(marker)
    if marker_at < 0:
        raise ValueError("Gift catalog data marker was not found in the page.")
    equals_at = html.find("=", marker_at + len(marker))
    if equals_at < 0:
        raise ValueError("Gift catalog data assignment is malformed.")

    try:
        data, _ = json.JSONDecoder().raw_decode(html[equals_at + 1 :].lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError("Gift catalog JSON could not be decoded.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("gifts"), list):
        raise ValueError("Gift catalog data has an unexpected shape.")

    gifts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data["gifts"]:
        if not isinstance(item, dict):
            raise ValueError("Gift catalog contains a non-object row.")
        gift_id = str(item.get("id", "")).strip()
        name = item.get("name")
        coins = item.get("coins")
        if not gift_id.isdigit() or not isinstance(name, str) or not name.strip():
            raise ValueError("Gift catalog contains a row without a valid ID/name.")
        if isinstance(coins, bool) or not isinstance(coins, int) or coins < 0:
            raise ValueError(f"Gift {gift_id} has an invalid coin price.")
        image_url = item.get("img")
        if image_url is not None and (
            not isinstance(image_url, str) or not image_url.startswith("https://")
        ):
            raise ValueError(f"Gift {gift_id} has an invalid image URL.")
        if gift_id in seen:
            raise ValueError(f"Gift catalog contains duplicate ID {gift_id}.")
        seen.add(gift_id)
        gifts.append({
            "gift_id": gift_id,
            "name": name,
            "coins": coins,
            "image_url": image_url,
        })

    advertised_count = data.get("count")
    if advertised_count != len(gifts):
        raise ValueError(
            f"Gift catalog is incomplete: advertises {advertised_count}, "
            f"contains {len(gifts)} rows."
        )
    updated_ms = data.get("updatedAt")
    if isinstance(updated_ms, bool) or not isinstance(updated_ms, (int, float)):
        raise ValueError("Gift catalog update timestamp is missing.")
    updated_at = datetime.fromtimestamp(updated_ms / 1000, timezone.utc).isoformat()
    return {"updated_at": updated_at, "count": len(gifts), "gifts": gifts}


def merge_catalog(
    current: dict[str, Any],
    source: dict[str, Any],
    *,
    retrieved_at: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Merge source labels/prices while preserving event names and diamond values."""
    merged = copy.deepcopy(current)
    gifts = merged.setdefault("gifts", {})
    if not isinstance(gifts, dict):
        raise ValueError("Existing catalog has an invalid gifts mapping.")

    added = matched = 0
    for item in source["gifts"]:
        gift_id = str(item["gift_id"])
        name = str(item["name"])
        coins = int(item["coins"])
        gift = gifts.get(gift_id)
        if gift is None:
            gift = {
                "gift_id": gift_id,
                "original_name": None,
                "original_names_seen": [],
                "name_en": None,
                "name_zh_display": None,
                "name_zh_tts": None,
                "diamond_count": None,
                "diamond_count_scope": None,
                "sources": {},
                "mapping_status": "needs_review",
                "pending_fields": ["name_en", "name_zh_display", "name_zh_tts"],
            }
            gifts[gift_id] = gift
            added += 1
        else:
            matched += 1

        sources = gift.setdefault("sources", {})
        sources["standout_catalog"] = {
            "kind": "public_catalog",
            "url": SOURCE_URL,
            "updated_at": source["updated_at"],
            "field_name": name,
            "coin_price": coins,
        }
        gift["standout_name"] = name
        gift["coin_price"] = coins
        gift["coin_price_source"] = SOURCE_URL
        gift["image_url"] = item.get("image_url")

        # The site's English catalog name can fill a missing English label, but
        # must never replace a name observed in a TikTok event.
        if not gift.get("name_en") and name.isascii():
            gift["name_en"] = name
            sources["name_en"] = {
                "kind": "public_catalog",
                "url": SOURCE_URL,
                "field": "gift.name",
            }

        pending = list(gift.get("pending_fields") or [])
        if gift.get("name_en"):
            pending = [field for field in pending if field != "name_en"]
        for field in ("name_zh_display", "name_zh_tts"):
            if not gift.get(field) and field not in pending:
                pending.append(field)
        gift["pending_fields"] = pending
        gift["mapping_status"] = "mapped" if not pending else "needs_review"

    merged["gifts"] = dict(sorted(gifts.items(), key=lambda pair: int(pair[0])))
    merged["updated_at"] = retrieved_at[:10]
    merged["scope"] = (
        "Gift IDs observed in saved Browser Network event sessions and listed in "
        "the STANDOUT Streamers public catalog. Only event observations populate "
        "original TikTok names and diamond counts."
    )
    merged["coin_price_note"] = (
        "coin_price is the public catalog's viewer purchase cost in TikTok Coins. "
        "It is not diamond_count; event-level diamond values remain authoritative."
    )
    merged.setdefault("external_catalogs", {})["standout_streamers"] = {
        "url": SOURCE_URL,
        "updated_at": source["updated_at"],
        "retrieved_at": retrieved_at,
        "listed_gift_count": source["count"],
    }
    return merged, {
        "source_ids": source["count"],
        "added_ids": added,
        "matched_existing_ids": matched,
        "total_ids": len(merged["gifts"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-html",
        type=Path,
        help="Import a previously saved page instead of fetching the public page.",
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    args = parser.parse_args()

    html = args.source_html.read_text(encoding="utf-8") if args.source_html else fetch_source_html()
    source = parse_source_html(html)
    current = json.loads(args.catalog.read_text(encoding="utf-8"))
    retrieved_at = datetime.now(timezone.utc).isoformat()
    merged, counts = merge_catalog(current, source, retrieved_at=retrieved_at)
    args.catalog.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "Imported {source_ids} source gifts; added {added_ids}, matched "
        "{matched_existing_ids}, catalog now has {total_ids} IDs.".format(**counts)
    )
    print(f"Source snapshot updated_at={source['updated_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
