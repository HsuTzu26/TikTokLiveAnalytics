import json
import tempfile
import unittest
from pathlib import Path

from app.dashboard import browser_viewer_count, combine_session_metrics, load_session


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _viewer_frame(total: int, total_user: int) -> bytes:
    from TikTokLiveProto.v3.webcast.model.message import WebcastRoomUserSeqMessage

    payload = bytes(WebcastRoomUserSeqMessage.from_dict({
        "total": total,
        "total_user": total_user,
    }))
    message = _field(1, b"WebcastRoomUserSeqMessage") + _field(2, payload)
    response = _field(1, message)
    return _field(7, b"msg") + _field(8, response)


class DashboardViewerSampleTests(unittest.TestCase):
    def _write_session(self, root: Path, name: str, events: list[dict]) -> Path:
        session = root / name
        session.mkdir()
        (session / "session.json").write_text(
            json.dumps({"username": "streamer", "provider": "browser_network"}),
            encoding="utf-8",
        )
        (session / "events.ndjson").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        return session

    def test_latest_viewer_sample_uses_cdp_receive_time(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session = self._write_session(root, "session", [
                {
                    "type": "viewer",
                    "viewer_count": 101,
                    "timestamp_ms": 1_800_000_010_000,
                    "timestamp_local": "2027-01-15T00:00:10+08:00",
                    "received_at_ms": 1_800_000_020_000,
                    "received_at_local": "2027-01-15T00:00:20+08:00",
                },
                {
                    "type": "viewer",
                    "viewer_count": 99,
                    "timestamp_ms": 1_800_000_030_000,
                    "timestamp_local": "2027-01-15T00:00:30+08:00",
                    "received_at_ms": 1_800_000_015_000,
                    "received_at_local": "2027-01-15T00:00:15+08:00",
                },
            ])

            metrics = load_session(session)
            combined = combine_session_metrics([session], session)

            self.assertEqual(metrics["viewers"][-1], 101)
            self.assertEqual(combined["viewers"][-1], 101)

    def test_old_browser_events_can_be_corrected_from_the_bounded_raw_frame(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = Path(temporary_directory) / "session"
            frames = session / "frames"
            frames.mkdir(parents=True)
            (frames / "frame_000001.bin").write_bytes(_viewer_frame(292, 30_395))

            corrected = browser_viewer_count(session, {
                "frame_seq": 1,
                "msg_id": "",
                "viewer_count": 30_395,
            })

            self.assertEqual(corrected, 292)


if __name__ == "__main__":
    unittest.main()
