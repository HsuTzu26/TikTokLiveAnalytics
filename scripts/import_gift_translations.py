"""Import a UTF-8 tab-separated Gift ID translation list into the catalog."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tts.gift_catalog_editor import merge_translation_rows


DEFAULT_INPUT = ROOT / "data" / "gift_catalog_zh_TW.txt"
DEFAULT_CATALOG = ROOT / "src" / "tts" / "gift_catalog.json"
REQUIRED_COLUMNS = {
    "Gift ID",
    "Gift Name",
    "Coin",
    "Image URL",
    "中文顯示名",
    "TTS 發音名",
}


def read_translation_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    tabular = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    reader = csv.DictReader(StringIO(tabular), delimiter="\t")
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
        raise ValueError(f"Translation file is missing columns: {', '.join(missing)}")
    return [dict(row) for row in reader if row.get("Gift ID")]


def write_catalog(path: Path, catalog: dict[str, Any]) -> None:
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = read_translation_rows(args.input)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        updated, count = merge_translation_rows(
            catalog,
            rows,
            updated_at=timestamp,
            source_file=str(args.input.resolve().relative_to(ROOT)),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not args.dry_run:
        write_catalog(args.catalog, updated)
    action = "Validated" if args.dry_run else "Imported"
    print(f"{action} {count} Gift ID translation rows from {args.input}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
