"""Pluggable Edge TTS synthesis and system-audio playback adapters."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path


class EdgeTTSBackend:
    async def synthesize(self, text: str, voice: str, rate_percent: int) -> Path:
        try:
            import edge_tts
        except ImportError as error:
            raise RuntimeError("edge-tts is not installed; install requirements.txt") from error

        with tempfile.NamedTemporaryFile(prefix="tiktok-live-tts-", suffix=".mp3", delete=False) as handle:
            output = Path(handle.name)
        rate = f"{rate_percent:+d}%"
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                receive_timeout=30,
            )
            await communicate.save(str(output))
            return output
        except Exception:
            output.unlink(missing_ok=True)
            raise


class PygameAudioPlayer:
    def __init__(self):
        self._pygame = None

    def initialize(self) -> None:
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            import pygame
        except ImportError as error:
            raise RuntimeError("pygame is not installed; install requirements.txt") from error
        self._pygame = pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()

    async def play(self, audio_path: Path, volume: int) -> None:
        if self._pygame is None:
            self.initialize()
        music = self._pygame.mixer.music
        music.load(str(audio_path))
        music.set_volume(max(0, min(100, int(volume))) / 100)
        music.play()
        while music.get_busy():
            await asyncio.sleep(0.05)
        music.unload()

    def stop(self) -> None:
        if self._pygame is None:
            return
        try:
            self._pygame.mixer.music.stop()
            self._pygame.mixer.music.unload()
        except Exception:
            pass

    def close(self) -> None:
        if self._pygame is not None:
            try:
                self.stop()
                self._pygame.mixer.quit()
            finally:
                self._pygame = None
