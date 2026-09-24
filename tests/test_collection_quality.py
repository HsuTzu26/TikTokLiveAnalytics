import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.analytics import build_health_report


class CollectionQualityTests(unittest.TestCase):
    def test_connected_coverage_and_stall_are_observed_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'session.json').write_text(json.dumps({
                'username': 'demo', 'status': 'running',
                'collector_started_at_utc': '2026-09-20T00:00:00+00:00',
                'last_received_at_local': '2026-09-20T08:03:00+08:00',
                'connection_history': [
                    {'connected_at_utc': '2026-09-20T00:00:00+00:00', 'disconnected_at_utc': '2026-09-20T00:02:00+00:00'},
                    {'connected_at_utc': '2026-09-20T00:01:00+00:00', 'disconnected_at_utc': '2026-09-20T00:03:00+00:00'},
                ],
            }), encoding='utf-8')
            (folder / 'events.ndjson').write_text(json.dumps({
                'type': 'chat', 'timestamp_ms': 1789862580000,
            }) + '\n', encoding='utf-8')
            report = build_health_report(folder, checked_at=datetime(2026, 9, 20, 0, 10, tzinfo=timezone.utc), stale_after_seconds=300)
            self.assertEqual(report['observed_window_seconds'], 600)
            self.assertEqual(report['connected_seconds_in_window'], 180)
            self.assertEqual(report['observed_connection_coverage'], 0.3)
            self.assertEqual(report['last_event_age_seconds'], 420)
            self.assertTrue(report['event_stalled'])
            self.assertEqual(report['coverage_scope'], 'collector_observation_window_not_full_live')

    def test_empty_or_ended_session_not_false_stalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'session.json').write_text('{"status":"offline_confirmed"}', encoding='utf-8')
            report = build_health_report(folder, checked_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
            self.assertIsNone(report['observed_connection_coverage'])
            self.assertFalse(report['event_stalled'])

    def test_running_without_first_event_warns_after_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'session.json').write_text(json.dumps({
                'status': 'running', 'collector_started_at_utc': '2026-09-20T00:00:00+00:00',
            }), encoding='utf-8')
            report = build_health_report(folder, checked_at=datetime(2026, 9, 20, 0, 6, tzinfo=timezone.utc))
            self.assertTrue(report['event_stalled'])
            self.assertEqual(report['last_event_age_seconds'], 360)
