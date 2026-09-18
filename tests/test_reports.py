import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from src.reports import build_report, period_bounds

class ReportsTests(unittest.TestCase):
    def test_period_bounds(self):
        self.assertEqual(period_bounds('Week', date(2026, 9, 16)), (date(2026, 9, 14), date(2026, 9, 20)))
        self.assertEqual(period_bounds('Month', date(2024, 2, 10))[1], date(2024, 2, 29))

    def test_overlap_and_taipei_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            event = {'type': 'viewer', 'timestamp_local': '2026-09-10T00:00:00+08:00', 'session_id': 'original', 'seq': 1, 'room_id': 'room', 'viewer_count': 12}
            for name, row in [('original', event), ('daily', {**event, 'session_id': 'daily', 'source_session_id': 'original', 'source_seq': 1, 'seq': 55})]:
                p = root / name
                p.mkdir()
                (p / 'session.json').write_text(json.dumps({'username': 'test'}))
                (p / 'events.ndjson').write_text(json.dumps(row) + '\n')
            report = build_report(root, 'test', date(2026, 9, 10), date(2026, 9, 10))
            self.assertEqual(len(report['events']), 1)
            self.assertEqual(report['rooms'].iloc[0].peak_viewers, 12)
            self.assertEqual(report['quality']['overlapping_records_skipped'], 1)
            self.assertTrue(build_report(root, 'test', date(2026, 9, 9), date(2026, 9, 9))['events'].empty)

    def test_cross_midnight_gift_counting(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'session'
            p.mkdir()
            (p / 'session.json').write_text(json.dumps({'username': 'test'}))
            rows = [
                {'type': 'gift', 'timestamp_utc': '2026-09-10T15:59:00Z', 'room_id': 'same', 'seq': 1, 'counted': False, 'diamond_total': 100},
                {'type': 'gift', 'timestamp_utc': '2026-09-10T16:01:00Z', 'room_id': 'same', 'seq': 2, 'counted': True, 'diamond_total': 100},
            ]
            (p / 'events.ndjson').write_text('\n'.join(json.dumps(row) for row in rows))
            report = build_report(Path(folder), 'test', date(2026, 9, 10), date(2026, 9, 11))
            self.assertEqual(len(report['rooms']), 1)
            self.assertEqual(len(report['daily']), 2)
            self.assertEqual(report['events'].diamonds.sum(), 100)

if __name__ == '__main__':
    unittest.main()
