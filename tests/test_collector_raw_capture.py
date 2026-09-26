import unittest

from src.collector_cli import build_argument_parser


class CollectorRawCaptureTests(unittest.TestCase):
    def test_raw_payload_capture_is_disabled_by_default(self):
        args = build_argument_parser().parse_args(["test_streamer"])
        self.assertFalse(args.capture_raw)

    def test_raw_payload_capture_requires_explicit_debug_flag(self):
        args = build_argument_parser().parse_args(
            ["test_streamer", "--capture-raw"]
        )
        self.assertTrue(args.capture_raw)


if __name__ == "__main__":
    unittest.main()
