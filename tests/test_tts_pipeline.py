import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tts.pipeline import (
    ChatProcessor,
    TTSSettings,
    clean_chat_text,
    dominant_language,
)
from src import tts_worker


class TTSChatPipelineTests(unittest.TestCase):
    def test_cleaning_removes_urls_emoji_and_limits_length(self):
        self.assertEqual(
            clean_chat_text("你好 😊 https://example.com nice", max_length=10),
            "你好 nice",
        )

    def test_language_router_uses_dominant_script_for_whole_message(self):
        self.assertEqual(dominant_language("今天開台嗎"), "zh-TW")
        self.assertEqual(dominant_language("hello streamer"), "en-US")
        self.assertEqual(dominant_language("hi 今天"), "zh-TW")

    def test_user_cooldown_and_duplicate_window_are_enforced(self):
        processor = ChatProcessor(TTSSettings())
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
        self.assertEqual(settings.min_request_interval_seconds, 0.5)
        self.assertEqual(settings.blacklist_terms, ("a", "b"))
        self.assertEqual(TTSSettings.from_mapping(settings.to_mapping()), settings)

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

    def test_ndjson_chat_is_enqueued_and_full_queue_drops_new_message(self):
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
                worker._process_line(json.dumps(event).encode("utf-8"))

            self.assertEqual(worker.metrics["chat_events_seen"], 2)
            self.assertEqual(worker.queue.qsize(), 1)
            self.assertEqual(worker.metrics["skipped"]["queue_full"], 1)
            queued_message, _ = worker.queue.get_nowait()
            self.assertEqual(queued_message.text, "hello")

    def test_mid_session_attach_skips_existing_chat_backlog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(TTSSettings().to_mapping()), encoding="utf-8")
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
            worker._read_new_events()

            self.assertEqual(worker.metrics["chat_events_seen"], 1)
            queued_message, _ = worker.queue.get_nowait()
            self.assertEqual(queued_message.text, "new chat")
            worker._close_tail()


class TTSWorkerOfflineIntegrationTests(unittest.IsolatedAsyncioTestCase):
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

            self.assertEqual(backend.texts, [("new chat", "en-US-AriaNeural", 5)])
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
