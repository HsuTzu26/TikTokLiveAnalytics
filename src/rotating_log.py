"""Small synchronous text log writer with bounded on-disk history."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO


class RotatingTextLog:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 3,
    ):
        self.path = Path(path)
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = min(20, max(0, int(backup_count)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = self.path.open("a", encoding="utf-8", buffering=1)
        self._current_bytes = self.path.stat().st_size
        if self.max_bytes and self._current_bytes >= self.max_bytes:
            self._rotate()

    def write(self, text: str) -> int:
        written = self._file.write(text)
        self._file.flush()
        self._current_bytes += len(text.encode("utf-8"))
        if self.max_bytes and self._current_bytes >= self.max_bytes:
            self._rotate()
        return written

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def _rotate(self) -> None:
        self._file.close()
        try:
            if self.backup_count == 0:
                self.path.unlink(missing_ok=True)
                self._current_bytes = 0
                return

            oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
            oldest.unlink(missing_ok=True)
            for index in range(self.backup_count - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    os.replace(source, source.with_name(f"{self.path.name}.{index + 1}"))
            if self.path.exists():
                os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            self._current_bytes = 0
        finally:
            self._file = self.path.open("a", encoding="utf-8", buffering=1)


def append_rotating_text(
    path: Path,
    text: str,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    writer = RotatingTextLog(
        path,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    try:
        writer.write(text)
    finally:
        writer.close()
