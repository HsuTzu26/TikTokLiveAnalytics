# Live Chat TTS Runtime

This is an opt-in local audio worker. It reads the active Collector or selected benchmark session from its append-only events.ndjson file and does not open another TikTok LIVE connection.

Open Live TTS directly at /live-tts. The Streamlit multipage router preserves this URL when the page is refreshed.

## Setup

Install the added packages into the project environment:

    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Start the Watcher in Streamlit, then choose Live TTS, configure voices and moderation filters, save, and start the worker. Only one local TTS worker is allowed at a time. Stopping it does not stop the Watcher or Collector.

## Processing behavior

- Chat and eligible gift events share one FIFO queue in arrival order.
- The default per-user cooldown and duplicate-text filter are disabled so repeated messages are retained. Both can still be enabled in the UI.
- The default queue holds 500 entries. If it fills, the worker pauses reading the event file instead of discarding accepted messages.
- To keep speech close to the live chat, events older than 5 seconds in the TTS queue are skipped by default. Set Maximum TTS queue age to 0 to retain every event and accept a longer delay.
- A completed gift streak is announced once with the sender's nickname, gift name, and final quantity. Intermediate streak updates are not announced.
- The voice selectors include common Traditional Chinese and English voices. Changing a voice automatically plays a short sentence in that language. Custom voice identifiers already in the config remain selectable.
- Voice changes and voice-preview requests apply while the worker is running. A preview is placed ahead of queued chat and gift events.
- Sticker messages are announced as "send a sticker". URLs and emoji are removed by default; text is whitespace-normalized and capped at the configured character limit.
- Common abbreviations such as IG, FB, YT, DM, and VIP are spaced into letter names for speech, and @ is spoken as "at". Displayed chat text is unchanged.
- Remote TTS requests are spaced by at least 0.5 seconds by default. A transient synthesis or playback error retries that same event up to three times with exponential backoff before moving to the next event.
- A prolonged TTS outage, an event rejected by filters, or stopping the worker can still prevent an announcement. TTS failures do not affect collection.
- Chat text is not copied to the TTS state or worker log. Audio plays through the computer's default audio device.

The Provider Benchmark page refreshes live chat, gift charts, and viewer trends every five seconds. It supports multi-panel or single-panel layouts, a latest-item limit, and separate toggles for chat and gift lists.

Edge TTS is a separate remote service and makes speech requests independently of Euler. Euler is used by TikTokLive to sign the WebSocket connection; chat and gift events then arrive over that connection, so each message is not a separate Euler signing API call.

TikTokLive does not document a separate fixed numeric quota for this local client. Euler documents anonymous signing rate limits and returns HTTP 429 when exceeded; current anonymous allowance can be checked at https://api.eulerstream.com/webcast/rate_limits. Adding an Euler API key gives the account its own increased limits.

For LIVE Studio/OBS, configure desktop-audio capture or route the output through a virtual audio cable. Actual audio-device and end-to-end latency testing remains a manual step.
