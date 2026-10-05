"""Pluggable Edge TTS synthesis and system-audio playback adapters."""
from __future__ import annotations

import asyncio
from array import array
import base64
import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path


class EdgeTTSBackend:
    receive_timeout_seconds = 10

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
                receive_timeout=self.receive_timeout_seconds,
            )
            await communicate.save(str(output))
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("Edge TTS returned an empty audio file")
            return output
        except Exception:
            output.unlink(missing_ok=True)
            raise


class WindowsSystemSpeechBackend:
    """Offline Windows SAPI fallback for a Gift whose Edge audio failed.

    This uses the voices installed in Windows. It does not install voices or
    reach the network. A language-matched voice is required so a missing
    Traditional Chinese voice is reported as a fallback failure instead of
    silently reading Chinese text with an unrelated voice.
    """

    def __init__(self, retry_probe_after_seconds: float = 300.0):
        self.retry_probe_after_seconds = max(30.0, float(retry_probe_after_seconds))
        self.retry_after = 0.0

    async def synthesize(self, text: str, voice: str, rate_percent: int) -> Path:
        if time.monotonic() < self.retry_after:
            raise RuntimeError("Windows offline voice probe is cooling down after a previous failure")
        if os.name != "nt":
            raise RuntimeError("Windows System.Speech is only available on Windows")
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if not executable:
            self.retry_after = time.monotonic() + self.retry_probe_after_seconds
            raise RuntimeError("Windows PowerShell is unavailable")

        with tempfile.NamedTemporaryFile(
            prefix="tiktok-live-offline-tts-", suffix=".wav", delete=False
        ) as handle:
            output = Path(handle.name)
        output.unlink(missing_ok=True)

        request = {
            "text": str(text),
            "voice": str(voice),
            "rate_percent": int(rate_percent),
            "output": str(output),
        }
        encoded_request = base64.b64encode(
            json.dumps(request, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        script = r"""
$ErrorActionPreference = 'Stop'
$request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(([Console]::In.ReadToEnd().Trim()))) | ConvertFrom-Json
$synth = $null
try {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled })
    if ($voices.Count -eq 0) { throw 'No enabled Windows offline speech voices are installed' }
    $preferred = [regex]::Match([string]$request.voice, '^[a-zA-Z]{2}-[a-zA-Z]{2}').Value
    $language = $preferred.Split('-')[0]
    $selected = $voices | Where-Object { $_.VoiceInfo.Culture.Name -ieq $preferred } | Select-Object -First 1
    if (-not $selected) {
        $selected = $voices | Where-Object { $_.VoiceInfo.Culture.Name -like ($language + '-*') } | Select-Object -First 1
    }
    if (-not $selected) { throw ('No installed offline voice matches language ' + $language) }
    $synth.SelectVoice($selected.VoiceInfo.Name)
    $synth.Rate = [Math]::Max(-10, [Math]::Min(10, [int]([int]$request.rate_percent / 10)))
    $synth.SetOutputToWaveFile([string]$request.output)
    $synth.Speak([string]$request.text)
    $synth.SetOutputToNull()
    [Console]::Out.WriteLine('OK')
}
finally {
    if ($null -ne $synth) { $synth.Dispose() }
}
"""
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(encoded_request.encode("ascii")), timeout=45
                )
            except asyncio.TimeoutError as error:
                process.kill()
                await process.wait()
                raise RuntimeError("Windows offline speech synthesis timed out") from error
            if process.returncode != 0 or b"OK" not in stdout:
                raise RuntimeError("Windows offline speech synthesis failed or no matching voice is installed")
            if not output.is_file() or output.stat().st_size < 44:
                raise RuntimeError("Windows offline speech synthesis returned an empty WAV")
            with output.open("rb") as audio_file:
                header = audio_file.read(12)
            if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise RuntimeError("Windows offline speech synthesis returned an invalid WAV")
            return output
        except Exception:
            self.retry_after = time.monotonic() + self.retry_probe_after_seconds
            output.unlink(missing_ok=True)
            raise


class PygameAudioPlayer:
    def __init__(self):
        self._pygame = None
        self._effect_sounds = []

    def initialize(self) -> None:
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            import pygame
        except ImportError as error:
            raise RuntimeError("pygame is not installed; install requirements.txt") from error
        self._pygame = pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        if pygame.mixer.get_num_channels() < 16:
            pygame.mixer.set_num_channels(16)

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

    def play_effect(self, audio_path: Path, volume: int) -> bool:
        """Start a short sound on a mixed channel without blocking TTS order."""
        if self._pygame is None:
            self.initialize()
        pygame = self._pygame
        sound = pygame.mixer.Sound(str(audio_path))
        channel = pygame.mixer.find_channel()
        if channel is None:
            return False
        channel.set_volume(max(0, min(100, int(volume))) / 100)
        channel.play(sound)
        self._effect_sounds = [
            item for item in self._effect_sounds if item[0].get_busy()
        ]
        self._effect_sounds.append((channel, sound))
        return True

    def play_failure_alert(self, volume: int) -> bool:
        """Play a short local alert when Gift speech cannot be synthesized."""
        if self._pygame is None:
            self.initialize()
        pygame = self._pygame
        mixer = pygame.mixer.get_init()
        if not mixer:
            raise RuntimeError("pygame audio mixer is not initialized")
        sample_rate, sample_format, channels = mixer
        if sample_format != -16:
            raise RuntimeError(f"unsupported mixer sample format: {sample_format}")

        duration = 0.18
        sample_count = int(sample_rate * duration)
        samples = array("h")
        for index in range(sample_count):
            fade = min(1.0, index / max(1, int(sample_rate * 0.008)))
            fade *= min(1.0, (sample_count - index) / max(1, int(sample_rate * 0.035)))
            tone = math.sin(2 * math.pi * 880 * index / sample_rate)
            value = int(9000 * fade * tone)
            for _ in range(max(1, channels)):
                samples.append(value)
        if samples.itemsize != 2:
            raise RuntimeError("unsupported PCM sample width")

        sound = pygame.mixer.Sound(buffer=samples.tobytes())
        channel = pygame.mixer.find_channel()
        if channel is None:
            return False
        channel.set_volume(max(0, min(100, int(volume))) / 100)
        channel.play(sound)
        self._effect_sounds = [
            item for item in self._effect_sounds if item[0].get_busy()
        ]
        self._effect_sounds.append((channel, sound))
        return True

    def stop(self) -> None:
        if self._pygame is None:
            return
        try:
            self._pygame.mixer.music.stop()
            self._pygame.mixer.music.unload()
            for channel, _ in self._effect_sounds:
                channel.stop()
            self._effect_sounds.clear()
        except Exception:
            pass

    def close(self) -> None:
        if self._pygame is not None:
            try:
                self.stop()
                self._pygame.mixer.quit()
            finally:
                self._pygame = None
