# Live Chat TTS Runtime

This is an opt-in local audio worker. By default it reads the active Collector session; an explicit session option can select a benchmark or Browser Network Provider session. It only tails `events.ndjson` and does not open a TikTok LIVE connection.

Open Live TTS directly at /live-tts. The Streamlit multipage router preserves this URL when the page is refreshed.

## Setup

Install the added packages into the project environment:

    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Start the Watcher in Streamlit, then choose Live TTS, configure voices and moderation filters, save, and start the worker. Only one local TTS worker is allowed at a time. Stopping it does not stop the Watcher or Collector.

For a Browser Network Provider run, start Chrome and the provider as documented in [Browser Network Provider](browser-network-provider-spike.md), then select its session in Live TTS or start it explicitly in a terminal:

    .\.venv\Scripts\python.exe -X utf8 -m src.tts_worker --username <username> --browser-session-dir data\browser_ws_probe\<session_id>_<username>

The browser session must be under `data/browser_ws_probe` and its suffix must match the username. On a first attach, the worker begins at the current end of the event file to avoid replaying earlier messages. It checkpoints its position in `tts_cursor.json`; after a worker crash, it resumes from that position and restores queued Gifts that have no recorded successful playback. The Watcher does not switch providers automatically.

## Processing behavior

- Chat and eligible Gift events share one chronological queue ordered by event timestamp and capture order. Gifts no longer jump ahead or get grouped behind all Chat solely because of event type.
- The worker keeps at most one announcement pre-synthesized. It prepares the next audio while the current audio plays, then plays the prepared item in chronological order. This overlaps remote synthesis with playback without speaking over itself.
- The default per-user cooldown and duplicate-text filter are disabled so repeated messages are retained. Both can still be enabled in the UI.
- The default queue holds 500 entries. If it fills, the worker pauses reading the event file instead of discarding accepted messages.
- Chat expires after 15 seconds by default; the setting is configurable from 5 to 15 seconds. This gives short bursts more time to drain while keeping very old Chat out of live speech. Expired Chat increments `expired_chat`.
- Captured, counted Gifts have no queue-age limit. After three Edge TTS synthesis failures, the worker tries to synthesize the Gift announcement with a language-matched Windows offline speech voice. After playback failures, it also tries a WAV version once. If offline speech is unavailable or fails, the worker queues that Gift for one more attempt after five seconds, keeping it in timestamp order. If that retry also fails, it records the delivery failure, immediately persists the event ID for replay, and plays the configured Gift sound or a short locally generated alert. The alert does not count as spoken speech. The next worker start restores the Gift; an event without a stable ID cannot be replayed automatically.
- A completed gift streak is announced once with the sender's nickname, gift name, and final quantity. Intermediate streak updates are not announced.
- Chat speech contains the cleaned message only; it does not announce the username.
- TTS text uses Unicode NFKC normalization, which converts compatibility-style alphabets such as mathematical/script Latin letters to ordinary text. Unicode characters with no compatible English or CJK form (including emoji when ignored and decorative symbols) are skipped for speech; captured event text remains unchanged. Other writing systems are not transliterated.
- Japanese kana directly between Latin letters in a handle is treated as a separator, keeping the Latin part in one English speech segment; kana in ordinary Japanese text is preserved.
- Edge TTS Chat synthesis falls back to Windows offline speech immediately after the first Edge synthesis failure, preserving the event's bilingual segment order. The fallback requires locally installed English and/or Traditional Chinese SAPI voices for the languages in the message. Every Chat event records its synthesis, fallback, expiry, and playback stages in the session's `chat_tts_delivery.ndjson`; message text is not copied into that diagnostic log.
- The voice selectors include common Traditional Chinese and English voices. Changing a voice automatically plays a short sentence in that language. Custom voice identifiers already in the config remain selectable.
- Voice changes and voice-preview requests apply while the worker is running. A preview joins the same timestamp-ordered queue.
- Sticker messages are announced as "send a sticker". URLs are removed, common ASCII faces such as `^_^` and `:)` are skipped, and known emoji labels such as `[laugh]` are spoken in Traditional Chinese even when they appear alone. Common Unicode emoji also use the Traditional Chinese labels; unknown emoji still follow the Ignore emoji setting.
- Mixed Chinese and English Chat and Gift announcements are split at script changes and synthesized with their corresponding configured voices. Each normalized event remains one queued item and keeps its original chronological position.
- Gift names are kept as TikTok supplied them in captured events and reports. The ID-keyed catalog covers 78 Gift IDs seen in saved LIVE captures and stores the exact original name, English name when TikTok supplied one, Chinese display name, Chinese TTS name, observed diamond count, mapping status, and field sources separately. Six gifts with localized Chinese originals still need a reliable English name and are marked pending; unknown IDs keep their exact raw name and appear as pending in the session report. See [Gift catalog](gift_catalog.md).
- Gift localization and mixed-language cleanup run after protobuf decoding and never write back to the original Gift name. Up to four script-matched speech segments are synthesized per event; pathological script alternation falls back to one dominant-language segment while preserving all text.
- Gift delivery logs record protobuf envelope/payload, event normalization, catalog lookup, TTS name resolution, speech preparation, each synthesis segment, total synthesis, and playback durations. Each error is logged against the event and Gift ID.
- Chat TTS defaults to **Everyone**. The Live TTS page can limit Chat speech to recognized fan-club badge members or selected account IDs. Selected IDs can be picked from recent captured users or entered manually. These filters apply to Chat; Gifts remain announced for all senders.
- In Live TTS, expand **Gift pronunciation and sounds**, choose a Gift seen in the event data, and upload a WAV, MP3, or OGG file. No special source filename is required; the app stores a local copy under `data/tts/sounds/`. Choose **Every completed Gift** for a general cue or **One selected Gift** for a specific cue. A specific Gift cue overrides the general cue. Leave it unset to keep sounds off. Intermediate Gift streak updates do not trigger sounds. Gift cues begin after the Gift TTS and play on a separate mixed audio channel, so the next announcement does not wait for the whole sound to finish.
- Expand **Entry sounds** to upload a cue for any member entry, a minimum fan-badge level, or one specific TikTok user. The user-specific cue takes priority over fan-level and general entry cues. The default level threshold is 10; set it to 1 for any detected fan-club badge. Sounds are optional and remain disabled until a file is uploaded.
- Common abbreviations such as IG, FB, YT, DM, and VIP are spaced into letter names for speech, and @ is spoken as "at". Displayed chat text is unchanged.
- Remote TTS requests are spaced by at least 0.5 seconds by default. A transient synthesis or playback error retries that same event up to three times with a short bounded delay, then clears its retry delay before the next queued event.
- Each Edge TTS receive attempt is bounded to 10 seconds. Long service or network outages can still fail individual Gifts, but one failure no longer applies an unbounded global cooldown to later announcements.
- TTS logs include a content-free Gift trace (`event_id`, `gift_id`, and queued, synthesis, playback, retry, alert, or failure stage). The status counters distinguish final Gift synthesis failures, playback failures, delayed retry attempts, and alert playback. These stages show what the software completed; they cannot confirm that speakers were physically audible.
- Browser LIVE sessions automatically get `data/browser_ws_probe/<session_id>/live_summary.md` when capture stops. The formatted Gift table totals streak quantity and event diamond totals. After the launcher gracefully stops TTS, it refreshes the report with final TTS counters and any failed Gift events.
- Each Browser session's `tts_delivery.ndjson` records Gift stages with the session ID, event ID, Gift ID, raw and spoken names, quantity, diamond total, retry attempt, and error type. Failed synthesis/playback attempts and final delivery outcomes are tied to the same Gift event, so a failure can be traced back to its Gift ID.
- Gift IDs missing a complete Chinese display/TTS mapping are recorded once in `data/tts/unmapped_gifts.ndjson`, retaining the raw TikTok name and observed coin value. A session-local copy is written to `gift_catalog_pending.ndjson` for inclusion in the end report.
- During an intentional worker handoff, `data/tts/replay_handoff.json` can restore counted Gifts that were not spoken from the same Browser session. Replay is bounded to 5,000 events; Chat is not replayed because stale Chat expires by design.
- State reports queue depth and peak, full-queue waits, oldest wait age, bounded p50/p95 event-to-queue, queue wait, synthesis, audio playback, and event-to-playback latency, plus skip reasons and synthesis/playback errors. Percentiles describe the most recent 1,000 samples; peak counters cover the worker lifetime.
- Offline speech uses Windows `System.Speech` and an already installed voice matching the announcement language. It adds no Python dependency and makes no network request. Windows installations without a suitable offline voice will log the fallback failure; Chats continue through Edge retries until they play or expire, while Gifts use the existing retry, alert, and replay path. An event rejected by filters or stopping the worker can also prevent an announcement; the worker records these cases in its state. TTS failures do not affect collection.
- Chat text is not copied to the TTS state or worker log. Audio plays through the computer's default audio device.

The Provider Benchmark page refreshes live chat, gift charts, and viewer trends every five seconds. It supports multi-panel or single-panel layouts, a latest-item limit, and separate toggles for chat and gift lists.

Edge TTS is a separate remote service and makes speech requests independently of Euler. Euler is used by TikTokLive to sign the WebSocket connection; chat and gift events then arrive over that connection, so each message is not a separate Euler signing API call.

TikTokLive does not document a separate fixed numeric quota for this local client. Euler documents anonymous signing rate limits and returns HTTP 429 when exceeded; current anonymous allowance can be checked at https://api.eulerstream.com/webcast/rate_limits. Adding an Euler API key gives the account its own increased limits.

For LIVE Studio/OBS, configure desktop-audio capture or route the output through a virtual audio cable. Actual audio-device and end-to-end latency testing remains a manual step.
