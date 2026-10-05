import base64
import asyncio
import gzip
import importlib.util
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

from src.providers.base import NormalizedEventBus
from src.providers.browser_network import (
    BrowserNetworkProvider,
    FrameRecorder,
    _sdk_decode_methods,
    build_parser,
    _feasibility_report,
    confirm_live_from_event,
    decode_cdp_payload,
    decode_webcast_frame,
    clear_browser_http_cache,
    is_candidate_websocket,
    is_live_candidate_websocket,
    inspect_event_frame,
    _local_cdp_endpoint,
    _uses_everyday_chrome_profile,
    normalize_proto_event,
    parse_live_preflight,
    sanitize_url,
    try_gzip_decompress,
    websocket_hostname,
)


def varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def length_delimited(field_number: int, payload: bytes) -> bytes:
    return varint((field_number << 3) | 2) + varint(len(payload)) + payload


def synthetic_push_frame(
    method: str,
    compressed: bool = False,
    message_payload: bytes = b"",
) -> bytes:
    message = length_delimited(1, method.encode("ascii"))
    if message_payload:
        message += length_delimited(2, message_payload)
    response = length_delimited(1, message)
    headers = b""
    if compressed:
        response = gzip.compress(response)
        header = length_delimited(1, b"compress_type") + length_delimited(2, b"gzip")
        headers = length_delimited(5, header)
    return headers + length_delimited(7, b"msg") + length_delimited(8, response)


class BrowserWebSocketProbeTests(unittest.TestCase):
    def test_cli_defaults_to_headful_chromium_and_bounded_capture(self) -> None:
        args = build_parser().parse_args(["--username", "kclcann"])
        self.assertFalse(args.headless)
        self.assertEqual(args.max_frames, 5000)
        self.assertFalse(args.save_raw_frames)
        self.assertFalse(args.clear_browser_cache_on_start)
        debug_args = build_parser().parse_args([
            "--username", "kclcann", "--save-raw-frames", "--max-frames", "12",
        ])
        self.assertTrue(debug_args.save_raw_frames)
        self.assertEqual(debug_args.max_frames, 12)
        self.assertEqual(args.browser_mode, "launch")
        cdp_args = build_parser().parse_args([
            "--username", "kclcann", "--browser-mode", "cdp",
            "--cdp-url", "http://127.0.0.1:9333", "--page-index", "1",
            "--reload-live-page", "--clear-browser-cache-on-start",
        ])
        self.assertEqual(cdp_args.cdp_url, "http://127.0.0.1:9333")
        self.assertEqual(cdp_args.page_index, 1)
        self.assertTrue(cdp_args.reload_live_page)
        self.assertTrue(cdp_args.clear_browser_cache_on_start)
        self.assertTrue(_local_cdp_endpoint(cdp_args.cdp_url))
        self.assertFalse(_local_cdp_endpoint("http://0.0.0.0:9222"))

    def test_browser_provider_uses_explicit_cdp_target(self) -> None:
        provider = BrowserNetworkProvider(
            "streamer",
            cdp_url="http://127.0.0.1:9222",
            output_dir="data/browser_ws_probe",
        )
        self.assertEqual(provider.args.browser_mode, "cdp")
        self.assertEqual(provider.args.cdp_url, "http://127.0.0.1:9222")
        self.assertEqual(provider.args.username, "streamer")
        self.assertFalse(provider.args.reload_live_page)
        self.assertFalse(provider.args.save_raw_frames)
        self.assertFalse(provider.args.clear_browser_cache_on_start)

    def test_browser_http_cache_clear_uses_cdp_and_reports_failure_safely(self) -> None:
        class FakeCDPSession:
            def __init__(self, error: Exception | None = None) -> None:
                self.commands: list[str] = []
                self.error = error

            async def send(self, command: str) -> None:
                self.commands.append(command)
                if self.error:
                    raise self.error

        session = FakeCDPSession()
        self.assertEqual(asyncio.run(clear_browser_http_cache(session)), (True, None))
        self.assertEqual(session.commands, ["Network.clearBrowserCache"])

        failed_session = FakeCDPSession(RuntimeError("do not persist CDP details"))
        self.assertEqual(
            asyncio.run(clear_browser_http_cache(failed_session)),
            (False, "RuntimeError"),
        )

    def test_bounded_event_bus_does_not_block_durable_frame_output(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.model.message import WebcastChatMessage

        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory) / "session"
            bus = NormalizedEventBus(queue_size=1)
            overlay_queue = bus.subscribe("overlay")
            recorder = FrameRecorder(session_dir, max_frames=2, event_bus=bus)
            for sequence, comment in enumerate(("first", "second"), start=1):
                event_payload = bytes(WebcastChatMessage.from_dict({
                    "common": {
                        "create_time": 1_800_000_000,
                        "msg_id": sequence,
                        "room_id": 456,
                    },
                    "user": {"id_str": "42", "display_id": "viewer", "nickname": "Viewer"},
                    "content": comment,
                }))
                recorder.record(
                    f"request-{sequence}",
                    "wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/",
                    2,
                    base64.b64encode(synthetic_push_frame(
                        "WebcastChatMessage", message_payload=event_payload
                    )).decode("ascii"),
                )
            recorder.close()

            self.assertEqual(overlay_queue.get_nowait()["comment"], "first")
            self.assertEqual(bus.dropped["overlay"], 1)
            rows = [
                json.loads(line)
                for line in (session_dir / "events.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["comment"] for row in rows], ["first", "second"])
            self.assertFalse((session_dir / "frames").exists())
            self.assertFalse((session_dir / "frames.ndjson").exists())

    def test_duplicate_normalized_chat_is_dropped_but_raw_frames_remain(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.model.message import WebcastChatMessage

        payload = bytes(WebcastChatMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 77, "room_id": 456},
            "user": {"id_str": "42", "display_id": "viewer", "nickname": "Viewer"},
            "content": "same message",
        }))
        frame = base64.b64encode(synthetic_push_frame(
            "WebcastChatMessage", message_payload=payload
        )).decode("ascii")
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory) / "session"
            recorder = FrameRecorder(session_dir, max_frames=2, save_raw_frames=True)
            for request_id in ("request-1", "request-2"):
                recorder.record(
                    request_id,
                    "wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/",
                    2,
                    frame,
                )
            recorder.close()

            events = (session_dir / "events.ndjson").read_text(encoding="utf-8").splitlines()
            frames = (session_dir / "frames.ndjson").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 1)
            self.assertEqual(len(frames), 2)
            self.assertEqual(recorder.duplicate_event_counts["chat"], 1)

    def test_launch_profile_guard_rejects_everyday_chrome_data(self) -> None:
        local_app_data = r"C:\Users\Example\AppData\Local"
        with patch.dict("os.environ", {"LOCALAPPDATA": local_app_data}):
            self.assertTrue(_uses_everyday_chrome_profile(
                local_app_data + r"\Google\Chrome\User Data\Profile 1"
            ))
            self.assertFalse(_uses_everyday_chrome_profile(
                local_app_data + r"\TikTokLiveAnalytics\data\browser_profile"
            ))

    def test_binary_cdp_payload_is_base64_decoded(self) -> None:
        original = b"\x00\x01\xffTikTok"
        self.assertEqual(
            decode_cdp_payload(2, base64.b64encode(original).decode("ascii")),
            original,
        )
        self.assertEqual(decode_cdp_payload(2, "YQ"), b"a")

    def test_text_cdp_payload_remains_utf8_text_bytes(self) -> None:
        self.assertEqual(decode_cdp_payload(1, "café"), "café".encode("utf-8"))

    def test_malformed_binary_base64_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_cdp_payload(2, "%%%not-base64%%")

    def test_method_names_are_found_in_raw_frame(self) -> None:
        methods, errors = inspect_event_frame(
            b"prefix WebcastChatMessage and WebcastGiftMessage suffix"
        )
        self.assertEqual(methods, ["WebcastChatMessage", "WebcastGiftMessage"])
        self.assertEqual(errors, [])

    def test_method_names_are_extracted_from_push_frame_envelope(self) -> None:
        methods, errors = inspect_event_frame(synthetic_push_frame("WebcastLikeMessage"))
        self.assertEqual(methods, ["WebcastLikeMessage"])
        self.assertEqual(errors, [])

    def test_existing_tiktoklive_sdk_definitions_when_installed(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        self.assertEqual(
            _sdk_decode_methods(synthetic_push_frame("WebcastRoomUserSeqMessage")),
            ["WebcastRoomUserSeqMessage"],
        )

    def test_sdk_envelope_normalizes_chat_from_synthetic_protobuf(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.model.message import WebcastChatMessage

        payload = bytes(WebcastChatMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 123, "room_id": 456},
            "user": {
                "id": 42,
                "id_str": "42",
                "display_id": "viewer_42",
                "nickname": "Viewer",
            },
            "content": "TEST_CHAT_927",
        }))
        decoded, errors = decode_webcast_frame(
            synthetic_push_frame("WebcastChatMessage", message_payload=payload),
            received_at_ms=1_800_000_100,
        )
        self.assertEqual(errors, [])
        self.assertEqual(decoded[0]["method"], "WebcastChatMessage")
        self.assertEqual(decoded[0]["normalized"]["comment"], "TEST_CHAT_927")
        self.assertEqual(decoded[0]["normalized"]["unique_id"], "viewer_42")

    def test_sdk_envelope_normalizes_completed_gift_streak(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.model.message import WebcastGiftMessage

        payload = bytes(WebcastGiftMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 789, "room_id": 456},
            "user": {
                "id": 42,
                "id_str": "42",
                "display_id": "viewer_42",
                "nickname": "Viewer",
            },
            "gift": {"id": 13, "name": "Rose", "type": 1, "diamond_count": 1},
            "gift_id": 13,
            "repeat_count": 10,
            "repeat_end": 1,
            "order_id": "order-1",
        }))
        decoded, errors = decode_webcast_frame(
            synthetic_push_frame("WebcastGiftMessage", message_payload=payload),
            received_at_ms=1_800_000_100,
        )
        self.assertEqual(errors, [])
        gift = decoded[0]["normalized"]
        self.assertEqual(gift["gift_name"], "Rose")
        self.assertEqual(gift["repeat_count"], 10)
        self.assertTrue(gift["counted"])
        self.assertEqual(gift["diamond_total"], 10)

    def test_sdk_envelope_normalizes_stream_ended_control(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.im import ControlAction
        from TikTokLiveProto.v3.webcast.model.message import WebcastControlMessage

        payload = bytes(WebcastControlMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 987, "room_id": 456},
            "action": ControlAction.STREAM_ENDED,
        }))
        decoded, errors = decode_webcast_frame(
            synthetic_push_frame("WebcastControlMessage", message_payload=payload),
            received_at_ms=1_800_000_100,
        )
        self.assertEqual(errors, [])
        self.assertEqual(decoded[0]["method"], "WebcastControlMessage")
        self.assertEqual(decoded[0]["normalized"]["type"], "live_ended")
        self.assertEqual(decoded[0]["normalized"]["action"], "STREAM_ENDED")
        self.assertEqual(decoded[0]["normalized"]["room_id"], "456")
        pause_payload = bytes(WebcastControlMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 988, "room_id": 456},
            "action": ControlAction.STREAM_PAUSED,
        }))
        paused, pause_errors = decode_webcast_frame(
            synthetic_push_frame("WebcastControlMessage", message_payload=pause_payload),
            received_at_ms=1_800_000_100,
        )
        self.assertEqual(pause_errors, [])
        self.assertIsNone(paused[0]["normalized"])

    def test_live_end_event_is_written_and_published_once_per_room(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.im import ControlAction
        from TikTokLiveProto.v3.webcast.model.message import WebcastControlMessage

        payload = bytes(WebcastControlMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 987, "room_id": 456},
            "action": ControlAction.STREAM_ENDED,
        }))
        encoded = base64.b64encode(synthetic_push_frame(
            "WebcastControlMessage", message_payload=payload
        )).decode("ascii")
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory) / "session"
            bus = NormalizedEventBus(queue_size=4)
            queue = bus.subscribe("settlement")
            recorder = FrameRecorder(session_dir, max_frames=2, event_bus=bus)
            for request_id in ("request-1", "request-2"):
                recorder.record(
                    request_id,
                    "wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/",
                    2,
                    encoded,
                )
            recorder.close()

            records = [
                json.loads(line)
                for line in (session_dir / "events.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["type"], "live_ended")
            self.assertEqual(queue.get_nowait()["action"], "STREAM_ENDED")
            self.assertTrue(recorder.live_end_event)
            self.assertEqual(recorder.duplicate_event_counts["live_ended"], 1)

    def test_viewer_ranks_are_written_in_analytics_compatible_format(self) -> None:
        if importlib.util.find_spec("TikTokLive") is None:
            self.skipTest("TikTokLive SDK is not installed in this interpreter")
        from TikTokLiveProto.v3.webcast.model.message import WebcastRoomUserSeqMessage

        payload = bytes(WebcastRoomUserSeqMessage.from_dict({
            "common": {"create_time": 1_800_000_000, "msg_id": 765, "room_id": 456},
            "total": 321,
            "total_user": 321,
            "ranks": [{
                "rank": 1,
                "score": 1234,
                "delta": 5,
                "user": {"id": 42, "display_id": "viewer_42", "nickname": "Viewer"},
            }],
        }))
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory) / "session"
            recorder = FrameRecorder(session_dir, max_frames=1)
            recorder.record(
                "request-1",
                "wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/",
                2,
                base64.b64encode(synthetic_push_frame(
                    "WebcastRoomUserSeqMessage", message_payload=payload
                )).decode("ascii"),
            )
            recorder.close()

            from src.analytics import ranking_history

            event = json.loads(
                (session_dir / "events.ndjson").read_text(encoding="utf-8").strip()
            )
            self.assertEqual(recorder.ranking_snapshot_count, 1)
            self.assertFalse((session_dir / "rankings.ndjson").exists())
            self.assertEqual(event["ranks"][0]["rank"], 1)
            self.assertEqual(event["ranks"][0]["user"]["display_id"], "viewer_42")
            history = ranking_history([session_dir])
            self.assertEqual(len(history), 1)
            self.assertEqual(history.iloc[0]["rank"], 1)
            self.assertEqual(history.iloc[0]["user"], "viewer_42")

    def test_gzip_nested_payload_is_decompressed_before_method_read(self) -> None:
        compressed_frame = synthetic_push_frame("WebcastMemberMessage", compressed=True)
        methods, errors = inspect_event_frame(compressed_frame)
        self.assertEqual(methods, ["WebcastMemberMessage"])
        self.assertEqual(errors, [])
        if importlib.util.find_spec("TikTokLive") is not None:
            self.assertEqual(_sdk_decode_methods(compressed_frame), ["WebcastMemberMessage"])

    def test_gzip_success_and_failure(self) -> None:
        original = b"nested protobuf payload"
        self.assertEqual(try_gzip_decompress(gzip.compress(original)), original)
        self.assertIsNone(try_gzip_decompress(b"not gzip"))
        self.assertIsNone(try_gzip_decompress(b"\x1f\x8btruncated"))
        _, errors = inspect_event_frame(b"\x1f\x8btruncated")
        self.assertIn("gzip", errors)

    def test_malformed_protobuf_is_reported_without_crashing(self) -> None:
        methods, errors = inspect_event_frame(b"\x80")
        self.assertEqual(methods, [])
        self.assertIn("protobuf", errors)

    def test_websocket_url_query_and_credentials_are_sanitized(self) -> None:
        sanitized = sanitize_url(
            "wss://private:secret@webcast-ws.tiktok.com/webcast/im/fetch/"
            "?room_id=123&session=secret#fragment"
        )
        self.assertEqual(
            sanitized,
            "wss://webcast-ws.tiktok.com/webcast/im/fetch/",
        )
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("room_id", sanitized)

    def test_websocket_hosts_are_discovered_and_classified_safely(self) -> None:
        self.assertTrue(is_candidate_websocket("wss://webcast-ws.tiktok.com/ws?token=x"))
        self.assertTrue(is_candidate_websocket("wss://webcast-us.tiktok.com/webcast/im/fetch/"))
        self.assertTrue(is_candidate_websocket("wss://ws.tiktokv.com/webcast/im/fetch/"))
        self.assertTrue(is_candidate_websocket("wss://im-ws-sg.tiktok.com/"))
        self.assertFalse(is_candidate_websocket("wss://example.com/webcast/im/fetch/"))
        self.assertTrue(is_live_candidate_websocket("wss://webcast-ws.tiktok.com/ws"))
        self.assertTrue(is_live_candidate_websocket("wss://ws.tiktokv.com/webcast/im/fetch/"))
        self.assertFalse(is_live_candidate_websocket("wss://im-ws-sg.tiktok.com/ws/v2"))
        self.assertFalse(is_live_candidate_websocket("wss://example.com/webcast/im/fetch/"))
        self.assertEqual(websocket_hostname("wss://webcast-ws.tiktok.com/ws?token=x"), "webcast-ws.tiktok.com")

    def test_preflight_requires_explicit_live_signal_and_room_id(self) -> None:
        result = parse_live_preflight({"data": {"room": {
            "is_live": True, "room_id": "77", "create_time": 1_800_000_000,
        }}})
        self.assertEqual(result["status"], "LIVE_CONFIRMED")
        self.assertEqual(result["room_id"], "77")
        self.assertEqual(result["source_timestamp"], 1_800_000_000)
        self.assertEqual(parse_live_preflight({"status_code": 0})["status"], "UNKNOWN")
        self.assertEqual(
            parse_live_preflight({"data": {"room": {"is_live": False}}})["status"],
            "OFFLINE",
        )

    def test_decoded_live_event_confirms_observed_room(self) -> None:
        session = {
            "username": "streamer",
            "live_preflight": {"status": "UNKNOWN", "room_id": None},
        }
        self.assertFalse(confirm_live_from_event(
            session, ["WebcastGiftPanelUpdateMessage"], "123"
        ))
        self.assertTrue(confirm_live_from_event(
            session,
            ["WebcastChatMessage"],
            "123",
            observed_at="2026-09-28T00:00:00Z",
        ))
        self.assertEqual(session["live_preflight"]["status"], "LIVE_CONFIRMED")
        self.assertEqual(session["live_preflight"]["room_id"], "123")
        self.assertEqual(session["room_id"], "123")
        self.assertEqual(
            session["live_preflight"]["confirmation_source"], "webcast_event"
        )
        self.assertFalse(confirm_live_from_event(
            session, ["WebcastGiftMessage"], "123"
        ))

    def test_stage_a_requires_ten_minutes_of_live_transport(self) -> None:
        from collections import Counter
        from types import SimpleNamespace

        recorder = SimpleNamespace(
            method_counts=Counter({"WebcastChatMessage": 1}),
            decoded_field_counts=Counter({"chat": 1}),
            binary_frame_count=1,
        )
        session = {
            "live_preflight": {"status": "LIVE_CONFIRMED"},
            "target_live_websocket_open_count": 1,
            "target_live_websocket_active_seconds": 600,
            "target_binary_frame_count": 1,
            "target_method_counts": {"WebcastChatMessage": 1},
            "target_decoded_field_counts": {"chat": 1},
            "page_loaded": True,
            "duration_seconds": 600,
        }
        self.assertEqual(
            _feasibility_report(session, recorder)["stage_10_15_min_stability"],
            "PASS",
        )
        session["target_live_websocket_active_seconds"] = 569.9
        self.assertEqual(
            _feasibility_report(session, recorder)["stage_10_15_min_stability"],
            "NOT_RUN",
        )
        session["target_live_websocket_active_seconds"] = 600
        session["duration_seconds"] = 599.9
        self.assertEqual(
            _feasibility_report(session, recorder)["stage_10_15_min_stability"],
            "NOT_RUN",
        )

    def test_early_live_socket_close_does_not_pass_long_stability_gate(self) -> None:
        from collections import Counter
        from types import SimpleNamespace

        recorder = SimpleNamespace(
            method_counts=Counter({"WebcastChatMessage": 1}),
            decoded_field_counts=Counter({"chat": 1}),
            binary_frame_count=320,
        )
        session = {
            "live_preflight": {"status": "LIVE_CONFIRMED"},
            "target_live_websocket_open_count": 1,
            "target_live_websocket_close_count": 1,
            "target_live_websocket_active_seconds": 747.2,
            "target_binary_frame_count": 320,
            "target_method_counts": {"WebcastChatMessage": 89},
            "target_decoded_field_counts": {"chat": 89},
            "page_loaded": True,
            "duration_seconds": 7200,
        }
        report = _feasibility_report(session, recorder)
        self.assertEqual(report["target_transport_coverage_ratio"], 0.1038)
        self.assertEqual(report["target_live_websocket_close_count"], 1)
        self.assertEqual(report["stage_2h_stability"], "NOT_RUN")

    def test_highest_level_does_not_skip_unvalidated_tts(self) -> None:
        from collections import Counter
        from types import SimpleNamespace

        recorder = SimpleNamespace(
            method_counts=Counter({"WebcastChatMessage": 1, "WebcastGiftMessage": 1}),
            decoded_field_counts=Counter({"chat": 1, "gift": 1, "viewer": 1}),
            binary_frame_count=1,
        )
        session = {
            "live_preflight": {"status": "LIVE_CONFIRMED"},
            "target_live_websocket_open_count": 1,
            "target_live_websocket_active_seconds": 1800,
            "target_binary_frame_count": 1,
            "target_method_counts": {
                "WebcastChatMessage": 1,
                "WebcastGiftMessage": 1,
            },
            "target_decoded_field_counts": {"chat": 1, "gift": 1, "viewer": 1},
            "page_loaded": True,
            "duration_seconds": 1800,
        }
        report = _feasibility_report(session, recorder)
        self.assertEqual(report["highest_level"], 6)
        self.assertTrue(report["viewer_member_metadata_decoded"])
        self.assertFalse(report["level_8_viewer_or_member"])
        session["tts_integration_status"] = "PASS"
        report = _feasibility_report(session, recorder)
        self.assertEqual(report["highest_level"], 8)

    def test_auxiliary_tiktok_socket_does_not_pass_live_transport_gate(self) -> None:
        from collections import Counter
        from types import SimpleNamespace

        recorder = SimpleNamespace(
            method_counts=Counter(),
            decoded_field_counts=Counter(),
            binary_frame_count=0,
        )
        session = {
            "live_preflight": {"status": "LIVE_CONFIRMED"},
            "candidate_websocket_open_count": 9,
            "target_live_websocket_open_count": 0,
            "target_binary_frame_count": 0,
            "target_method_counts": {},
            "target_decoded_field_counts": {},
            "page_loaded": True,
            "duration_seconds": 600,
        }
        report = _feasibility_report(session, recorder)
        self.assertEqual(report["decision_gate_a"], "INCOMPLETE")
        self.assertFalse(report["level_2_live_and_socket"])
        self.assertEqual(report["highest_level"], 1)

    def test_chat_normalization_extracts_minimum_fields(self) -> None:
        event = SimpleNamespace(
            common=SimpleNamespace(create_time=1_800_000_000_000, msg_id=123, room_id=456),
            user=SimpleNamespace(id_str="42", unique_id="viewer_42", nickname="Viewer"),
            content="TEST_CHAT_927",
            content_language="en",
        )
        normalized = normalize_proto_event(
            "WebcastChatMessage", event, received_at_ms=1_800_000_000_100
        )
        self.assertEqual(normalized["type"], "chat")
        self.assertEqual(normalized["user_id"], "42")
        self.assertEqual(normalized["unique_id"], "viewer_42")
        self.assertEqual(normalized["nickname"], "Viewer")
        self.assertEqual(normalized["comment"], "TEST_CHAT_927")
        self.assertEqual(normalized["msg_id"], "123")

    def test_gift_streak_intermediate_is_not_counted_and_completion_is(self) -> None:
        common = SimpleNamespace(create_time=1_800_000_000_000, msg_id=567, room_id=456)
        user = SimpleNamespace(id_str="42", unique_id="viewer_42", nickname="Viewer")
        gift = SimpleNamespace(type=1, id=13, name="Rose", diamond_count=1)
        intermediate = normalize_proto_event(
            "WebcastGiftMessage",
            SimpleNamespace(common=common, user=user, gift=gift, gift_id=13,
                             repeat_count=4, repeat_end=False, order_id="order-1"),
            received_at_ms=1_800_000_000_100,
        )
        completed = normalize_proto_event(
            "WebcastGiftMessage",
            SimpleNamespace(common=common, user=user, gift=gift, gift_id=13,
                             repeat_count=10, repeat_end=True, order_id="order-1"),
            received_at_ms=1_800_000_000_200,
        )
        self.assertFalse(intermediate["counted"])
        self.assertEqual(intermediate["diamond_total"], 0)
        self.assertTrue(completed["counted"])
        self.assertEqual(completed["gift_name"], "Rose")
        self.assertEqual(completed["repeat_count"], 10)
        self.assertEqual(completed["diamond_total"], 10)

    def test_gift_normalization_preserves_exact_raw_name_and_adds_catalog_fields(self) -> None:
        exact_raw_name = "Rose 🌹 "
        normalized = normalize_proto_event(
            "WebcastGiftMessage",
            SimpleNamespace(
                common=SimpleNamespace(create_time=1_800_000_000_000, msg_id=568, room_id=456),
                user=SimpleNamespace(id_str="42", unique_id="viewer_42", nickname="Viewer"),
                gift=SimpleNamespace(type=0, id=5655, name=exact_raw_name, diamond_count=1),
                gift_id=5655,
                repeat_count=1,
                repeat_end=False,
            ),
            received_at_ms=1_800_000_000_100,
        )
        self.assertEqual(normalized["gift_name"], exact_raw_name)
        self.assertEqual(normalized["gift_name_original"], exact_raw_name)
        self.assertEqual(normalized["gift_name_en"], "Rose")
        self.assertEqual(normalized["gift_name_zh_display"], "玫瑰花")
        self.assertEqual(normalized["gift_name_zh_tts"], "玫瑰花")
        self.assertEqual(normalized["gift_catalog_diamond_count"], 1)
        self.assertIn("gift_name_resolution_ms", normalized["processing_timings_ms"])

    def test_unknown_gift_normalization_keeps_raw_name_and_marks_review(self) -> None:
        exact_raw_name = " New 🌹 Gift "
        normalized = normalize_proto_event(
            "WebcastGiftMessage",
            SimpleNamespace(
                common=SimpleNamespace(create_time=1_800_000_000_000, msg_id=569, room_id=456),
                user=SimpleNamespace(id_str="42", unique_id="viewer_42", nickname="Viewer"),
                gift=SimpleNamespace(type=0, id=99999999, name=exact_raw_name, diamond_count=7),
                gift_id=99999999,
                repeat_count=1,
                repeat_end=False,
            ),
        )
        self.assertEqual(normalized["gift_name"], exact_raw_name)
        self.assertEqual(normalized["gift_catalog_status"], "needs_review")
        self.assertIsNone(normalized["gift_name_zh_tts"])
        self.assertEqual(normalized["diamond_per_gift"], 7)

    def test_viewer_member_and_like_normalization(self) -> None:
        common = SimpleNamespace(create_time=1_800_000_000_000, msg_id=901, room_id=456)
        user = SimpleNamespace(id_str="42", unique_id="viewer_42", nickname="Viewer")
        viewer = normalize_proto_event(
            "WebcastRoomUserSeqMessage",
            SimpleNamespace(common=common, total_user=321),
            received_at_ms=1_800_000_000_100,
        )
        member = normalize_proto_event(
            "WebcastMemberMessage",
            SimpleNamespace(common=common, user=user, action=1, enter_type=2),
            received_at_ms=1_800_000_000_100,
        )
        like = normalize_proto_event(
            "WebcastLikeMessage",
            SimpleNamespace(common=common, user=user, count=3, total=99),
            received_at_ms=1_800_000_000_100,
        )
        self.assertEqual(viewer["viewer_count"], 321)
        self.assertEqual(viewer["total_user_count"], 321)
        self.assertEqual(member["type"], "member")
        self.assertEqual(member["unique_id"], "viewer_42")
        self.assertEqual(like["like_count"], 3)
        self.assertEqual(like["total_likes"], 99)

    def test_viewer_uses_current_total_not_cumulative_total_user(self) -> None:
        common = SimpleNamespace(create_time=1_800_000_000, msg_id=903, room_id=456)
        viewer = normalize_proto_event(
            "WebcastRoomUserSeqMessage",
            SimpleNamespace(common=common, total=292, total_user=30_395),
            received_at_ms=1_800_000_100_000,
        )

        self.assertEqual(viewer["viewer_count"], 292)
        self.assertEqual(viewer["total_user_count"], 30_395)

    def test_member_badges_and_viewer_rankings_are_normalized(self) -> None:
        common = SimpleNamespace(create_time=1_800_000_000_000, msg_id=902, room_id=456)
        badge = lambda scene, level: SimpleNamespace(
            scene_type=SimpleNamespace(name=scene),
            privilege_log_extra=SimpleNamespace(level=str(level)),
        )
        user = SimpleNamespace(
            id_str="",
            id=0,
            display_id="viewer_42",
            nickname="Viewer",
            badge_list=[badge("BADGE_SCENE_TYPE_FANS", 7), badge("USER_GRADE", 32)],
        )
        identity = SimpleNamespace(is_subscriber_of_anchor=True)
        member = normalize_proto_event(
            "WebcastMemberMessage",
            SimpleNamespace(common=common, user=user, user_id=42, user_identity=identity,
                             action=1, enter_type=2, is_top_user=True,
                             top_user_no=1, rank_score=987),
            received_at_ms=1_800_000_000_100,
        )
        viewer = normalize_proto_event(
            "WebcastRoomUserSeqMessage",
            SimpleNamespace(
                common=common,
                total_user=321,
                ranks=[SimpleNamespace(rank=1, score=1234, delta=5, user=user)],
            ),
            received_at_ms=1_800_000_000_100,
        )

        self.assertEqual(member["user_id"], "42")
        self.assertEqual(member["fan_club_level"], 7)
        self.assertEqual(member["user_grade_level"], 32)
        self.assertTrue(member["is_subscriber_of_anchor"])
        self.assertTrue(member["is_top_user"])
        self.assertEqual(member["top_user_no"], 1)
        self.assertEqual(member["rank_score"], 987)
        self.assertEqual(
            member["badges"],
            [{"scene": "FANS", "level": 7}, {"scene": "USER_GRADE", "level": 32}],
        )
        self.assertEqual(viewer["ranks"][0]["rank"], 1)
        self.assertEqual(viewer["ranks"][0]["score"], 1234)
        self.assertEqual(viewer["ranks"][0]["user"]["unique_id"], "viewer_42")

    def test_frame_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session_dir = Path(temporary_directory) / "session"
            recorder = FrameRecorder(session_dir, max_frames=1, save_raw_frames=True)
            payload = synthetic_push_frame("WebcastChatMessage")
            reached_limit = recorder.record(
                "request-1",
                "wss://webcast-ws.tiktok.com/ws?token=private",
                2,
                base64.b64encode(payload).decode("ascii"),
            )
            limit_still_decodes_events = recorder.record(
                "request-1",
                "wss://webcast-ws.tiktok.com/ws?token=private",
                2,
                base64.b64encode(payload).decode("ascii"),
            )
            recorder.close()

            self.assertTrue(reached_limit)
            self.assertTrue(limit_still_decodes_events)
            self.assertEqual(recorder.frame_count, 2)
            self.assertEqual(recorder.binary_frame_count, 2)
            self.assertEqual(recorder.saved_raw_frame_count, 1)
            self.assertEqual(recorder.method_counts["WebcastChatMessage"], 2)
            self.assertEqual(
                (session_dir / "frames" / "frame_000001.bin").read_bytes(),
                payload,
            )
            rows = [
                json.loads(line)
                for line in (session_dir / "frames.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["file"], "frames/frame_000001.bin")
            self.assertNotIn("token", json.dumps(rows[0]))
            event_rows = [
                json.loads(line)
                for line in (session_dir / "events.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(event_rows), 2)
            self.assertEqual([row["type"] for row in event_rows], ["chat", "chat"])
            self.assertTrue(all(row["source"] == "browser_network" for row in event_rows))
            method_rows = [
                json.loads(line)
                for line in (session_dir / "methods.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(method_rows[0]["methods"], ["WebcastChatMessage"])


if __name__ == "__main__":
    unittest.main()
