import json
import asyncio
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tts.pipeline import (
    ChatProcessor,
    PreparedChat,
    TTSSettings,
    clean_chat_text,
    dominant_language,
    member_entry_sound,
    should_speak_chat,
    spoken_gift_name,
)
from src import tts_worker
from src.tts.pipeline import speech_segments
from src.tts.audio import EdgeTTSBackend, WindowsSystemSpeechBackend
from src.tts.gift_catalog import OBSERVED_GIFTS, gift_metadata_for
from src.live_summary import (
    _gift_rows,
    _timeline_summary,
    _user_activity_summary,
    build_live_summary,
    render_live_summary,
)


class TTSChatPipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_mixed_chat_is_split_into_language_matched_speech_segments(self):
        processor = ChatProcessor(TTSSettings())
        prepared, reason = processor.prepare({"comment": "Hello 今天 good morning"})
        self.assertIsNone(reason)
        self.assertIsNotNone(prepared)
        self.assertEqual(
            prepared.segments,
            (
                ("Hello", "en-US-AriaNeural"),
                ("今天", "zh-TW-HsiaoChenNeural"),
                ("good morning", "en-US-AriaNeural"),
            ),
        )
        self.assertEqual(
            speech_segments("好! Hi?", zh_voice="zh", en_voice="en"),
            (("好!", "zh"), ("Hi?", "en")),
        )

    def test_common_emoji_are_spoken_and_unknown_emoji_can_still_be_ignored(self):
        self.assertEqual(clean_chat_text("太棒了👍", speak_common_emoji=True), "太棒了 讚")
        self.assertEqual(
            clean_chat_text("great😂", speak_common_emoji=True),
            "great 笑哭",
        )

    def test_tiktok_laugh_tokens_are_chinese_and_ascii_faces_are_ignored(self):
        self.assertEqual(
            clean_chat_text("[laugh]", speak_common_emoji=True),
            "笑翻",
        )
        self.assertEqual(
            clean_chat_text("好好笑[laugh]", speak_common_emoji=True),
            "好好笑 笑翻",
        )
        self.assertEqual(
            clean_chat_text("^_^ :) :D hello", speak_common_emoji=True),
            "hello",
        )

    def test_cleaning_removes_urls_emoji_and_limits_length(self):
        self.assertEqual(
            clean_chat_text("你好 😊 https://example.com nice", max_length=10),
            "你好 nice",
        )

    def test_unicode_decorations_are_skipped_and_bilingual_text_survives(self):
        styled_allen = "\U0001D4D0\U0001D4F5\U0001D4F5\U0001D4EE\U0001D4F7"
        text = clean_chat_text(
            f"@{styled_allen}\U0001FAE7 \u0f3a\u5c0f\u9b45 \u0f3b \u597d\u7684",
            speak_common_emoji=False,
        )

        self.assertEqual(text, "at Allen \u5c0f\u9b45 \u597d\u7684")
        segments = speech_segments(
            text,
            zh_voice="zh-TW-HsiaoChenNeural",
            en_voice="en-US-AriaNeural",
        )
        self.assertIn(("at Allen", "en-US-AriaNeural"), segments)
        self.assertIn(("\u5c0f\u9b45 \u597d\u7684", "zh-TW-HsiaoChenNeural"), segments)
        self.assertNotIn("\u0f3a", text)
        self.assertNotIn("\u0f3b", text)

    def test_latin_handle_with_kana_bridge_stays_one_english_segment(self):
        text = clean_chat_text(
            "@Chloe\U0001FAE7\u306eUU "
            "\u592a\u597d\u4e86\u4eca\u5929\u4e00\u5806\u4eba\u5e6b\u5fd9\u627f\u64d4"
        )
        self.assertEqual(
            text,
            "at Chloe UU \u592a\u597d\u4e86\u4eca\u5929\u4e00\u5806\u4eba\u5e6b\u5fd9\u627f\u64d4",
        )
        self.assertEqual(
            speech_segments(
                text,
                zh_voice="zh-TW-HsiaoChenNeural",
                en_voice="en-US-AriaNeural",
            ),
            (
                ("at Chloe UU", "en-US-AriaNeural"),
                ("\u592a\u597d\u4e86\u4eca\u5929\u4e00\u5806\u4eba\u5e6b\u5fd9\u627f\u64d4", "zh-TW-HsiaoChenNeural"),
            ),
        )

    def test_language_router_uses_dominant_script_for_whole_message(self):
        self.assertEqual(dominant_language("今天開台嗎"), "zh-TW")
        self.assertEqual(dominant_language("hello streamer"), "en-US")
        self.assertEqual(dominant_language("hi 今天"), "zh-TW")

    def test_user_cooldown_and_duplicate_window_are_enforced(self):
        processor = ChatProcessor(TTSSettings(
            user_cooldown_seconds=5,
            duplicate_window_seconds=10,
        ))
        first, reason = processor.prepare(
            {"comment": "hello", "unique_id": "viewer1"}, now=100
        )
        self.assertIsNotNone(first)
        self.assertIsNone(reason)

        second, reason = processor.prepare(
            {"comment": "another message", "unique_id": "viewer1"}, now=102
        )
        self.assertIsNone(second)
        self.assertEqual(reason, "user_cooldown")

        third, reason = processor.prepare(
            {"comment": "hello", "unique_id": "viewer2"}, now=106
        )
        self.assertIsNone(third)
        self.assertEqual(reason, "duplicate_text")

    def test_blacklist_blocks_matching_text(self):
        processor = ChatProcessor(TTSSettings(blacklist_terms=("badword",)))
        result, reason = processor.prepare({"comment": "BADWORD here"}, now=1)
        self.assertIsNone(result)
        self.assertEqual(reason, "moderation_blacklist")

    def test_settings_are_bounded_and_round_trip(self):
        settings = TTSSettings.from_mapping(
            {
                "volume": 200,
                "queue_size": 0,
                "rate_percent": -99,
                "min_request_interval_seconds": 0,
                "blacklist_terms": "a\nb",
            }
        )
        self.assertEqual(settings.volume, 100)
        self.assertEqual(settings.queue_size, 1)
        self.assertEqual(settings.rate_percent, -50)
        self.assertEqual(settings.min_request_interval_seconds, 0.1)
        self.assertEqual(settings.chat_ttl_seconds, 15.0)
        self.assertEqual(settings.blacklist_terms, ("a", "b"))
        self.assertEqual(TTSSettings.from_mapping({}).rate_percent, 35)
        self.assertEqual(TTSSettings.from_mapping(settings.to_mapping()), settings)
        self.assertEqual(
            TTSSettings.from_mapping({"max_queue_age_seconds": 12}).chat_ttl_seconds,
            12.0,
        )
        self.assertEqual(
            TTSSettings.from_mapping({"chat_ttl_seconds": 40}).chat_ttl_seconds,
            15.0,
        )

    def test_observed_gift_catalog_localizes_known_raw_names(self):
        self.assertEqual(len(OBSERVED_GIFTS), 78)
        self.assertEqual(spoken_gift_name("11919", "Pork Rice Bowl"), "滷肉飯")
        self.assertEqual(spoken_gift_name("unknown", "Pork Rice Bowl"), "滷肉飯")
        self.assertEqual(spoken_gift_name("6784", "Cake Slice"), "蛋糕切片")
        self.assertEqual(spoken_gift_name("1359368", "Sunset Cheer"), "日落歡呼")
        self.assertEqual(spoken_gift_name("unknown", "Sunset Cheer"), "日落歡呼")
        self.assertEqual(spoken_gift_name("unknown", "Unlisted Gift"), "Unlisted Gift")

    def test_gift_catalog_keeps_raw_display_and_tts_names_separate(self):
        gift = gift_metadata_for("5655", "Rose")
        self.assertEqual(gift["original_name"], "Rose")
        self.assertEqual(gift["name_en"], "Rose")
        self.assertEqual(gift["name_zh_display"], "玫瑰花")
        self.assertEqual(gift["name_zh_tts"], "玫瑰花")
        self.assertEqual(gift["diamond_count"], 1)
        self.assertIn("diamond_count", gift["sources"])
        self.assertEqual(gift["mapping_status"], "mapped")

        exact_raw = " Rose 🌹 "
        unknown = gift_metadata_for("99999999", exact_raw)
        self.assertEqual(unknown["original_name"], exact_raw)
        self.assertEqual(unknown["mapping_status"], "needs_review")
        self.assertIn("name_zh_tts", unknown["pending_fields"])
        self.assertEqual(spoken_gift_name("99999999", exact_raw), "Rose 🌹")

    def test_mixed_name_and_emoji_cleanup_stays_in_speech_preparation(self):
        event = {"comment": "Rose 🎉 玫瑰 [laugh]"}
        prepared, reason = ChatProcessor(TTSSettings()).prepare(event)
        self.assertIsNone(reason)
        self.assertIsNotNone(prepared)
        self.assertEqual(event["comment"], "Rose 🎉 玫瑰 [laugh]")
        self.assertEqual(prepared.text, "Rose 恭喜 玫瑰 笑翻")
        self.assertEqual(
            [voice for _, voice in prepared.segments],
            ["en-US-AriaNeural", "zh-TW-HsiaoChenNeural"],
        )

    def test_pathological_language_switches_use_one_bounded_speech_segment(self):
        processor = ChatProcessor(TTSSettings())
        text = "a中b中c中d中e"
        prepared, reason = processor.prepare({"comment": text})
        self.assertIsNone(reason)
        self.assertEqual(len(prepared.segments), 1)
        self.assertEqual(prepared.segments[0][0], text)
        self.assertTrue(prepared.speech_segmentation_fallback)

    def test_gift_delivery_log_keeps_raw_name_and_stage_timings_as_utf8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            message = PreparedChat(
                user_key=None,
                text="Gift sender 送出玫瑰花 1 個",
                voice="zh-TW-HsiaoChenNeural",
                received_at_ms=1_800_000_000_000,
                event_id="event-raw-name",
                gift_id="5655",
                source_session_dir=temp_dir,
                session_id="session-test",
                gift_name="Rose ",
                gift_name_original="Rose ",
                gift_name_en="Rose",
                gift_name_zh_display="玫瑰花",
                gift_name_zh_tts="玫瑰花",
                gift_catalog_status="mapped",
                gift_name_resolution_ms=0.12,
                speech_preparation_ms=0.34,
                processing_timings_ms=(
                    ("protobuf_envelope_ms", 1.2),
                    ("protobuf_event_ms", 0.7),
                    ("event_normalization_ms", 0.5),
                    ("gift_name_resolution_ms", 0.01),
                ),
                gift_quantity=1,
                gift_diamond_total=1,
            )
            tts_worker.TTSWorker._log_gift_stage(
                message, "synthesis_completed", duration_ms=14.25
            )
            row = json.loads(
                (Path(temp_dir) / "tts_delivery.ndjson").read_text(encoding="utf-8")
            )
            self.assertEqual(row["gift_name_original"], "Rose ")
            self.assertEqual(row["gift_name_zh_tts"], "玫瑰花")
            self.assertEqual(row["stage_timings_ms"]["protobuf_event"], 0.7)
            self.assertEqual(row["duration_ms"], 14.25)

    def test_session_gift_table_shows_bilingual_and_unmapped_original_names(self):
        rows, counted, skipped = _gift_rows([
            {"type": "gift", "gift_id": "5655", "gift_name": "Rose", "diamond_total": 1},
            {
                "type": "gift",
                "gift_id": "99999999",
                "gift_name": "Unknown Mixed 🌹",
                "diamond_total": 4,
            },
        ])
        self.assertEqual((counted, skipped), (2, 0))
        known = next(row for row in rows if row["gift_id"] == "5655")
        unknown = next(row for row in rows if row["gift_id"] == "99999999")
        self.assertEqual(known["display_name"], "玫瑰花（Rose）")
        self.assertEqual(unknown["raw_name"], "Unknown Mixed 🌹")
        self.assertIn("Unknown Mixed 🌹", unknown["display_name"])
        self.assertEqual(unknown["catalog_status"], "needs_review")

    def test_summary_aggregates_by_gift_id_and_itemizes_each_gifter_and_likes(self):
        events = [
            {
                "type": "gift", "gift_id": "5655", "gift_name": "Rose",
                "gift_name_original": "Rose", "repeat_count": 1, "diamond_count": 1,
                "diamond_total": 1, "unique_id": "viewer1", "nickname": "Viewer One",
                "counted": True, "timestamp_ms": 1000,
            },
            {
                "type": "gift", "gift_id": "5655", "gift_name": "Rose",
                "gift_name_original": "Rose ", "repeat_count": 2, "diamond_count": 1,
                "diamond_total": 2, "unique_id": "viewer1", "nickname": "Viewer One",
                "counted": True, "timestamp_ms": 2000,
            },
            {"type": "viewer", "viewer_count": 40, "timestamp_ms": 1000},
            {"type": "viewer", "viewer_count": 55, "timestamp_ms": 2000},
            {"type": "like", "like_count": 5, "total_likes": 100, "timestamp_ms": 1000},
            {"type": "like", "like_count": 3, "total_likes": 103, "timestamp_ms": 2000},
        ]
        gifts, counted, skipped = _gift_rows(events)
        self.assertEqual((counted, skipped), (2, 0))
        self.assertEqual(len(gifts), 1)
        self.assertEqual(gifts[0]["quantity"], 3)
        self.assertEqual(gifts[0]["diamonds"], 3)
        self.assertEqual(gifts[0]["raw_names"], ["Rose", "Rose "])

        users = _user_activity_summary(events)
        self.assertEqual(len(users["gifters"]), 1)
        self.assertEqual(users["gifters"][0]["gifts"][0]["gift_id"], "5655")
        self.assertEqual(users["gifters"][0]["gifts"][0]["quantity"], 3)
        timeline = _timeline_summary(events)
        self.assertEqual(sum(row["likes_received"] for row in timeline["phases"]), 8)
        self.assertEqual(timeline["phases"][-1]["like_total_end"], 103)

    def test_end_report_uses_session_tts_snapshot_and_shows_sender_gift_like_trends(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "session_test_streamer"
            session_dir.mkdir()
            (session_dir / "session.json").write_text(json.dumps({
                "session_id": session_dir.name,
                "username": "test_streamer",
                "status": "stopped",
            }), encoding="utf-8")
            events = [
                {
                    "type": "gift", "msg_id": "g1", "gift_id": "5655",
                    "gift_name": "Rose", "gift_name_original": "Rose",
                    "unique_id": "viewer1", "nickname": "Viewer One",
                    "repeat_count": 2, "diamond_count": 1, "diamond_total": 2,
                    "counted": True, "timestamp_ms": 1000,
                },
                {"type": "viewer", "viewer_count": 25, "total_user_count": 500, "timestamp_ms": 1000},
                {"type": "like", "like_count": 4, "total_likes": 200, "timestamp_ms": 1000},
            ]
            (session_dir / "events.ndjson").write_text(
                "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
            )
            (session_dir / "gift_catalog_pending.ndjson").write_text(json.dumps({
                "gift_id": "999999", "raw_name": "New gift",
            }) + "\n", encoding="utf-8")
            (session_dir / "tts_summary.json").write_text(json.dumps({
                "session_id": session_dir.name,
                "username": "test_streamer",
                "status": "stopped",
                "metrics": {
                    "gift_spoken": 1,
                    "gift_delivery_failures": 0,
                    "max_queue_depth": 3,
                    "queue_capacity": 500,
                    "p95_event_to_playback_ms": 2200,
                },
            }), encoding="utf-8")
            global_state = Path(temp_dir) / "state.json"
            global_state.write_text(json.dumps({
                "username": "another_streamer", "metrics": {"gift_spoken": 99},
            }), encoding="utf-8")
            with patch("src.live_summary.TTS_STATE_PATH", global_state):
                summary = build_live_summary(session_dir)
            self.assertEqual(summary["tts"]["metrics"]["gift_spoken"], 1)
            self.assertEqual(summary["events"]["gift_diamonds"], 2)
            self.assertEqual(summary["events"]["last_room_user_count"], 500)
            self.assertEqual(summary["events"]["likes_received"], 4)
            self.assertEqual(summary["users"]["gifters"][0]["gifts"][0]["quantity"], 2)
            self.assertEqual(summary["gift_catalog_pending"][0]["original_name"], "New gift")
            report = render_live_summary(summary)
            self.assertIn("每位送禮者的禮物明細", report)
            self.assertIn("人數、禮物與 Like 趨勢", report)
            self.assertIn("P95 2,200 ms", report)

    def test_gift_sound_rules_round_trip_by_gift_id(self):
        settings = TTSSettings.from_mapping({
            "gift_followup_audio_path": "sounds/default.wav",
            "gift_sound_by_id": {"5655": "sounds/rose.wav"},
            "gift_name_by_id": {"5655": "玫瑰花"},
        })
        self.assertEqual(settings.gift_sound_by_id, (("5655", "sounds/rose.wav"),))
        self.assertEqual(settings.gift_name_by_id, (("5655", "玫瑰花"),))
        self.assertEqual(
            TTSSettings.from_mapping(settings.to_mapping()),
            settings,
        )
        self.assertEqual(spoken_gift_name("5655", "Rose"), "玫瑰花")
        self.assertEqual(
            spoken_gift_name("5655", "Rose", (("5655", "玫瑰"),)),
            "玫瑰",
        )
        self.assertEqual(spoken_gift_name("7934", "Heart Me"), "愛心")
        self.assertEqual(spoken_gift_name("5487", "Finger Heart"), "比心")
        self.assertEqual(spoken_gift_name("5897", "Swan"), "天鵝")
        self.assertEqual(spoken_gift_name("13651", "Popular Vote"), "人氣投票")
        self.assertEqual(spoken_gift_name("13460", "Mini star"), "小星星")
        self.assertEqual(spoken_gift_name("13352", "Rose Carriage"), "\u73ab\u7470\u99ac\u8eca")
        self.assertEqual(spoken_gift_name("1241064", "Ray Serenade"), "\u9b5f\u9b5a\u5c0f\u591c\u66f2")
        self.assertEqual(spoken_gift_name("8913", "Rosa"), "\u7f85\u838e")
        self.assertEqual(spoken_gift_name("5660", "Hand Heart"), "手比愛心")
        self.assertEqual(spoken_gift_name("6267", "Corgi"), "柯基犬")
        self.assertEqual(spoken_gift_name("6788", "Glow Stick"), "螢光棒")
        self.assertEqual(spoken_gift_name("9500", "Flying Jets"), "飛行噴射機")
        self.assertEqual(spoken_gift_name("15232", "You're awesome"), "你真棒")
        self.assertEqual(spoken_gift_name("16478", "Bubble Headphones"), "泡泡耳機")
        self.assertEqual(spoken_gift_name("17085", "Music Album"), "音樂專輯")

    def test_chat_filter_defaults_to_everyone_and_supports_fan_club_or_allowlist(self):
        default = TTSSettings()
        self.assertEqual(default.chat_tts_filter, "all")
        self.assertTrue(should_speak_chat({"unique_id": "viewer"}, default))

        fan_settings = TTSSettings.from_mapping({"chat_tts_filter": "fan_club_only"})
        self.assertTrue(should_speak_chat({"fan_club_level": 2}, fan_settings))
        self.assertFalse(should_speak_chat({"fan_club_level": None}, fan_settings))
        self.assertFalse(should_speak_chat({"unique_id": "viewer"}, fan_settings))

        selected = TTSSettings.from_mapping({
            "chat_tts_filter": "selected_users",
            "chat_tts_user_allowlist": ["@viewer", "123"],
        })
        self.assertEqual(selected.chat_tts_user_allowlist, ("viewer", "123"))
        self.assertTrue(should_speak_chat({"unique_id": "VIEWER"}, selected))
        self.assertTrue(should_speak_chat({"user_id": "123"}, selected))
        self.assertFalse(should_speak_chat({"unique_id": "other"}, selected))
        self.assertEqual(TTSSettings.from_mapping(selected.to_mapping()), selected)

    def test_member_entry_sound_prefers_user_then_fan_level_then_general(self):
        settings = TTSSettings.from_mapping({
            "member_entry_sound_path": "sounds/entry.wav",
            "member_entry_sound_by_user": {"VIPUser": "sounds/vip.wav"},
            "fan_club_entry_sound_path": "sounds/iron-fan.wav",
            "fan_club_entry_min_level": 10,
        })
        self.assertEqual(
            member_entry_sound({"action_code": 1, "unique_id": "vipuser", "fan_club_level": 12}, settings),
            "sounds/vip.wav",
        )
        self.assertEqual(
            member_entry_sound({"action_code": 1, "fan_club_level": 10}, settings),
            "sounds/iron-fan.wav",
        )
        self.assertEqual(
            member_entry_sound({"action_code": 1, "fan_club_level": 2}, settings),
            "sounds/entry.wav",
        )
        self.assertIsNone(member_entry_sound({"action_code": 2, "fan_club_level": 12}, settings))

    def test_state_json_write_retries_transient_windows_file_lock(self):
        real_replace = tts_worker.os.replace
        attempts = []

        def replace_after_transient_lock(source, target):
            attempts.append((source, target))
            if len(attempts) < 3:
                raise PermissionError("temporary sharing violation")
            return real_replace(source, target)

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "state.json"
            with patch.object(
                tts_worker.os, "replace", side_effect=replace_after_transient_lock
            ), patch.object(tts_worker.time, "sleep"):
                tts_worker.write_json(destination, {"status": "watching"})

            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {
                "status": "watching"
            })
            self.assertEqual(len(attempts), 3)

    async def test_ndjson_chat_is_enqueued_and_full_queue_backpressures_reader(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            settings = TTSSettings(
                queue_size=1,
                user_cooldown_seconds=0,
                duplicate_window_seconds=0,
            )
            config_path.write_text(json.dumps(settings.to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")

            for comment, user in (("hello", "viewer1"), ("world", "viewer2")):
                event = {
                    "type": "chat",
                    "comment": comment,
                    "unique_id": user,
                    "received_at_ms": 1_800_000_000_000,
                }
                if comment == "hello":
                    await worker._process_line(json.dumps(event).encode("utf-8"))
                else:
                    blocked_put = asyncio.create_task(
                        worker._process_line(json.dumps(event).encode("utf-8"))
                    )
                    await asyncio.sleep(0)
                    self.assertFalse(blocked_put.done())

            self.assertEqual(worker.metrics["chat_events_seen"], 2)
            self.assertEqual(worker.queue.qsize(), 1)
            _, _, queued_message, _, _, _ = worker.queue.get_nowait()
            worker.queue.task_done()
            await blocked_put
            self.assertEqual(queued_message.text, "hello")
            self.assertEqual(worker.queue.qsize(), 1)
            _, _, queued_message, _, _, _ = worker.queue.get_nowait()
            worker.queue.task_done()
            self.assertEqual(queued_message.text, "world")

    async def test_sender_filter_affects_chat_but_not_gifts_and_entry_sound_uses_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(TTSSettings.from_mapping({
                "chat_tts_filter": "fan_club_only",
                "member_entry_sound_path": "sounds/entry.wav",
            }).to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")

            await worker._process_line(json.dumps({
                "type": "chat", "unique_id": "regular", "comment": "hello",
            }).encode("utf-8"))
            await worker._process_line(json.dumps({
                "type": "chat", "unique_id": "fan", "fan_club_level": 1,
                "comment": "welcome",
            }).encode("utf-8"))
            await worker._process_line(json.dumps({
                "type": "gift", "unique_id": "regular", "gift_id": "5655",
                "gift_name": "Rose", "counted": True,
            }).encode("utf-8"))
            await worker._process_line(json.dumps({
                "type": "member", "action_code": 1, "unique_id": "visitor",
                "received_at_ms": int(time.time() * 1000),
            }).encode("utf-8"))

            queued = []
            while not worker.queue.empty():
                item = worker.queue.get_nowait()
                queued.append((item[-1], item[2]))
                worker.queue.task_done()
            self.assertEqual(worker.metrics["chat_filtered_by_settings"], 1)
            self.assertEqual([event_type for event_type, _ in queued], [
                "chat", "gift", "member_sound",
            ])
            self.assertEqual(queued[-1][1].followup_audio_path, "sounds/entry.wav")

    async def test_mid_session_attach_skips_existing_chat_backlog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings(
                gift_name_by_id=(("5655", "玫瑰花"),),
            ).to_mapping()), encoding="utf-8")
            session_dir = root / "session"
            session_dir.mkdir()
            event_path = session_dir / "events.ndjson"
            old_event = {"type": "chat", "comment": "old chat", "unique_id": "old"}
            event_path.write_text(json.dumps(old_event) + "\n", encoding="utf-8")

            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            worker.first_session = True
            worker._attach(session_dir)
            worker._tail_position = worker.file_handle.tell()

            new_event = {"type": "chat", "comment": "new chat", "unique_id": "new"}
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(new_event) + "\n")
            await worker._read_new_events()

            self.assertEqual(worker.metrics["chat_events_seen"], 1)
            _, _, queued_message, _, _, _ = worker.queue.get_nowait()
            worker.queue.task_done()
            self.assertEqual(queued_message.text, "new chat")
            worker._close_tail()

    async def test_chat_ttl_does_not_expire_captured_gifts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")

            old_event_time = time.monotonic() - 60
            self.assertTrue(worker._event_is_stale("chat", old_event_time))
            self.assertFalse(worker._event_is_stale("gift", old_event_time))

    async def test_browser_provider_session_feeds_existing_tts_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            session_dir = root / "browser_ws_probe" / "session_test_streamer"
            session_dir.mkdir(parents=True)
            (session_dir / "events.ndjson").write_text("", encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path), patch.object(
                tts_worker, "BROWSER_SESSION_ROOT", root / "browser_ws_probe"
            ):
                worker = tts_worker.TTSWorker(
                    "test_streamer", browser_session_dir=session_dir
                )

            self.assertEqual(worker.source_session_dir, session_dir.resolve())
            chat = {
                "type": "chat",
                "comment": "natural browser chat",
                "unique_id": "viewer_42",
                "received_at_ms": int(time.time() * 1000),
            }
            await worker._process_line(json.dumps(chat).encode("utf-8"))
            gift = {
                "type": "gift",
                "nickname": "Gift sender",
                "gift_id": "5655",
                "gift_name": "Rose",
                "repeat_count": 3,
                "counted": True,
                "received_at_ms": chat["received_at_ms"] + 1,
            }
            await worker._process_line(json.dumps(gift).encode("utf-8"))

            self.assertEqual(worker.metrics["chat_events_seen"], 1)
            self.assertEqual(worker.metrics["chat_queued"], 1)
            self.assertEqual(worker.metrics["gift_events_seen"], 1)
            self.assertEqual(worker.metrics["gift_queued"], 1)
            self.assertEqual(len(worker.metrics["event_to_queue_latencies_ms"]), 2)
            _, _, message, _, _, event_type = worker.queue.get_nowait()
            worker.queue.task_done()
            self.assertEqual(event_type, "chat")
            self.assertEqual(message.text, "natural browser chat")
            _, _, message, _, _, event_type = worker.queue.get_nowait()
            worker.queue.task_done()
            self.assertEqual(event_type, "gift")
            self.assertTrue(message.text.startswith("Gift sender"))
            self.assertIn("玫瑰花", message.text)
            self.assertNotIn("Rose", message.text)
            self.assertIn("3", message.text)

    async def test_handoff_replays_only_unspoken_counted_gifts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            browser_root = root / "browser_ws_probe"
            session_dir = browser_root / "session_test_streamer"
            session_dir.mkdir(parents=True)
            since_ms = int(time.time() * 1000) - 10_000
            events = [
                {"type": "gift", "gift_id": "5655", "gift_name": "Rose",
                 "received_at_ms": since_ms + 1, "counted": True},
                {"type": "gift", "gift_id": "5655", "gift_name": "Rose",
                 "received_at_ms": since_ms + 2, "counted": True},
                {"type": "gift", "gift_id": "7934", "gift_name": "Heart Me",
                 "received_at_ms": since_ms + 3, "counted": False},
                {"type": "gift", "gift_id": "7934", "gift_name": "Heart Me",
                 "received_at_ms": since_ms + 4, "counted": True},
            ]
            (session_dir / "events.ndjson").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            handoff_path = root / "replay_handoff.json"
            handoff_path.write_text(json.dumps({
                "username": "test_streamer",
                "session_dir": str(session_dir),
                "replay_since_ms": since_ms,
                "terminal_gift_count": 2,
            }), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path), patch.object(
                tts_worker, "BROWSER_SESSION_ROOT", browser_root
            ), patch.object(tts_worker, "REPLAY_HANDOFF_PATH", handoff_path):
                worker = tts_worker.TTSWorker(
                    "test_streamer", browser_session_dir=session_dir
                )
                replayed = await worker._replay_handoff_gifts()

            self.assertEqual(replayed, 1)
            self.assertFalse(handoff_path.exists())
            self.assertEqual(worker.metrics["gift_queued"], 1)
            _, _, message, _, _, event_type = worker.queue.get_nowait()
            worker.queue.task_done()
            self.assertEqual(event_type, "gift")
            self.assertIn(spoken_gift_name("7934", "Heart Me"), message.text)

    async def test_edge_tts_uses_bounded_receive_timeout(self):
        timeout_values = []

        class FakeCommunicate:
            def __init__(self, **kwargs):
                timeout_values.append(kwargs["receive_timeout"])

            async def save(self, destination):
                Path(destination).write_bytes(b"fake audio")

        with patch.dict("sys.modules", {"edge_tts": SimpleNamespace(Communicate=FakeCommunicate)}):
            audio_path = await EdgeTTSBackend().synthesize(
                "test", "zh-TW-HsiaoChenNeural", 5
            )
        try:
            self.assertEqual(timeout_values, [10])
        finally:
            audio_path.unlink(missing_ok=True)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows SAPI fallback")
    async def test_windows_system_speech_fallback_produces_wav_from_safe_stdin_payload(self):
        async def fake_create_subprocess_exec(*_args, **_kwargs):
            class FakeProcess:
                returncode = 0

                async def communicate(self, _payload):
                    self.request = json.loads(
                        __import__("base64").b64decode(_payload).decode("utf-8")
                    )
                    with wave.open(self.request["output"], "wb") as output:
                        output.setnchannels(1)
                        output.setsampwidth(2)
                        output.setframerate(22050)
                        output.writeframes(b"\x00\x00" * 220)
                    return b"OK\n", b""

                async def wait(self):
                    return 0

                def kill(self):
                    return None

            return FakeProcess()

        with patch("src.tts.audio.shutil.which", return_value="powershell.exe"), patch(
            "src.tts.audio.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            audio_path = await WindowsSystemSpeechBackend().synthesize(
                "測試 gift", "zh-TW-HsiaoChenNeural", 5
            )
        try:
            self.assertTrue(audio_path.is_file())
            with wave.open(str(audio_path), "rb") as audio_file:
                self.assertEqual(audio_file.getnframes(), 220)
        finally:
            audio_path.unlink(missing_ok=True)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows SAPI fallback")
    async def test_windows_system_speech_failure_caches_unavailable_voice_probe(self):
        calls = 0

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            nonlocal calls
            calls += 1

            class FailedProcess:
                returncode = 1

                async def communicate(self, _payload):
                    return b"", b"no matching offline voice"

            return FailedProcess()

        backend = WindowsSystemSpeechBackend(retry_probe_after_seconds=60)
        with patch("src.tts.audio.shutil.which", return_value="powershell.exe"), patch(
            "src.tts.audio.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await backend.synthesize("test", "zh-TW", 0)
        self.assertEqual(calls, 1)

    async def test_edge_synthesis_failure_uses_offline_gift_speech_when_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = root / "session_test_streamer"
            session_dir.mkdir()
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            worker.source_session_dir = session_dir
            worker.session_path = session_dir

            class NoAudioBackend:
                def __init__(self):
                    self.calls = 0

                async def synthesize(self, *_args):
                    self.calls += 1
                    raise RuntimeError("synthetic Edge failure")

            class OfflineBackend:
                def __init__(self):
                    self.calls = 0

                async def synthesize(self, *_args):
                    self.calls += 1
                    path = root / "fallback.wav"
                    path.write_bytes(b"RIFF" + b"\0" * 40)
                    return path

            class FakePlayer:
                async def play(self, _path, _volume):
                    return None

            edge_backend = NoAudioBackend()
            offline_backend = OfflineBackend()
            worker.backend = edge_backend
            worker.offline_backend = offline_backend
            worker.player = FakePlayer()
            event = {
                "type": "gift", "msg_id": "offline-gift-1", "gift_id": "5655",
                "nickname": "Viewer", "gift_name": "Rose", "repeat_count": 1,
                "counted": True, "received_at_ms": int(time.time() * 1000),
            }
            await worker._process_line(json.dumps(event).encode("utf-8"))
            with patch.object(tts_worker, "TTS_RETRY_BACKOFF_SECONDS", (0, 0, 0)):
                announcement = await worker._prepare_next_announcement()
            self.assertIsNotNone(announcement)
            self.assertEqual(edge_backend.calls, 1)
            self.assertEqual(offline_backend.calls, 1)
            self.assertEqual(worker.metrics["gift_retry_queued"], 0)
            self.assertEqual(worker.metrics["gift_offline_fallback_succeeded"], 1)
            await worker._deliver_prepared_announcement(announcement)
            self.assertEqual(worker.metrics["gift_spoken"], 1)
            self.assertIn("offline-gift-1", worker.completed_gift_ids)
            stages = [
                json.loads(line)["stage"]
                for line in (session_dir / "tts_delivery.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("offline_fallback_synthesized", stages)
            self.assertIn("playback_completed", stages)

    async def test_edge_synthesis_failure_uses_offline_chat_speech_when_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = root / "session_test_streamer"
            session_dir.mkdir()
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            worker.source_session_dir = session_dir
            worker.session_path = session_dir

            class NoAudioBackend:
                async def synthesize(self, *_args):
                    raise RuntimeError("synthetic Edge failure")

            class OfflineBackend:
                def __init__(self):
                    self.calls = []

                async def synthesize(self, text, voice, _rate):
                    path = root / f"chat-fallback-{len(self.calls)}.wav"
                    path.write_bytes(b"RIFF" + b"\0" * 40)
                    self.calls.append((text, voice))
                    return path

            class FakePlayer:
                async def play(self, _path, _volume):
                    return None

            offline = OfflineBackend()
            worker.backend = NoAudioBackend()
            worker.offline_backend = offline
            worker.player = FakePlayer()
            event = {
                "type": "chat",
                "msg_id": "offline-chat-1",
                "unique_id": "viewer1",
                "comment": "Hello \u4f60\u597d",
                "received_at_ms": int(time.time() * 1000),
            }
            await worker._process_line(json.dumps(event).encode("utf-8"))

            announcement = await worker._prepare_next_announcement()
            self.assertIsNotNone(announcement)
            self.assertEqual(worker.metrics["chat_offline_fallback_succeeded"], 1)
            self.assertEqual(
                [voice for _text, voice in offline.calls],
                ["en-US-AriaNeural", "zh-TW-HsiaoChenNeural"],
            )
            await worker._deliver_prepared_announcement(announcement)
            self.assertEqual(worker.metrics["chat_spoken"], 1)

            stages = [
                json.loads(line)["stage"]
                for line in (session_dir / "chat_tts_delivery.ndjson")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn("synthesis_attempt_failed", stages)
            self.assertIn("offline_fallback_synthesized", stages)
            self.assertIn("playback_completed", stages)

    async def test_failed_gift_gets_one_delayed_retry_before_delivery_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")

            session_dir = root / "session_test_streamer"
            session_dir.mkdir()
            handoff_path = root / "replay_handoff.json"
            worker.source_session_dir = session_dir

            class FakePlayer:
                def __init__(self):
                    self.alert_count = 0

                def play_failure_alert(self, volume):
                    self.alert_count += 1
                    return True

            fallback_player = FakePlayer()
            worker.player = fallback_player

            class NoAudioBackend:
                def __init__(self):
                    self.calls = 0

                async def synthesize(self, text, voice, rate_percent):
                    self.calls += 1
                    raise RuntimeError("synthetic no-audio response")

            class NoOfflineBackend:
                def __init__(self):
                    self.calls = 0

                async def synthesize(self, *_args):
                    self.calls += 1
                    raise RuntimeError("synthetic offline failure")

            edge_backend = NoAudioBackend()
            offline_backend = NoOfflineBackend()
            worker.backend = edge_backend
            worker.offline_backend = offline_backend
            gift = {
                "type": "gift", "msg_id": "gift-retry-1", "gift_id": "5655",
                "nickname": "Gift sender", "gift_name": "Rose", "repeat_count": 1,
                "counted": True, "received_at_ms": int(time.time() * 1000),
            }
            (session_dir / "events.ndjson").write_text(
                json.dumps(gift) + "\n", encoding="utf-8"
            )
            await worker._process_line(json.dumps(gift).encode("utf-8"))

            with patch.object(tts_worker, "TTS_RETRY_BACKOFF_SECONDS", (0, 0, 0)), patch.object(
                tts_worker, "GIFT_DELIVERY_RETRY_DELAY_SECONDS", 0
            ), patch.object(tts_worker, "REPLAY_HANDOFF_PATH", handoff_path):
                await worker._prepare_next_announcement()
                self.assertEqual(worker.metrics["gift_delivery_failures"], 0)
                self.assertEqual(worker.metrics["gift_retry_queued"], 1)
                self.assertEqual(edge_backend.calls, 3)
                self.assertEqual(offline_backend.calls, 1)
                retry = worker.queue.get_nowait()
                self.assertEqual(retry[2].retry_attempts, 1)
                worker.queue.task_done()
                worker.pending_queue_times.pop(retry[1], None)

                # Put the retry back so the worker path can finish its queue accounting.
                worker.queue.put_nowait(retry)
                worker.pending_queue_times[retry[1]] = time.monotonic()
                await worker._prepare_next_announcement()

            self.assertEqual(worker.metrics["gift_delivery_failures"], 1)
            self.assertEqual(worker.metrics["gift_synthesis_failures"], 1)
            self.assertEqual(edge_backend.calls, 6)
            self.assertEqual(offline_backend.calls, 2)
            self.assertEqual(fallback_player.alert_count, 1)
            self.assertEqual(worker.metrics["gift_failure_alerts_played"], 1)
            self.assertIn("gift-retry-1", worker.pending_gift_replay_ids)
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            self.assertEqual(handoff["pending_msg_ids"], ["gift-retry-1"])
            self.assertEqual(worker.queue.qsize(), 0)

    async def test_stopping_with_a_queued_gift_records_delivery_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            gift = {
                "type": "gift", "nickname": "Gift sender", "gift_name": "Rose",
                "repeat_count": 1, "counted": True,
                "received_at_ms": int(time.time() * 1000),
            }
            await worker._process_line(json.dumps(gift).encode("utf-8"))
            worker._record_worker_stopped_queue()

            self.assertEqual(worker.metrics["gift_delivery_failures"], 1)
            self.assertEqual(worker.metrics["skipped"]["worker_stopped"], 1)

    async def test_stopped_gifts_are_handed_off_by_message_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            browser_root = root / "browser_ws_probe"
            session_dir = browser_root / "session_test_streamer"
            session_dir.mkdir(parents=True)
            event_path = session_dir / "events.ndjson"
            event_path.write_text("", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            handoff_path = root / "replay_handoff.json"
            with patch.object(tts_worker, "CONFIG_PATH", config_path), patch.object(
                tts_worker, "BROWSER_SESSION_ROOT", browser_root
            ), patch.object(tts_worker, "REPLAY_HANDOFF_PATH", handoff_path):
                worker = tts_worker.TTSWorker(
                    "test_streamer", browser_session_dir=session_dir
                )
                gift = {
                    "type": "gift", "msg_id": "pending-gift-1",
                    "nickname": "Gift sender", "gift_id": "5655",
                    "gift_name": "Rose", "repeat_count": 1, "counted": True,
                    "received_at_ms": int(time.time() * 1000),
                }
                event_path.write_text(json.dumps(gift) + "\n", encoding="utf-8")
                await worker._process_line(json.dumps(gift).encode("utf-8"))
                worker._record_worker_stopped_queue()
                worker._write_gift_replay_handoff()

                handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
                self.assertEqual(handoff["pending_msg_ids"], ["pending-gift-1"])
                restarted = tts_worker.TTSWorker(
                    "test_streamer", browser_session_dir=session_dir
                )
                replayed = await restarted._replay_handoff_gifts()
                self.assertEqual(replayed, 1)
                self.assertFalse(handoff_path.exists())
                item = restarted.queue.get_nowait()
                restarted.queue.task_done()
                self.assertEqual(item[2].event_id, "pending-gift-1")

    async def test_unclean_restart_resumes_unfinished_gifts_from_session_cursor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            browser_root = root / "browser_ws_probe"
            session_dir = browser_root / "session_test_streamer"
            session_dir.mkdir(parents=True)
            event = {
                "type": "gift", "msg_id": "crashed-gift-1", "gift_id": "5655",
                "nickname": "Viewer", "gift_name": "Rose", "repeat_count": 1,
                "counted": True, "received_at_ms": int(time.time() * 1000),
            }
            event_line = json.dumps(event, ensure_ascii=False) + "\n"
            (session_dir / "events.ndjson").write_text(event_line, encoding="utf-8")
            (session_dir / "tts_cursor.json").write_text(json.dumps({
                "session_id": session_dir.name,
                "offset": 0,
            }), encoding="utf-8")
            (session_dir / "tts_delivery.ndjson").write_text(json.dumps({
                "event_id": "crashed-gift-1", "stage": "queued",
            }) + "\n", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path), patch.object(
                tts_worker, "BROWSER_SESSION_ROOT", browser_root
            ), patch.object(tts_worker, "UNMAPPED_GIFTS_PATH", root / "unmapped.ndjson"):
                restarted = tts_worker.TTSWorker(
                    "test_streamer", browser_session_dir=session_dir
                )
                restarted._attach(session_dir)
                self.assertTrue(restarted.resume_existing_session)
                recovered = await restarted._recover_inflight_gifts()
                self.assertEqual(recovered, 1)
                self.assertEqual(restarted.queue.qsize(), 1)
                self.assertEqual(restarted.metrics["gift_queued"], 1)

                # A completed Gift is skipped when resuming from an older cursor.
                with (session_dir / "tts_delivery.ndjson").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "event_id": "crashed-gift-1", "stage": "playback_completed",
                    }) + "\n")
                replay = tts_worker.TTSWorker(
                    "test_streamer", browser_session_dir=session_dir
                )
                replay._attach(session_dir)
                await replay._read_new_events()
                self.assertEqual(replay.queue.qsize(), 0)
                self.assertEqual(replay.metrics["skipped"]["already_delivered_replay"], 1)
                restarted._close_tail()
                replay._close_tail()

    async def test_queue_keeps_chats_and_gifts_in_event_timestamp_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")

            now_ms = int(time.time() * 1000)
            later_chat = {
                "type": "chat", "comment": "later chat", "unique_id": "viewer",
                "received_at_ms": now_ms + 200,
            }
            earlier_chat = {
                "type": "chat", "comment": "earlier chat", "unique_id": "viewer2",
                "received_at_ms": now_ms + 100,
            }
            earlier_gift = {
                "type": "gift", "nickname": "Gift sender", "gift_name": "Rose",
                "repeat_count": 1, "counted": True, "received_at_ms": now_ms + 50,
            }
            await worker._process_line(json.dumps(later_chat).encode("utf-8"))
            await worker._process_line(json.dumps(earlier_gift).encode("utf-8"))
            await worker._process_line(json.dumps(earlier_chat).encode("utf-8"))

            queued = []
            while not worker.queue.empty():
                item = worker.queue.get_nowait()
                queued.append((item[-1], item[2]))
                worker.queue.task_done()
            self.assertEqual([event_type for event_type, _ in queued], ["gift", "chat", "chat"])
            self.assertEqual(queued[0][1].text, "Gift sender \u9001\u51fa\u73ab\u7470\u82b1 1 \u500b")
            self.assertEqual(
                [message.text for _, message in queued[1:]],
                ["earlier chat", "later chat"],
            )

    async def test_queue_wait_metric_excludes_time_before_enqueue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            message = PreparedChat(
                user_key=None,
                text="",
                voice="zh-TW",
                received_at_ms=int(time.time() * 1000) - 10_000,
            )
            await worker._enqueue(
                message,
                "member_sound",
                event_arrived_at=time.monotonic() - 10,
                order_at_ms=int(time.time() * 1000),
            )
            await asyncio.sleep(0.02)
            announcement = await worker._prepare_next_announcement()
            self.assertIsNotNone(announcement)
            self.assertGreaterEqual(worker.metrics["max_queue_wait_ms"], 10)
            self.assertLess(worker.metrics["max_queue_wait_ms"], 500)
            await worker._deliver_prepared_announcement(announcement)


class TTSWorkerOfflineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_chat_is_synthesized_during_current_playback_in_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(TTSSettings(min_request_interval_seconds=0.1).to_mapping()),
                encoding="utf-8",
            )

            class FakeBackend:
                def __init__(self):
                    self.synthesized = []
                    self.audio_text = {}
                    self.second_synthesized = asyncio.Event()

                async def synthesize(self, text, voice, rate_percent):
                    self.synthesized.append(text)
                    if text == "second chat":
                        self.second_synthesized.set()
                    path = root / f"{text.replace(' ', '_')}.mp3"
                    path.write_bytes(b"audio")
                    self.audio_text[path] = text
                    return path

            class FakePlayer:
                def __init__(self, backend):
                    self.backend = backend
                    self.played = []

                async def play(self, audio_path, volume):
                    text = self.backend.audio_text[audio_path]
                    if text == "first chat":
                        await asyncio.wait_for(
                            self.backend.second_synthesized.wait(), timeout=2
                        )
                    self.played.append(text)

            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            backend = FakeBackend()
            player = FakePlayer(backend)
            worker.backend = backend
            worker.player = player
            now_ms = int(time.time() * 1000)
            for comment, offset in (("first chat", 0), ("second chat", 1)):
                await worker._process_line(json.dumps({
                    "type": "chat",
                    "comment": comment,
                    "unique_id": comment,
                    "received_at_ms": now_ms + offset,
                }).encode("utf-8"))

            playback = asyncio.create_task(worker._speak())
            await asyncio.wait_for(worker.queue.join(), timeout=3)
            playback.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await playback

            self.assertEqual(backend.synthesized, ["first chat", "second chat"])
            self.assertEqual(player.played, ["first chat", "second chat"])

    async def test_worker_synthesizes_mixed_chat_in_script_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(TTSSettings(min_request_interval_seconds=0.1).to_mapping()),
                encoding="utf-8",
            )

            class FakeBackend:
                def __init__(self):
                    self.calls = []

                async def synthesize(self, text, voice, rate_percent):
                    self.calls.append((text, voice))
                    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                    handle.close()
                    return Path(handle.name)

            class FakePlayer:
                def __init__(self):
                    self.played = []

                async def play(self, audio_path, volume):
                    self.played.append(audio_path)

                def stop(self):
                    pass

                def close(self):
                    pass

            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            backend = FakeBackend()
            player = FakePlayer()
            worker.backend = backend
            worker.player = player
            await worker._process_line(json.dumps({
                "type": "chat", "comment": "Hello 今天",
                "received_at_ms": int(time.time() * 1000),
            }).encode("utf-8"))

            playback = asyncio.create_task(worker._speak())
            deadline = asyncio.get_running_loop().time() + 3
            while worker.metrics["chat_spoken"] < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            playback.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await playback

            self.assertEqual(backend.calls, [
                ("Hello", "en-US-AriaNeural"),
                ("今天", "zh-TW-HsiaoChenNeural"),
            ])
            self.assertEqual(len(player.played), 2)
            self.assertEqual(worker.metrics["chat_spoken"], 1)

    async def test_gift_followup_audio_plays_after_gift_tts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sound_path = root / "gift.wav"
            sound_path.write_bytes(b"test audio")
            rose_path = root / "rose.wav"
            rose_path.write_bytes(b"specific test audio")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings(
                min_request_interval_seconds=0.1,
                gift_followup_audio_path=str(sound_path),
                gift_sound_by_id=(("5655", str(rose_path)),),
            ).to_mapping()), encoding="utf-8")

            class FakeBackend:
                async def synthesize(self, text, voice, rate_percent):
                    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                    handle.close()
                    return Path(handle.name)

            class FakePlayer:
                def __init__(self):
                    self.played = []

                async def play(self, audio_path, volume):
                    self.played.append(Path(audio_path))

                def play_effect(self, audio_path, volume):
                    self.played.append(Path(audio_path))
                    return True

                def stop(self):
                    pass

                def close(self):
                    pass

            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")
            player = FakePlayer()
            worker.backend = FakeBackend()
            worker.player = player
            await worker._process_line(json.dumps({
                "type": "gift", "nickname": "Gift sender", "gift_name": "Rose",
                "gift_id": "5655",
                "repeat_count": 1, "counted": True,
                "received_at_ms": int(time.time() * 1000),
            }).encode("utf-8"))

            playback = asyncio.create_task(worker._speak())
            deadline = asyncio.get_running_loop().time() + 3
            while worker.metrics["gift_spoken"] < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            playback.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await playback

            self.assertEqual(player.played[-1], rose_path)
            self.assertEqual(worker.metrics["gift_followup_sounds_played"], 1)
            self.assertEqual(worker.metrics["gift_delivery_failures"], 0)

    async def test_failed_event_retries_do_not_leave_global_backoff_for_next_chat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(TTSSettings(
                min_request_interval_seconds=0.1,
            ).to_mapping()), encoding="utf-8")
            with patch.object(tts_worker, "CONFIG_PATH", config_path):
                worker = tts_worker.TTSWorker("test_streamer")

            class IntermittentBackend:
                def __init__(self):
                    self.calls = 0

                async def synthesize(self, text, voice, rate_percent):
                    self.calls += 1
                    if self.calls <= 3:
                        raise RuntimeError("synthetic no-audio response")
                    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                    handle.close()
                    return Path(handle.name)

            class FakePlayer:
                async def play(self, audio_path, volume):
                    return None

                def play_effect(self, audio_path, volume):
                    return True

            worker.backend = IntermittentBackend()
            worker.player = FakePlayer()
            now_ms = int(time.time() * 1000)
            for comment, offset in (("first", 0), ("second", 1)):
                await worker._process_line(json.dumps({
                    "type": "chat", "comment": comment, "unique_id": comment,
                    "received_at_ms": now_ms + offset,
                }).encode("utf-8"))
            playback = asyncio.create_task(worker._speak())
            await asyncio.wait_for(worker.queue.join(), timeout=6)
            playback.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await playback
            self.assertEqual(worker.metrics["delivery_failures"], 1)
            self.assertEqual(worker.metrics["chat_spoken"], 1)
            self.assertEqual(worker.backend_retry_at, 0.0)

    async def test_old_chat_expires_but_old_captured_gift_is_delivered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            settings = TTSSettings(
                user_cooldown_seconds=0,
                duplicate_window_seconds=0,
                min_request_interval_seconds=0.1,
                chat_ttl_seconds=8,
            )
            config_path.write_text(json.dumps(settings.to_mapping()), encoding="utf-8")

            class FakeBackend:
                def __init__(self):
                    self.texts = []

                async def synthesize(self, text, voice, rate_percent):
                    self.texts.append(text)
                    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                    handle.close()
                    return Path(handle.name)

            class FakePlayer:
                async def play(self, audio_path, volume):
                    pass

                def stop(self):
                    pass

                def close(self):
                    pass

            with patch.object(tts_worker, "CONFIG_PATH", config_path), patch.object(
                tts_worker, "STATE_PATH", root / "state.json"
            ):
                worker = tts_worker.TTSWorker("test_streamer")
            backend = FakeBackend()
            worker.backend = backend
            worker.player = FakePlayer()
            now_ms = int(time.time() * 1000)
            gift = {
                "type": "gift", "nickname": "Gift sender", "gift_name": "Rose",
                "repeat_count": 1, "counted": True,
                "received_at_ms": now_ms - 30_000,
            }
            chat = {
                "type": "chat", "comment": "old chat", "unique_id": "viewer",
                "received_at_ms": now_ms - 12_000,
            }
            await worker._process_line(json.dumps(gift).encode("utf-8"))
            await worker._process_line(json.dumps(chat).encode("utf-8"))

            playback = asyncio.create_task(worker._speak())
            deadline = asyncio.get_running_loop().time() + 3
            while (
                worker.metrics["gift_spoken"] < 1
                or worker.metrics["skipped"]["expired_chat"] < 1
            ) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            playback.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await playback

            expected_text = (
                "Gift sender \u9001\u51fa"
                + spoken_gift_name("unknown", "Rose")
                + " 1 \u500b"
            )
            expected_text = clean_chat_text(expected_text, speak_common_emoji=True)
            expected_parts = speech_segments(
                expected_text,
                zh_voice="zh-TW-HsiaoChenNeural",
                en_voice="en-US-AriaNeural",
            )
            self.assertEqual(backend.texts, [part for part, _ in expected_parts])
            self.assertEqual(worker.metrics["skipped"]["expired_chat"], 1)
            self.assertEqual(worker.metrics["gift_delivery_failures"], 0)
            with patch.object(tts_worker, "STATE_PATH", root / "state.json"):
                worker.update_state("test")
            saved_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_state["metrics"]["p50_event_to_playback_ms"], saved_state["metrics"]["p95_event_to_playback_ms"])

    async def test_worker_speaks_only_new_chat_without_external_services(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_root = root / "raw"
            session_dir = raw_root / "20260923_170000_test_streamer"
            session_dir.mkdir(parents=True)
            events_path = session_dir / "events.ndjson"
            old_event = {"type": "chat", "comment": "old chat", "unique_id": "old"}
            events_path.write_text(json.dumps(old_event) + "\n", encoding="utf-8")
            (session_dir / "session.json").write_text(
                json.dumps({
                    "username": "test_streamer",
                    "status": "running",
                    "collector_started_at_utc": "2026-09-23T09:00:00+00:00",
                }),
                encoding="utf-8",
            )
            watcher_path = root / "watcher.json"
            watcher_path.write_text(
                json.dumps({
                    "streamers": {
                        "test_streamer": {
                            "status": "collecting",
                            "collector_started_at_utc": "2026-09-23T09:00:00+00:00",
                        }
                    }
                }),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(TTSSettings(
                    user_cooldown_seconds=0,
                    duplicate_window_seconds=0,
                ).to_mapping()),
                encoding="utf-8",
            )

            class FakeBackend:
                def __init__(self):
                    self.texts = []

                async def synthesize(self, text, voice, rate_percent):
                    self.texts.append((text, voice, rate_percent))
                    handle = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                    handle.close()
                    return Path(handle.name)

            class FakePlayer:
                def __init__(self):
                    self.played = []

                def initialize(self):
                    pass

                async def play(self, audio_path, volume):
                    self.played.append((audio_path, volume))

                def stop(self):
                    pass

                def close(self):
                    pass

            with patch.multiple(
                tts_worker,
                RAW_ROOT=raw_root,
                WATCHER_STATE=watcher_path,
                CONFIG_PATH=config_path,
                STATE_PATH=root / "tts-state.json",
                STOP_PATH=root / "tts-stop.json",
                LOCK_PATH=root / "tts.lock",
            ):
                worker = tts_worker.TTSWorker("test_streamer")
                backend = FakeBackend()
                player = FakePlayer()
                worker.backend = backend
                worker.player = player
                task = asyncio.create_task(worker.run())
                await asyncio.sleep(0.05)
                fresh_event = {
                    "type": "chat",
                    "comment": "new chat",
                    "unique_id": "new",
                    "received_at_ms": 1_800_000_000_000,
                }
                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(fresh_event) + "\n")

                deadline = asyncio.get_running_loop().time() + 3
                while worker.metrics["spoken"] < 1 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
                (root / "tts-stop.json").write_text("{}", encoding="utf-8")
                await asyncio.wait_for(task, timeout=3)

            self.assertEqual(backend.texts, [("new chat", "en-US-AriaNeural", 35)])
            self.assertEqual(worker.metrics["spoken"], 1)
            self.assertEqual(len(player.played), 1)

    def test_worker_only_follows_session_when_watcher_is_collecting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_root = root / "raw"
            session_dir = raw_root / "20260923_170000_test_streamer"
            session_dir.mkdir(parents=True)
            (session_dir / "events.ndjson").write_text("", encoding="utf-8")
            (session_dir / "session.json").write_text(
                json.dumps({
                    "username": "test_streamer",
                    "status": "running",
                    "collector_started_at_utc": "2026-09-23T09:00:00+00:00",
                }),
                encoding="utf-8",
            )
            watcher_path = root / "watcher.json"
            watcher_path.write_text(
                json.dumps({
                    "streamers": {
                        "test_streamer": {
                            "status": "collecting",
                            "collector_started_at_utc": "2026-09-23T09:00:00+00:00",
                        }
                    }
                }),
                encoding="utf-8",
            )

            active, status = tts_worker.find_active_session(
                raw_root, watcher_path, "test_streamer"
            )
            self.assertEqual(active, session_dir)
            self.assertEqual(status, "collecting")

            watcher_path.write_text(
                json.dumps({"streamers": {"test_streamer": {"status": "waiting"}}}),
                encoding="utf-8",
            )
            active, status = tts_worker.find_active_session(
                raw_root, watcher_path, "test_streamer"
            )
            self.assertIsNone(active)
            self.assertEqual(status, "waiting_for_collector")


if __name__ == "__main__":
    unittest.main()
