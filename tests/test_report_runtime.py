import inspect
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

import app.reports_ui as reports_ui
import src.report_html as report_html
from app.report_runtime import load_reports_ui


class ReportRuntimeTests(unittest.TestCase):
    def test_reload_replaces_cached_old_signature_and_ui_binding(self):
        def old_builder(username, start, end, report, charts, notes, actions, tables):
            raise AssertionError("Stale report builder must not be used")

        with patch.object(report_html, "build_streamer_html", old_builder), patch.object(
            reports_ui, "build_streamer_html", old_builder
        ):
            ui = load_reports_ui()
            self.assertIs(ui.build_streamer_html, report_html.build_streamer_html)
            self.assertEqual(len(inspect.signature(ui.build_streamer_html).parameters), 7)
            events = pd.DataFrame([{
                "user": "a", "room_id": "room", "chat": 1, "gifts": 1,
                "diamonds": 10, "date": "2026-09-11", "shares": 0,
                "follows": 0, "subscribes": 0, "viewer_count": 12,
                "time": "2026-09-11",
            }])
            html = ui.build_streamer_html(
                "test", date(2026, 9, 11), date(2026, 9, 17),
                {"events": events, "quality": {}}, [], [], [],
            )
            self.assertIn("<h2>參與</h2>", html)
        # patch restores original bindings; refresh once more for subsequent tests.
        load_reports_ui()
