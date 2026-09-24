import unittest
from datetime import datetime, timezone
import json
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.quota_policy import classify_limit, cooldown_seconds, pause_until, paused
from src.watcher import Watcher, probe_live


class QuotaPolicyTests(unittest.TestCase):
    def test_edge_connection_alone_does_not_confirm_live(self):
        class EdgeOnly:
            def __init__(self, *args, **kwargs):
                self.handlers = {}

            def on(self, event):
                def register(callback):
                    self.handlers[event] = callback
                    return callback
                return register

            async def run(self):
                self.handlers['connected']({})

            def stop(self):
                pass

        with patch('src.watcher.TikTokLive', EdgeOnly):
            result = asyncio.run(probe_live('demo', 1))
        self.assertTrue(result['connected'])
        self.assertFalse(result['confirmed_live'])
        self.assertEqual(result['error'], 'probe_no_room_info')

    def test_classify_server_messages(self):
        self.assertEqual(classify_limit('received 4429 Daily Demo Limit Reached. Upgrade Required.'), 'daily')
        self.assertEqual(classify_limit('4429 Sandbox allows 60 WebSocket connections per hour'), 'hourly')
        self.assertEqual(classify_limit('ws_credentials failed: HTTP Error 429: Too Many Requests'), 'generic')
        self.assertEqual(classify_limit('4429 Evicted - newer connection arrived (FIFO)'), 'generic')
        self.assertIsNone(classify_limit('probe_timeout'))
        self.assertIsNone(classify_limit('is not currently live'))

    def test_cooldown_persists_and_never_shrinks(self):
        start = datetime(2026, 9, 20, tzinfo=timezone.utc)
        hourly = pause_until(None, 'hourly', start)
        self.assertTrue(paused(hourly, start))
        self.assertFalse(paused(hourly, start.replace(hour=1)))
        self.assertEqual(pause_until(hourly, 'generic', start), hourly)
        self.assertEqual(cooldown_seconds('daily'), 86400)

    def test_watcher_paces_probes_and_restores_quota_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'watchlist.json'
            config.write_text(json.dumps({'streamers': [
                {'username': 'a'}, {'username': 'b'}, {'username': 'c'},
            ]}), encoding='utf-8')
            watcher = Watcher(config)
            enabled = watcher.config['streamers']
            start = datetime(2026, 9, 20, tzinfo=timezone.utc)
            self.assertEqual(watcher.select_probe_target(enabled, start)['username'], 'a')
            self.assertIsNone(watcher.select_probe_target(enabled, start))
            later = start.replace(minute=2)
            self.assertEqual(watcher.select_probe_target(enabled, later)['username'], 'b')
            watcher.quota_pause_until_utc = pause_until(None, 'hourly', later)
            watcher.save_state()
            restored = Watcher(config)
            self.assertIsNone(restored.select_probe_target(enabled, later.replace(minute=10)))

    def test_probe_quota_error_pauses_without_starting_collector(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'watchlist.json'
            config.write_text(json.dumps({'streamers': [{'username': 'demo'}]}), encoding='utf-8')
            watcher = Watcher(config)
            watcher.apply_probe({
                'username': 'demo', 'checked_at_utc': '2026-09-20T00:00:00+00:00',
                'error': 'HTTP Error 429: Too Many Requests', 'confirmed_live': False,
            })
            self.assertEqual(watcher.states['demo']['status'], 'quota_paused')
            self.assertTrue(paused(watcher.quota_pause_until_utc))
            self.assertFalse(watcher.processes)

    def test_fallback_needs_repeated_non_quota_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'watchlist.json'
            config.write_text(json.dumps({'streamers': [{'username': 'demo'}]}), encoding='utf-8')
            watcher = Watcher(config)
            self.assertFalse(watcher.should_start_probe_error_fallback('demo', 'probe_timeout'))
            watcher.states['demo']['consecutive_misses'] = 2
            self.assertFalse(watcher.should_start_probe_error_fallback('demo', 'probe_no_room_info'))
            self.assertFalse(watcher.should_start_probe_error_fallback('demo', 'HTTP Error 429'))
            self.assertTrue(watcher.should_start_probe_error_fallback('demo', 'probe_timeout'))
