import unittest
from datetime import date
import pandas as pd
from app.reports_ui import readable_table, report_insights

class ReadabilityTests(unittest.TestCase):
    def test_table_labels_and_rounding(self):
        frame = pd.DataFrame([{'sample_avg_viewers': 12.345, 'first_event': '2026-09-10T01:02:03+08:00'}])
        table = readable_table(frame)
        self.assertEqual(table.iloc[0]['平均同時觀看（採樣）'], 12.3)
        self.assertEqual(table.iloc[0]['首筆事件時間'], '2026-09-10 01:02:03')
        self.assertEqual(frame.iloc[0].sample_avg_viewers, 12.345)

    def test_insights_concentration(self):
        report = {'daily': pd.DataFrame([{'date':'2026-09-10','peak_viewers':75}]), 'gifters': pd.DataFrame([{'diamonds':75},{'diamonds':25}])}
        notes = report_insights(report, date(2026,9,10), date(2026,9,16))
        self.assertTrue(any('75.0%' in note for note in notes))
        self.assertTrue(any('7 天' in note for note in notes))

if __name__ == '__main__':
    unittest.main()
