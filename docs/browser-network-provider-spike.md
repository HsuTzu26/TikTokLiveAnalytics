# Browser Network Provider

This is an explicitly selected Browser Network Provider. Chromium owns the TikTok connection; the provider observes its CDP Network events and never opens a TikTok WebSocket itself. The normalized `events.ndjson` format is shared with the existing Collector. TikTools remains the Watcher's production source unless a user explicitly attaches the TTS worker to a Browser Network session.

## Windows workflow

For a repeatable Browser Network + TTS run, use the batch launcher from the repository root:

```bat
scripts\run_browser_network_test.bat
```

It prompts for the streamer username. To pass it directly:

```bat
scripts\run_browser_network_test.bat chelseypiggy
```

Replace the username as needed. Add a duration in minutes to stop capture automatically, for example `scripts\run_browser_network_test.bat chelseypiggy 120`. The launcher starts or verifies the dedicated Chrome profile, waits for manual login and LIVE page selection, then starts the CDP probe and attaches TTS to that exact capture. At probe startup it clears the dedicated profile's disposable HTTP cache through CDP; cookies and site data are preserved. The probe runs in its own console; Ctrl+C there stops capture and the launcher requests a graceful TTS stop. Chrome remains open. Normal runs save event logs but not raw frame bodies under `data\browser_ws_probe\<session_id>_<username>`; TTS logs are saved under `data\tts\logs`.

To retain a bounded raw packet sample for debugging, append `raw`, for example `scripts\run_browser_network_test.bat chelseypiggy 120 raw`. This keeps the first 5,000 frame payloads and continues decoding events after the storage limit. To preserve the HTTP cache for a debugging run, append `keepcache` as the fourth argument: `scripts\run_browser_network_test.bat chelseypiggy 120 raw keepcache`.

The probe command does not start the Dashboard. To open the local Dashboard without starting the Watcher or another TikTok collector, run `scripts\start_dashboard.bat`; it opens `http://127.0.0.1:8502`.

1. Close the dedicated debug Chrome window if it is already running. Do not close everyday Chrome windows.
2. From the repository root, launch Chrome with its isolated profile:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start_tiktok_debug_chrome.ps1
   ```

The profile is `data/chrome_tiktok_profile`; it is separate from Chrome's everyday profile. `-ChromePath`, `-ProfileDir`, and `-Port` can override the defaults.
3. Log into TikTok manually in that Chrome window. Complete consent or human verification there if TikTok asks. The provider does not automate login, CAPTCHA, or verification.
4. Start the provider from the repository root. Use the same TikTok username whose LIVE will be watched:

   ```powershell
   .\.venv\Scripts\python.exe experiments\browser_ws_probe.py --username <username> --browser-mode cdp --cdp-url http://127.0.0.1:9222 --reload-live-page
   ```

   Optional page selection is zero-based across the printed `[CDP PAGE]` listing:

   ```powershell
   .\.venv\Scripts\python.exe experiments\browser_ws_probe.py --username <username> --browser-mode cdp --cdp-url http://127.0.0.1:9222 --page-index 0
   ```

   Normal event collection does not save raw packet bodies. To preserve a bounded sample for decoder troubleshooting, add `--save-raw-frames --max-frames 5000`. Reaching this storage limit does not stop LIVE event capture.

5. The command attaches to the selected matching LIVE tab, enables CDP, then reloads that same tab because CDP does not replay frames from an earlier socket. It does not close Chrome or change the dedicated profile. Without `--reload-live-page`, open or reload the LIVE tab manually after starting the probe.
6. Check `[LIVE PREFLIGHT]`, `[WS DISCOVERED]`, `[WS CANDIDATE]`, `[WS OPEN]`, frame/method output, and the session summary. A page HTTP 200 is not evidence that the streamer is live. `OFFLINE` means the transport was not tested; `UNKNOWN` means no observed response gave an explicit enough status signal.
7. Let the browser run for the desired stage and stop the provider with Ctrl+C. CDP mode detaches from the debugging session and leaves the user's Chrome open. Raw WebSocket payload storage is off by default; event decoding and NDJSON capture continue for the full run.

The provider can also be used from Python with `src.providers.browser_network.BrowserNetworkProvider`. It accepts an optional `NormalizedEventBus`; events are written to the durable NDJSON file first, then published to bounded subscriber queues. A full or broken subscriber cannot block capture. Analytics continues to read the compatible `events.ndjson` format. Browser sessions store rank arrays with viewer events; analytics reads those rows directly, avoiding a duplicate `rankings.ndjson`. Older sessions that already have a rankings file remain supported.

To attach the existing TTS worker explicitly to one Browser Network session, open a second terminal and pass that session directory:

```powershell
.\.venv\Scripts\python.exe -m src.tts_worker --username <username> --browser-session-dir data\browser_ws_probe\<session_id>_<username>
```

The worker starts at the current end of that session file so it does not replay old LIVE comments. Chat expiry, chronological queueing, filters, gift streak handling, and Gift delivery behavior stay in the existing TTS pipeline. The normal Watcher/TikTools path is unchanged.

The launch mode remains available for isolated headful Chromium:

```powershell
.\.venv\Scripts\python.exe experiments\browser_ws_probe.py --username <username>
```

The dedicated profile is required by current Chrome security behavior: since Chrome 136, remote debugging flags are ignored for Chrome's default data directory. The helper supplies a non-default `--user-data-dir`, as Chrome recommends. This means the first run has a separate TikTok/Google sign-in from everyday Chrome; use the normal sign-in page and complete any account checks manually. The probe does not automate or bypass Google/TikTok security checks. See [Chrome's remote debugging change](https://developer.chrome.com/blog/remote-debugging-port).

## Captures and privacy

Each run creates `data/browser_ws_probe/<session_id>/` with `session.json`, `sockets.ndjson`, `methods.ndjson`, and normalized `events.ndjson`. Raw `frames.ndjson` metadata and `.bin` payloads are created only when `--save-raw-frames` is enabled, with at most `--max-frames` observed frames saved (default 5000). Reaching this limit stops raw storage only; event decoding continues. WebSocket reports remove credentials, query strings, and fragments. Cookies, browser profile contents, and authentication headers are not written by the probe. Treat captured frames and normalized chat text as private session data; `data/` is git-ignored.

The preflight only reports `LIVE_CONFIRMED` when a browser-observed candidate endpoint returns an explicit live signal and room ID; an explicit offline signal reports `OFFLINE`. Candidate path names are observations, not guaranteed TikTok API contracts. No response body is retained in the output.

## Validation gates

Use a currently LIVE streamer, confirm the page and candidate WebSocket, then observe incoming binary frames and decoded methods (Decision Gates A and B). Natural Chat and Gift events can validate their decoders; a distinctive test Chat or Gift is optional. If no Gift occurs during a capture, mark Gate D `NOT_TESTED`, not failed, and resume on a later LIVE with natural Gift activity. Do not synthesize events to claim runtime validation.

Feasibility levels are recorded in `session.json`: 1 page loaded; 2 LIVE confirmed plus candidate socket; 3 binary frames; 4 methods; 5 Chat content; 6 Gift metadata; 7 TTS runtime validation; 8 viewer/member fields; 9 two-hour stability; 10 four-hour stability. Earlier captures reached Level 6 before physical TTS was attached. The `@8.6.6.1.3` run below reached Level 8. LIVE confirmation requires an explicit observed metadata response or a decoded room-scoped LIVE event; an HTTP 200 alone is insufficient. Provider selection is explicit, with no failover.

## Current validation status

The latest Stage A run on `@_03kxzxii` lasted 600 seconds. It discovered 9 TikTok WebSocket connections overall, including 8 auxiliary `im-ws-sg.tiktok.com` connections and 1 LIVE candidate connection at `webcast-ws.tiktok.com`. The LIVE socket completed a 101 handshake and remained open for about 570 seconds through capture shutdown. It supplied 305 binary frames with no payload/protobuf decode errors, 55 Chat events, 3 Gifts, 143 Viewer updates, 54 Member events, and 13 Like events. Chrome stayed attached and stable for the full run. The eight auxiliary sockets had CDP frame errors; the LIVE event socket did not. The feasibility report counts the LIVE socket and its frames separately from auxiliary TikTok sockets.

A follow-up Stage B run lasted 1,800.1 seconds. After enabling CDP and reloading the selected LIVE tab, Chromium received 764 binary frames through one `webcast-ws.tiktok.com` connection; that socket remained open for about 1,793.6 seconds through capture shutdown. It decoded 50 Chat, 1 Gift, 402 Viewer, 165 Member, and 6 Like events. The natural Gift payload contained sender, Gift ID/name, quantity/streak state, and diamond fields. There were no payload/protobuf decoding errors and no frame errors on the LIVE socket. Eleven frame errors were recorded on auxiliary IM sockets. The capture used 1.41 MB of raw frame files and 2.20 MB total session data. Analytics read the normalized output as 624 events (including the Gift message), with 1 counted Gift and 1 diamond.

Stage C was started on the same streamer and ran for 1,210.1 seconds. It captured 320 binary frames, 83 Chat, 2 Gift, 118 Viewer, 39 Member, and 13 Like events with no protobuf decode errors. The last LIVE frame contained `WebcastControlMessage` with SDK action `STREAM_ENDED`; the Webcast socket closed about 126 ms later. Chromium did not reconnect during the remaining 7 minutes 39 seconds. This positively identifies the observed stream end, rather than an unexplained transport loss. The run is not a two-hour stability pass: the LIVE socket was active for 747.2 seconds (61.75% of total capture time) because the stream ended. The probe now writes this signal as a deduplicated `live_ended` normalized event and records `live_end_detection` in new session summaries. A socket close without that control action remains `NOT_OBSERVED` and must not trigger settlement by itself.

An earlier 10-minute sample captured 10 Gifts. Every record included sender, Gift ID/name, repeat count/state, and diamond metadata; six were intermediate streak updates and four were counted completions, totaling 13 diamonds. The latest sample also had complete sender/Gift/count/state/diamond fields for all three Gifts. Across both samples, Chat comments were recovered as text, and Viewer and Member payloads were normalized.

The browser-observed `/webcast/room/check_alive/` endpoint returned HTTP 200 but its body did not expose a recognized explicit status. The provider confirms LIVE from a decoded, room-scoped Webcast event and records that evidence and room ID in `session.json`. The observed `STREAM_ENDED` control message is now a separate terminal event, so downstream session settlement can subscribe to an explicit signal instead of treating an arbitrary socket close as an ended LIVE.

Long-run stability gates require the LIVE WebSocket to be active for at least 95% of the capture duration; the tolerance covers initial page attach/reload. Stage A (10 minutes) and Stage B (30 minutes) passed on earlier captures; Stage C ended when TikTok emitted `STREAM_ENDED`, and Stage D (four hours) was not run. The `@8.6.6.1.3` run also validated real Browser-session TTS playback and measured event-to-queue/playback latency. Automated decoder/provider tests use synthetic protobuf/gzip fixtures and do not require a TikTok connection.

### Previous extended run: `@8.6.6.1.3`

Chromium confirmed LIVE room `7690435274904849204`, discovered one `webcast-ws.tiktok.com` socket, and captured 1,081 binary frames over 14 minutes 17 seconds. That socket stayed active for 846.4 seconds (98.8% of the measured interval); it had a 101 handshake and no target-socket frame errors. Nine auxiliary `im-ws-sg.tiktok.com` sockets produced CDP frame errors. The probe process was interrupted before its finalizer wrote the session summary; `session.json` is marked recovered/interrupted, and no Chromium version was persisted.

Saved frames decode to 144 Chat methods (140 with comment text), 74 Gift methods, 413 viewer updates, 339 member-entry events, and 274 Like updates. All 74 Gift messages include sender, Gift ID/name, quantity, streak state, and diamond fields; 39 completed Gift events sum to 3,012 Diamonds. This is the stream's Diamond value, not a cash payout estimate. Viewer updates contain 413 ranking snapshots with five entries each; viewer samples ranged from 1,272 to 1,821, and the final observed Like total was 14,508. Member entries include account identifiers and nicknames in all 339 events, plus entry source, badges, fan-club level, and user grade where TikTok supplied them. The SDK top-user flag and rank score were present but never positive in this capture. Thirteen offline decoder diagnostics were limited to `WebcastLinkLayerMessage`; Chat, Gift, Viewer, Member, and Like records decoded successfully. `rankings.ndjson` was reconstructed from saved frames for this run.

The attached TTS worker spoke 90 announcements: 59 Chat and 31 completed Gifts. It queued 81 Chat and 31 Gifts, skipped 18 intermediate Gift streak updates, expired 22 stale Chat messages, and had zero Gift delivery failures. Event-to-queue p50/p95 were 270/500 ms; event-to-playback p50/p95 were 2,761/25,404 ms. The long p95 reflects queued speech backlog. The new mixed-language segmentation, common-emoji speech mapping, and optional per-Gift-ID audio cue are covered by offline tests but were not present in this already-running worker. No audio file was configured, so follow-up music playback remains unvalidated on a real audio device.

This sample passed Level 8 and Stage A. Level 9 (two hours) and Level 10 (four hours) remain untested. CPU and RAM were not measured per session because the machine had multiple Chrome processes and the probe had already exited. The recovered session folder occupies 4.77 MB, including frames, normalized events, ranking snapshots, and the summary.

### Natural LIVE retest: `@chelseypiggy` ending naturally

The capture confirmed room `7690446797991725876` and one `webcast-ws.tiktok.com` socket. It ran for 6,635.75 seconds (110.6 minutes); the LIVE socket was active for 6,482.1 seconds (108.0 minutes). Chromium received 5,649 binary frames. The stream ended naturally at 17:33:15 Asia/Taipei: the final LIVE control event decoded as `WebcastControlMessage` / `STREAM_ENDED`, and the WebSocket closed immediately afterward. The probe remained attached for about 2.5 more minutes before it was stopped. This validates live-end detection, but does not pass the two-hour stability gate because the stream ended before two hours of active LIVE transport.

The session recorded 885 Chat, 226 Gift, 1,828 Like, 1,094 Member, and 2,355 Viewer events, plus one `live_ended` event and 2,355 ranking snapshots. Chat text and Gift fields were decoded. Methods included `WebcastChatMessage`, `WebcastGiftMessage`, `WebcastRoomUserSeqMessage`, `WebcastLikeMessage`, `WebcastMemberMessage`, and `WebcastControlMessage`. No decoding errors were recorded. The browser stayed connected until TikTok sent the natural end signal; there was one target socket open and one close.

The TTS worker was attached to the same Browser session. It saw 861 Chat and 224 Gift events, queued 772 Chat and 153 Gifts, and spoke 439 Chat plus 113 Gifts. It expired 329 stale Chat messages and recorded 40 Gift delivery failures. Event-to-queue latency was 265 ms p50 / 483 ms p95; event-to-playback latency was 2,078 ms p50 / 31,154 ms p95. This confirms the Browser-to-TTS path while showing that queued speech can become delayed during bursts. CPU and Chrome RAM were not measured for this run.

This run confirms live-end event detection and extends continuous operation to 108 minutes of active LIVE transport. It does not meet the two-hour or four-hour stability gates. Use the batch command above to reproduce the same capture-and-TTS workflow on a future LIVE; the username and duration can be changed for each run.
