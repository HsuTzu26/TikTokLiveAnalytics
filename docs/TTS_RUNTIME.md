# Live Chat TTS Runtime

This is an opt-in, local audio action layer for captured TikTok LIVE chat. It does not create its own TikTok connection. The TTS worker follows the selected Collector's `events.ndjson`, and starts from the current end of that file if enabled mid-stream; it therefore does not replay the old chat backlog.

## Setup

Install the added packages into the project environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the Watcher in Streamlit, then choose **Live TTS**, configure the voices and safety filters, save, and start the worker. Only one local TTS worker is allowed at a time. Stopping it does not stop the Watcher or Collector.

## Processing behavior

- Whole-message language routing: Traditional Chinese uses `zh-TW-HsiaoChenNeural`; other text uses `en-US-AriaNeural` by default.
- URLs and emoji are removed by default; text is whitespace-normalized and capped at the configured character limit.
- Optional blocked words, per-user cooldown, duplicate-message window, and bounded queue prevent common abuse and runaway latency.
- Remote speech requests are spaced by at least one second by default; repeated TTS/audio failures trigger exponential backoff up to 60 seconds, during which queued items are dropped instead of creating a retry burst.
- Queue overflow drops the new item. TTS synthesis/playback errors are counted and do not affect collection.
- Chat text is not copied to the TTS state or worker log. TTS output is played through the computer's default audio device.

The Edge TTS backend requires an internet connection and is a separate remote speech request for each accepted chat. It can fail or be rate limited independently of TikTok collection. For LIVE Studio/OBS, configure desktop-audio capture or route the output through a virtual audio cable. Actual audio-device and end-to-end latency testing remains a manual step.

Gift/Follow/Subscribe actions, word-level voice switching, priority queues, overlays, and virtual-audio routing are not included in this first upgrade.
