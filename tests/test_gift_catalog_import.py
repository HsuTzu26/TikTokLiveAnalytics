import json
import unittest

from scripts.update_standout_gift_catalog import merge_catalog, parse_source_html
from src.tts.gift_catalog_editor import (
    apply_translation_edits,
    build_editor_rows,
    catalog_statistics,
    merge_translation_rows,
    merge_observed_gifts,
)


class GiftCatalogImportTests(unittest.TestCase):
    def _page(self, *, count=2):
        data = {
            "updatedAt": 1790661553047,
            "count": count,
            "gifts": [
                {"id": 7, "name": "Rose", "coins": 1, "img": "https://cdn.example/7.webp"},
                {"id": 99, "name": "A New Gift", "coins": 10, "img": "https://cdn.example/99.webp"},
            ][:count],
        }
        return "<script>window.TTGL_DATA = " + json.dumps(data) + ";</script>"

    def test_parse_source_html_extracts_embedded_catalog(self):
        source = parse_source_html(self._page())
        self.assertEqual(source["count"], 2)
        self.assertEqual(source["gifts"][0], {
            "gift_id": "7",
            "name": "Rose",
            "coins": 1,
            "image_url": "https://cdn.example/7.webp",
        })
        self.assertTrue(source["updated_at"].startswith("2026-09-29T05:59:13"))

    def test_parser_rejects_incomplete_or_duplicate_catalog(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            parse_source_html(self._page(count=1).replace('"count": 1', '"count": 2'))

        duplicate_data = {
            "updatedAt": 1790661553047,
            "count": 2,
            "gifts": [
                {"id": 7, "name": "Rose", "coins": 1},
                {"id": 7, "name": "Rose again", "coins": 1},
            ],
        }
        with self.assertRaisesRegex(ValueError, "duplicate ID"):
            parse_source_html(
                "window.TTGL_DATA = " + json.dumps(duplicate_data) + ";"
            )

    def test_merge_preserves_event_values_and_separates_coin_price(self):
        existing = {
            "schema_version": 1,
            "gifts": {
                "7": {
                    "gift_id": "7",
                    "original_name": "玫瑰",
                    "original_names_seen": ["玫瑰"],
                    "name_en": None,
                    "name_zh_display": None,
                    "name_zh_tts": None,
                    "diamond_count": 5,
                    "diamond_count_scope": "observed_in_saved_live_sessions",
                    "sources": {
                        "original_name": {
                            "kind": "observed_event",
                            "field": "WebcastGiftMessage.gift.name",
                        }
                    },
                    "mapping_status": "needs_review",
                    "pending_fields": ["name_en"],
                }
            },
        }
        source = parse_source_html(self._page())
        merged, counts = merge_catalog(
            existing, source, retrieved_at="2026-09-29T12:00:00+00:00"
        )

        rose = merged["gifts"]["7"]
        self.assertEqual(rose["original_name"], "玫瑰")
        self.assertEqual(rose["diamond_count"], 5)
        self.assertEqual(rose["coin_price"], 1)
        self.assertEqual(rose["name_en"], "Rose")
        self.assertEqual(rose["pending_fields"], ["name_zh_display", "name_zh_tts"])

        added = merged["gifts"]["99"]
        self.assertIsNone(added["original_name"])
        self.assertIsNone(added["diamond_count"])
        self.assertEqual(added["standout_name"], "A New Gift")
        self.assertEqual(added["coin_price"], 10)
        self.assertEqual(counts, {
            "source_ids": 2,
            "added_ids": 1,
            "matched_existing_ids": 1,
            "total_ids": 2,
        })

    def test_translation_editor_applies_chinese_names_by_id_only(self):
        catalog = {
            "gifts": {
                "5655": {
                    "gift_id": "5655",
                    "original_name": "Rose",
                    "name_en": "Rose",
                    "name_zh_display": None,
                    "name_zh_tts": None,
                    "diamond_count": 1,
                    "coin_price": 1,
                    "sources": {"original_name": {"kind": "observed_event"}},
                    "pending_fields": ["name_zh_display", "name_zh_tts"],
                }
            }
        }
        updated, changed = apply_translation_edits(
            catalog,
            [{"Gift ID": "5655", "中文顯示名": "玫瑰花", "TTS 發音名": "玫瑰花"}],
            updated_at="2026-09-29T12:00:00+00:00",
        )
        gift = updated["gifts"]["5655"]
        self.assertEqual(changed, 1)
        self.assertEqual(gift["original_name"], "Rose")
        self.assertEqual(gift["diamond_count"], 1)
        self.assertEqual(gift["coin_price"], 1)
        self.assertEqual(gift["mapping_status"], "mapped")
        self.assertEqual(gift["sources"]["name_zh_tts"]["kind"], "manual_translation")
        self.assertEqual(catalog["gifts"]["5655"]["name_zh_tts"], None)

    def test_editor_views_separate_saved_pending_and_all_ids(self):
        catalog = {
            "gifts": {
                "1": {
                    "gift_id": "1",
                    "standout_name": "Rose",
                    "coin_price": 1,
                    "name_zh_display": "玫瑰花",
                    "name_zh_tts": "玫瑰花",
                    "sources": {"standout_catalog": {"kind": "public_catalog"}},
                },
                "2": {
                    "gift_id": "2",
                    "standout_name": "Rose",
                    "coin_price": 5,
                    "name_zh_display": None,
                    "name_zh_tts": None,
                    "sources": {"standout_catalog": {"kind": "public_catalog"}},
                },
                "3": {
                    "gift_id": "3",
                    "original_name": "本地實測禮物",
                    "name_zh_display": None,
                    "name_zh_tts": None,
                    "sources": {"original_name": {"kind": "observed_event"}},
                },
            }
        }
        self.assertEqual(len(build_editor_rows(catalog, view="已儲存")), 1)
        self.assertEqual(len(build_editor_rows(catalog, view="待補")), 2)
        self.assertEqual(
            len(build_editor_rows(catalog, view="全部", source_scope="網站清單")),
            2,
        )
        self.assertEqual(
            len(build_editor_rows(catalog, view="全部", source_scope="直播實測追加")),
            1,
        )
        all_rows = build_editor_rows(catalog, view="全部")
        self.assertEqual(len(all_rows), 3)
        self.assertEqual(len({row["Gift ID"] for row in all_rows}), 3)
        self.assertEqual(all_rows[-1]["資料來源"], "直播實測追加")
        self.assertEqual(catalog_statistics(catalog), {
            "total": 3,
            "saved": 1,
            "pending": 2,
            "website": 2,
            "observed": 1,
            "event_only": 1,
        })

    def test_new_event_gifts_are_added_by_id_without_overwriting_existing_translation(self):
        catalog = {
            "gifts": {
                "5655": {
                    "gift_id": "5655",
                    "original_name": "Rose",
                    "original_names_seen": ["Rose"],
                    "name_en": "Rose",
                    "name_zh_display": "玫瑰花",
                    "name_zh_tts": "玫瑰花",
                    "diamond_count": 1,
                    "coin_price": 1,
                    "sources": {"original_name": {"kind": "observed_event"}},
                    "mapping_status": "mapped",
                    "pending_fields": [],
                }
            }
        }
        observations = [
            {
                "gift_id": "5655", "raw_name": "Rose Flower",
                "diamond_count": 1, "session_id": "session-a",
            },
            {
                "gift_id": "999999", "raw_name": "New Gift",
                "diamond_count": 7, "session_id": "session-a",
            },
        ]
        merged, changed = merge_observed_gifts(
            catalog, observations, updated_at="2026-09-30T10:00:00+00:00"
        )
        self.assertEqual(changed, 2)
        self.assertEqual(merged["gifts"]["5655"]["name_zh_display"], "玫瑰花")
        self.assertEqual(merged["gifts"]["5655"]["original_names_seen"], ["Rose", "Rose Flower"])
        new_gift = merged["gifts"]["999999"]
        self.assertEqual(new_gift["original_name"], "New Gift")
        self.assertEqual(new_gift["coin_price"], 7)
        self.assertEqual(new_gift["mapping_status"], "needs_review")
        pending_rows = build_editor_rows(merged, view="待補")
        self.assertEqual({row["Gift ID"] for row in pending_rows}, {"999999"})

    def test_import_rows_verify_id_metadata_before_applying_translation(self):
        catalog = {
            "gifts": {
                "5655": {
                    "gift_id": "5655",
                    "standout_name": "Rose",
                    "image_url": "https://cdn.example/rose.webp",
                    "coin_price": 1,
                    "original_name": "Rose",
                    "name_en": "Rose",
                    "name_zh_display": None,
                    "name_zh_tts": None,
                    "diamond_count": 1,
                    "sources": {
                        "standout_catalog": {"kind": "public_catalog"},
                        "original_name": {"kind": "observed_event"},
                    },
                    "pending_fields": ["name_zh_display", "name_zh_tts"],
                }
            }
        }
        row = {
            "Gift ID": "5655",
            "Gift Name": "Rose",
            "Coin": "1",
            "Image URL": "https://cdn.example/rose.webp",
            "中文顯示名": "玫瑰花",
            "TTS 發音名": "玫瑰花",
        }
        updated, count = merge_translation_rows(
            catalog,
            [row],
            updated_at="2026-09-29T12:00:00+00:00",
            source_file="data/gift_catalog_zh_TW.txt",
        )
        gift = updated["gifts"]["5655"]
        self.assertEqual(count, 1)
        self.assertEqual(gift["name_zh_display"], "玫瑰花")
        self.assertEqual(gift["sources"]["name_zh_tts"]["source_file"], "data/gift_catalog_zh_TW.txt")
        self.assertEqual(gift["original_name"], "Rose")
        self.assertEqual(gift["diamond_count"], 1)

        mismatched = dict(row, **{"Gift Name": "Wrong gift"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            merge_translation_rows(
                catalog,
                [mismatched],
                updated_at="2026-09-29T12:00:00+00:00",
                source_file="data/gift_catalog_zh_TW.txt",
            )


if __name__ == "__main__":
    unittest.main()
