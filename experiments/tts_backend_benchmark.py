"""Offline benchmark for candidate local TTS backends.

This experiment does not connect to TikTok, play audio, or touch the production
TTS worker. Models stay loaded for a run so the measurements represent a warm
live process rather than repeated process startup.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import re
import sys
import time
import wave
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = ROOT / "data" / "tts_backend_eval" / "models" / "kokoro-int8-multi-lang-v1_1"
OUTPUT_ROOT = ROOT / "data" / "tts_backend_eval" / "runs"

# Full utterances are deliberate: a bilingual Gift line should be synthesized
# as one unit so the benchmark can expose unnatural segment joins or pauses.
SAMPLES = (
    ("traditional_chinese", "今天直播很熱鬧，謝謝大家一直陪著我。"),
    ("english", "Thank you so much for the gifts. I really appreciate you."),
    ("mixed_username", "@ChloeのUU 太好了今天一堆人幫忙承擔，thank you!"),
    ("gift_normal", "Chloe 送出玫瑰花三朵，謝謝支持！"),
    ("gift_mixed", "@Allen 送出滷肉飯 x12，thank you for the support!"),
    ("emoji_cleaned", "好好笑，謝謝大家。"),
)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return ordered[index]


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower() or "sample"


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
    if rate <= 0:
        raise RuntimeError(f"invalid WAV sample rate: {path}")
    return frames / rate


class KokoroBackend:
    def __init__(self, model_dir: Path, voice_id: int, threads: int):
        import sherpa_onnx

        model_path = model_dir / "model.onnx"
        if not model_path.is_file():
            model_path = model_dir / "model.int8.onnx"
        required = ("voices.bin", "tokens.txt", "espeak-ng-data")
        missing = [name for name in required if not (model_dir / name).exists()]
        if not model_path.is_file():
            missing.insert(0, "model.onnx or model.int8.onnx")
        if missing:
            raise FileNotFoundError(
                f"Kokoro model files missing from {model_dir}: {', '.join(missing)}"
            )

        lexicons = [
            model_dir / "lexicon-us-en.txt",
            model_dir / "lexicon-zh.txt",
        ]
        lexicon = ",".join(str(path) for path in lexicons if path.is_file())
        rule_files = [
            model_dir / "phone-zh.fst",
            model_dir / "date-zh.fst",
            model_dir / "number-zh.fst",
        ]
        rule_fsts = ",".join(str(path) for path in rule_files if path.is_file())

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(model_path),
                    voices=str(model_dir / "voices.bin"),
                    tokens=str(model_dir / "tokens.txt"),
                    data_dir=str(model_dir / "espeak-ng-data"),
                    lexicon=lexicon,
                ),
                num_threads=threads,
            ),
            rule_fsts=rule_fsts,
        )
        if not config.validate():
            raise RuntimeError(f"Invalid Kokoro model configuration: {model_dir}")
        self.engine = sherpa_onnx.OfflineTts(config)
        self.voice_id = voice_id

    def synthesize(self, text: str, output_path: Path, speed: float) -> float:
        import soundfile

        result = self.engine.generate(text, sid=self.voice_id, speed=speed)
        soundfile.write(output_path, result.samples, result.sample_rate)
        return len(result.samples) / result.sample_rate


class MeloBackend:
    def __init__(self, device: str):
        from melo.api import TTS

        self.engine = TTS(language="ZH", device=device)
        speaker_ids = self.engine.hps.data.spk2id
        if "ZH" not in speaker_ids:
            raise RuntimeError(f"MeloTTS Chinese speaker not found; available={list(speaker_ids)}")
        self.speaker_id = speaker_ids["ZH"]

    def synthesize(self, text: str, output_path: Path, speed: float) -> float:
        self.engine.tts_to_file(
            text,
            self.speaker_id,
            str(output_path),
            speed=speed,
        )
        return wav_duration_seconds(output_path)


def make_backend(args):
    if args.engine == "kokoro":
        return KokoroBackend(args.model_dir, args.voice_id, args.threads)
    return MeloBackend(args.device)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("kokoro", "melo"), required=True)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--voice-id", type=int, default=48, help="Kokoro speaker ID")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu", help="MeloTTS device")
    parser.add_argument("--threads", type=int, default=4, help="Kokoro CPU inference threads")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.iterations < 1 or args.iterations > 100:
        parser.error("--iterations must be between 1 and 100")
    if args.warmup < 0 or args.warmup > 10:
        parser.error("--warmup must be between 0 and 10")
    if args.threads < 1 or args.threads > 32:
        parser.error("--threads must be between 1 and 32")
    if args.speed <= 0 or args.speed > 2.0:
        parser.error("--speed must be greater than 0 and at most 2.0")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (args.output_dir or OUTPUT_ROOT / f"{run_id}_{args.engine}").resolve()
    audio_dir = run_dir / "audio"
    scratch_dir = run_dir / "scratch"
    audio_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print(f"[TTS BENCH] engine={args.engine} device={args.device if args.engine == 'melo' else 'cpu'}")
    print("[TTS BENCH] Loading model; no audio will be played.", flush=True)
    load_started = time.perf_counter()
    backend = make_backend(args)
    model_load_ms = (time.perf_counter() - load_started) * 1000
    print(f"[MODEL READY] load_ms={model_load_ms:.1f}", flush=True)

    first_text = SAMPLES[0][1]
    for warmup_index in range(args.warmup):
        warmup_path = scratch_dir / f"warmup_{warmup_index + 1:02d}.wav"
        backend.synthesize(first_text, warmup_path, args.speed)
        warmup_path.unlink(missing_ok=True)

    timings: dict[str, list[float]] = defaultdict(list)
    rtfs: dict[str, list[float]] = defaultdict(list)
    records = []
    sequence = 0
    results_path = run_dir / "results.ndjson"
    with results_path.open("w", encoding="utf-8", newline="\n") as results_file:
        for iteration in range(1, args.iterations + 1):
            for case_name, text in SAMPLES:
                sequence += 1
                sample_slug = safe_slug(case_name)
                keep_path = audio_dir / f"{sample_slug}.wav"
                target_path = (
                    keep_path
                    if iteration == 1
                    else scratch_dir / f"{sample_slug}_{iteration:03d}.wav"
                )
                synthesis_started = time.perf_counter()
                audio_seconds = backend.synthesize(text, target_path, args.speed)
                synthesis_ms = (time.perf_counter() - synthesis_started) * 1000
                rtf = (synthesis_ms / 1000) / audio_seconds if audio_seconds else None
                timings[case_name].append(synthesis_ms)
                if rtf is not None:
                    rtfs[case_name].append(rtf)
                record = {
                    "sequence": sequence,
                    "case": case_name,
                    "iteration": iteration,
                    "characters": len(text),
                    "synthesis_ms": round(synthesis_ms, 3),
                    "audio_seconds": round(audio_seconds, 3),
                    "realtime_factor": round(rtf, 4) if rtf is not None else None,
                    "sample_file": str(keep_path.relative_to(run_dir)) if iteration == 1 else None,
                }
                records.append(record)
                results_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                results_file.flush()
                if target_path != keep_path:
                    target_path.unlink(missing_ok=True)
                print(
                    f"[SYNTH {sequence:03d}] case={case_name} ms={synthesis_ms:.0f} "
                    f"audio={audio_seconds:.2f}s rtf={rtf:.3f}",
                    flush=True,
                )

    summary = {
        "engine": args.engine,
        "device": args.device if args.engine == "melo" else "cpu",
        "model_load_ms": round(model_load_ms, 3),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "logical_cpu_count": __import__("os").cpu_count(),
        "iterations": args.iterations,
        "warmup_count": args.warmup,
        "speed": args.speed,
        "utterance_count": len(records),
        "results_file": "results.ndjson",
        "summary_by_case": {
            case_name: {
                "count": len(timings[case_name]),
                "synthesis_p50_ms": round(percentile(timings[case_name], 50), 3),
                "synthesis_p95_ms": round(percentile(timings[case_name], 95), 3),
                "rtf_p50": round(percentile(rtfs[case_name], 50), 4),
                "rtf_p95": round(percentile(rtfs[case_name], 95), 4),
            }
            for case_name in timings
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for path in scratch_dir.iterdir():
        path.unlink(missing_ok=True)
    scratch_dir.rmdir()
    print(f"[SUMMARY] {run_dir / 'summary.json'}")
    print(f"[SAMPLES] {audio_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
