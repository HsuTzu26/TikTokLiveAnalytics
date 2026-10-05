# Chromium TikTok LIVE WebSocket probe

This isolated experiment observes frames that Chromium already receives for a TikTok LIVE page. It does not create a WebSocket connection, use TikTools credentials, or start any production collector/provider. It does not send chat, gifts, or likes.

Playwright is already included in the repository's `requirements.txt`; no production dependency changes are needed. Install the project's requirements and Chromium browser if they are not already present:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Repeat the Windows Browser Network + TTS run

From the repository folder, run:

```bat
scripts\run_browser_network_test.bat chelseypiggy
```

You can replace `chelseypiggy` with another TikTok username. Add a duration in minutes to stop capture automatically, for example `scripts\run_browser_network_test.bat chelseypiggy 120`. Without a duration, capture continues until you stop the probe.

The command starts the project's dedicated Chrome profile if CDP is not already available. Sign in and complete any verification manually, open the streamer's LIVE page, then press Enter in the launcher window. Probe startup clears the dedicated profile's HTTP cache through CDP while preserving cookies and site data. It opens the probe in a separate console and starts TTS against that exact Browser session. Audio plays through the default Windows audio device; the TTS worker logs go under `data\tts\logs\`.

After the natural LIVE end event appears, press Ctrl+C in the probe console. The launcher then requests a graceful TTS stop and prints the session folder to review. Chrome stays open. Routine runs persist normalized events without raw WebSocket payload files; append `raw` as the third BAT argument only for decoder troubleshooting. Append `keepcache` as the fourth BAT argument to preserve HTTP cache during debugging, for example `scripts\run_browser_network_test.bat chelseypiggy 120 raw keepcache`. The standalone Python probe does not clear cache unless `--clear-browser-cache-on-start` is explicitly supplied. Do not run this command against your everyday Chrome profile. This flow does not send Chat, Gifts, or Likes.

Run headful by default so you can use your normal TikTok login, consent flow, and manual verification:

```powershell
python experiments/browser_ws_probe.py --username kclcann
```

Optional arguments:

```text
--url https://www.tiktok.com/@kclcann/live
--profile-dir data/browser_profile
--output-dir data/browser_ws_probe
--save-raw-frames
--max-frames 5000
--duration-seconds 60
--headless
```

The profile persists between runs. Keep it private: it may contain TikTok login cookies. Probe output is written under `data/browser_ws_probe/<session_id>/`, which is ignored by Git. The report URL fields omit query strings, fragments, and credentials. Raw binary frames may contain chat content and should also be treated as private. Normal runs decode frames in memory and save normalized events without retaining the raw binary payloads. Pass `--save-raw-frames` to retain a bounded debugging sample; `--max-frames` limits that sample and no longer stops event capture. `--duration-seconds` is optional and useful for bounded smoke runs; stop longer runs with Ctrl+C.

## Manual validation

1. Start the probe for a streamer who is currently LIVE.
2. Confirm the LIVE page loads in the Chromium window. Sign in or accept consent manually if needed.
3. Confirm the console reports `[WS OPEN]` for a sanitized `webcast-ws.tiktok.com` URL.
4. Let the stream run and check that decoded method/event counts continue to increase. Add `--save-raw-frames` only when you need `.bin` payloads for decoder debugging.
5. From another account, send the distinctive chat text `TEST_CHAT_927` and check for `WebcastChatMessage` in the console and `methods.ndjson`.
6. Send one inexpensive Gift and check for `WebcastGiftMessage`.
7. Generate Likes if practical and check for `WebcastLikeMessage`.
8. Leave the probe running for at least 10–15 minutes to observe background traffic and browser stability.
9. Stop with Ctrl+C. The probe flushes `session.json`, `methods.ndjson`, and normalized `events.ndjson`; raw frame files are optional.
10. Review the captured frame count, detected methods, and decoding errors in `session.json`.

If TikTok displays CAPTCHA or human verification, complete it yourself in Chromium. The probe prints a reminder, keeps the page open, and continues observing; it does not attempt to solve or bypass the challenge. Raw frame storage is bounded independently from event collection.

## Decoder scope and result levels

The first pass searches raw bytes and gzip-decoded candidates. It prefers `WebcastPushFrame` and `ProtoMessageFetchResult` definitions from the installed `TikTokLive` SDK; a small protobuf wire reader is available for offline fixtures and environments without that optional import. The fallback follows the SDK's `payload`/`messages[].method` envelope fields and does not parse full message schemas.

Chat text and Gift sender/name/count decoding are intentionally not part of this first spike. Record the observed highest level after a manual run:

1. Chromium loads the LIVE page.
2. Chromium reports a `webcast-ws.tiktok.com` WebSocket handshake.
3. Incoming binary WebSocket frames are captured continuously.
4. Webcast method names are extracted.
5. Chat text is decoded.
6. Gift sender, name, and count are decoded.

Level 3 or 4 supports continuing work on a Browser Network Provider. Level 5 or 6 is the gate for designing a normalized adapter for the production event pipeline. This experiment does not make that integration.
