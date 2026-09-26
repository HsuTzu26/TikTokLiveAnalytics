import json
import tempfile
import unittest
from pathlib import Path

from src.live_state import LiveStateWriter


class LiveStateWriterTests(unittest.TestCase):
    def test_state_is_atomic_bounded_and_counts_only_completed_gifts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "live_state.json"
            writer = LiveStateWriter(
                path,
                session_id="session-1",
                username="test_streamer",
                flush_interval_seconds=60,
                recent_limit=2,
                viewer_sample_limit=2,
                activity_limit=2,
            )
            writer.flush(force=True)
            writer.record({
                "type": "viewer", "viewer_count": 9, "received_at_ms": 1,
                "received_at_utc": "2026-09-26T00:00:00+00:00",
            }, force=True)
            writer.record({
                "type": "gift", "counted": False, "diamond_total": 100,
                "received_at_utc": "2026-09-26T00:00:01+00:00",
            })
            writer.record({
                "type": "gift", "counted": True, "diamond_total": 4,
                "gift_name": "Rose", "repeat_count": 2,
                "received_at_utc": "2026-09-26T00:00:02+00:00",
            })
            writer.record({
                "type": "chat", "unique_id": "viewer", "comment": "hello",
                "received_at_utc": "2026-09-26T00:00:03+00:00",
            })
            writer.flush(force=True)

            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["viewer_count"], 9)
            self.assertEqual(state["diamonds"], 4)
            self.assertEqual(state["gift_count"], 1)
            self.assertEqual(state["chat_count"], 1)
            self.assertEqual(len(state["recent_gifts"]), 1)
            self.assertEqual(len(state["recent_activity"]), 2)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_missing_viewer_samples_stay_missing_instead_of_becoming_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "live_state.json"
            writer = LiveStateWriter(path, session_id="session-1", username="x")
            writer.record({"type": "system", "system_event": "disconnected"}, force=True)

            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(state["viewer_count"])
            self.assertEqual(state["viewer_samples"], [])
            self.assertEqual(state["connection_state"], "disconnected")

    def test_reconnect_closes_and_measures_the_observed_gap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "live_state.json"
            writer = LiveStateWriter(path, session_id="session-1", username="x")
            writer.record({
                "type": "system", "system_event": "disconnected",
                "received_at_ms": 1000,
            })
            writer.record({
                "type": "system", "system_event": "reconnected",
                "received_at_ms": 5500,
            }, force=True)

            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["gap_count"], 1)
            self.assertEqual(state["last_gap_seconds"], 4.5)
            self.assertEqual(state["current_gap_started_at_ms"], None)


if __name__ == "__main__":
    unittest.main()
