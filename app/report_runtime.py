"""Refresh reporting dependencies in Streamlit's long-lived Python process."""

import importlib


def load_reports_ui():
    """Reload dependencies first, then rebind the UI's imported functions."""
    importlib.invalidate_caches()
    for name in (
        "src.reports",
        "src.streamer_insights",
        "src.report_html",
        "app.reports_ui",
    ):
        module = importlib.reload(importlib.import_module(name))
    return module
