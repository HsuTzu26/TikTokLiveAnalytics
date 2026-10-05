"""Compatibility CLI for the CDP Browser Network Provider."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.providers import browser_network as _browser_network  # noqa: E402

globals().update(
    {
        name: value
        for name, value in vars(_browser_network).items()
        if not name.startswith("__")
    }
)

if __name__ == "__main__":
    _browser_network.main()
