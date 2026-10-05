"""Download and safely unpack the official int8 Kokoro zh/en evaluation model."""
from __future__ import annotations

import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "data" / "tts_backend_eval" / "models"
ARCHIVE = MODEL_ROOT / "kokoro-int8-multi-lang-v1_1.tar.bz2"
MODEL_DIR = MODEL_ROOT / "kokoro-int8-multi-lang-v1_1"
URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    "kokoro-int8-multi-lang-v1_1.tar.bz2"
)


def main() -> int:
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    model_names = ("model.onnx", "model.int8.onnx")
    required = ("voices.bin", "tokens.txt", "espeak-ng-data")
    if any((MODEL_DIR / name).is_file() for name in model_names) and all(
        (MODEL_DIR / name).exists() for name in required
    ):
        print(f"[MODEL READY] {MODEL_DIR}")
        return 0
    if MODEL_DIR.exists():
        raise RuntimeError(
            f"Incomplete model directory already exists: {MODEL_DIR}. "
            "Move or remove it manually before downloading again."
        )

    partial = ARCHIVE.with_suffix(ARCHIVE.suffix + ".download")
    try:
        if not ARCHIVE.is_file():
            request = urllib.request.Request(URL, headers={"User-Agent": "TikTokLiveAnalytics-TTS-eval"})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
                total = int(response.headers.get("Content-Length", "0") or 0)
                received = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    if total:
                        print(f"\r[DOWNLOAD] {received * 100 / total:.1f}%", end="", flush=True)
            partial.replace(ARCHIVE)
            print(f"\n[DOWNLOADED] {ARCHIVE.stat().st_size:,} bytes")

        with tempfile.TemporaryDirectory(prefix="_extracting_kokoro_", dir=MODEL_ROOT) as temporary:
            extract_root = Path(temporary)
            root_resolved = extract_root.resolve()
            bundle = tarfile.open(ARCHIVE, mode="r:bz2")
            with bundle:
                members = bundle.getmembers()
                for member in members:
                    destination = (extract_root / member.name).resolve()
                    if not destination.is_relative_to(root_resolved):
                        raise RuntimeError(f"unsafe archive path: {member.name}")
                    if member.issym() or member.islnk():
                        raise RuntimeError(f"archive links are not accepted: {member.name}")
                bundle.extractall(extract_root, members=members, filter="data")

            model_files = [
                path
                for path in extract_root.rglob("*")
                if path.name in model_names and path.is_file()
            ]
            if len(model_files) != 1:
                raise RuntimeError(f"expected one model.onnx in archive, found {len(model_files)}")
            extracted = model_files[0].parent.resolve()
            if not extracted.is_relative_to(root_resolved):
                raise RuntimeError(f"extracted model is outside temporary root: {extracted}")
            shutil.move(str(extracted), str(MODEL_DIR))

        if not MODEL_DIR.resolve().is_relative_to(MODEL_ROOT.resolve()):
            raise RuntimeError(f"model path escaped evaluation model directory: {MODEL_DIR}")

        missing = [name for name in required if not (MODEL_DIR / name).exists()]
        if missing:
            raise RuntimeError(f"model package missing required files: {', '.join(missing)}")
        ARCHIVE.unlink(missing_ok=True)
        print(f"[MODEL READY] {MODEL_DIR}")
        print(f"[MODEL SIZE] {sum(p.stat().st_size for p in MODEL_DIR.rglob('*') if p.is_file()):,} bytes")
        return 0
    except Exception:
        partial.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
