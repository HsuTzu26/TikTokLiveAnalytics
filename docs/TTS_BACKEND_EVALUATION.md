# Free Local TTS Evaluation

This evaluation is isolated from the production TTS worker. It does not attach
to TikTok, play generated audio, change `requirements.txt`, or switch the live
TTS backend. Piper and paid/cloud speech services are outside this evaluation.

## Candidates

- **Kokoro multilingual ONNX via sherpa-onnx**: offline CPU inference, Chinese
  and English in one utterance, with an int8 model package. Windows has
  precompiled CPU wheels. The model assets are stored under ignored `data/`.
- **MeloTTS**: official Chinese speaker supports mixed Chinese and English and
  the project describes CPU real-time inference. The upstream install guide
  recommends Docker for Windows, so native Windows setup needs a separate
  compatibility check. Keep it in its own environment.

The current machine has an RTX 3050 Laptop GPU with 6 GB VRAM. The first
benchmark uses CPU to avoid competing with Chromium or the live TTS worker.
No benchmark inference should run while a live session is being monitored.

## Test utterances

Each sample is sent to the model as one complete utterance. This measures
natural mixed-language output and avoids inserting artificial pauses between
segments.

- Traditional Chinese chat
- English chat
- A Latin username with Japanese kana and Chinese text
- A normal Chinese Gift sentence
- A mixed English and Chinese Gift sentence
- Cleaned text that originally contained emoji

The harness keeps one WAV sample per case and writes timing records without
copying the spoken text into logs. It reports model load time, warm synthesis
p50/p95, generated audio duration, and real-time factor (RTF). An RTF below 1
means synthesis completed faster than the generated audio would play.

## Isolated setup

Use Python 3.12 and a separate environment for each engine. Do not install
these packages into `.venv` or production requirements. The helper creates the
environment at `.venv_tts_eval_<engine>`; those environments and all model/run
data are ignored by Git.

```powershell
& .\scripts\setup_tts_backend_eval.ps1 -Engine kokoro
& .\.venv_tts_eval_kokoro\Scripts\python.exe scripts\download_kokoro_eval_model.py
```

The helper downloads the official int8 Chinese/English model and removes the
compressed archive after successful extraction. Then run:

```powershell
& .\.venv_tts_eval_kokoro\Scripts\python.exe experiments\tts_backend_benchmark.py --engine kokoro --iterations 8 --warmup 2
```

For MeloTTS, the upstream Windows guide recommends Docker. This machine
currently has no Docker command available, so the helper tries the native
install in its own environment. If native installation fails, the production
environment remains untouched. Review disk use before choosing a Docker
installation, since its runtime and model cache are additional local storage.

```powershell
& .\scripts\setup_tts_backend_eval.ps1 -Engine melo
& .\.venv_tts_eval_melo\Scripts\python.exe experiments\tts_backend_benchmark.py --engine melo --device cpu --iterations 8 --warmup 2
```

The source repositories are [Kokoro](https://github.com/hexgrad/kokoro),
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), and
[MeloTTS](https://github.com/myshell-ai/MeloTTS). Kokoro weights are Apache-2.0;
MeloTTS is MIT-licensed. Keep their license notices with any later packaged
distributions.

Results are written to `data/tts_backend_eval/runs/<run_id>_<engine>/`.
Listen to the WAVs in `audio/` and compare the JSON timing summaries. Remove
the ignored evaluation venv/model/run folders when finished to reclaim space.

## Acceptance checks before any production switch

1. Windows setup completes in the isolated environment.
2. Traditional Chinese, English, and mixed usernames are intelligible.
3. Gift lines sound like one normal sentence without a long inserted pause.
4. Warm synthesis RTF stays below 1 for representative short chat and Gift
   messages; review p95, not only median.
5. A sustained consecutive-generation run does not build an audio backlog.
6. Listen to samples and confirm pronunciation quality before enabling a
   backend in production.

Only after these checks should the local backend be integrated as immediate
fallback. Gift ID audio caching, ordered pre-generation, bounded Edge retry,
and shorter utterances remain separate changes to validate against real TTS
delivery logs.
