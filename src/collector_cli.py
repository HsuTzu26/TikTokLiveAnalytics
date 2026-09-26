"""Command-line options for the TikTok LIVE collector."""
from __future__ import annotations

import argparse


DEFAULT_TIMEZONE = "Asia/Taipei"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TikTok LIVE long-session analytics collector v0.3"
    )
    parser.add_argument("username", help="TikTok LIVE username, with or without @")
    parser.add_argument(
        "--output-root",
        default="data/raw",
        help="Root directory for captured sessions (default: data/raw)",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Display timezone metadata (default: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--max-reconnect-attempts",
        type=int,
        default=20,
        help="Automatic reconnect attempts (default: 20)",
    )
    parser.add_argument(
        "--offline-confirmations",
        type=int,
        default=3,
        help="End the session after this many consecutive authoritative offline responses (default: 3)",
    )
    parser.add_argument(
        "--snapshot-seconds",
        type=int,
        default=300,
        help="Room/ranking snapshot interval; 0 disables snapshots (default: 300)",
    )
    parser.add_argument(
        "--capture-raw",
        action="store_true",
        help="Write complete SDK payloads to raw_events.ndjson for debugging (off by default).",
    )
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=5 * 1024 * 1024,
        help="Rotate the collector log after this many bytes (default: 5242880; 0 disables rotation).",
    )
    parser.add_argument(
        "--log-backups",
        type=int,
        default=3,
        help="Number of rotated collector log files to keep (default: 3).",
    )
    return parser
