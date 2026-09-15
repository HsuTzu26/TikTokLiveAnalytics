import json
import unittest
from unittest.mock import patch

from src.snapshots import TikToolSnapshots, public_live_snapshot


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class SnapshotTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_public_live_snapshot(self, urlopen):
        state = {
            "LiveRoom": {
                "liveRoomUserInfo": {
                    "user": {"uniqueId": "demo", "roomId": "123", "status": 2},
                    "stats": {"followerCount": 10},
                    "liveRoom": {
                        "title": "Demo LIVE",
                        "startTime": 100,
                        "status": 2,
                        "liveRoomStats": {"enterCount": 44, "userCount": 7},
                    },
                }
            }
        }
        page = f'<script id="SIGI_STATE">{json.dumps(state)}</script>'.encode()
        urlopen.return_value = FakeResponse(page)
        result = public_live_snapshot("demo")
        self.assertEqual(result["room_id"], "123")
        self.assertEqual(result["viewer_count"], 7)
        self.assertEqual(result["enter_count"], 44)
        self.assertTrue(result["live"])

    def test_rest_is_disabled_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(TikToolSnapshots().enabled)


if __name__ == "__main__":
    unittest.main()
