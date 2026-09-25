# v2 Provider Full-Live Benchmark

This runbook operationalizes Phase 0 of `TikTokLiveAnalytics_v2.docx`. It does not select or switch the production provider. The candidate must pass a 2–4 hour live session before it is adopted.

## Candidate identity

Record the exact integration being tested. These are separate candidates:

- **TikTokLive Python client + Euler signing service**: the open-source `TikTokLive` client, using its configured signing endpoint and optional Euler key.
- **Euler managed WebSocket Python SDK**: Euler's separate `EulerApiSdk` integration and managed event connection.
- **TikTools `tiktok-live-events`**: the current collector provider and the v2 cold-fallback candidate.

Do not label the first two simply “Euler”; their connection path, limits, SDK, and failure behavior differ. The upstream TikTokLive README currently describes the library as not production-ready and recommends Euler's WebSocket API for production, so the exact integration and measured behavior must be recorded.

## Run conditions

1. Use one provider connection for the run. Do not run two providers into the same formal event store.
2. Capture only normalized benchmark events. Sticker messages may retain an emote ID and one approved CDN image URL per sticker; do not retain full JSON/protobuf payloads, cookies, API keys, or audio.
3. Record provider/package versions, Python version, OS, region, start/end UTC, room ID, and whether a key was configured. Record key presence only, never its value.
4. Ensure the stream contains representative Viewer samples and, if possible, at least one Chat and completed Gift streak. If an event does not occur naturally, mark that criterion **inconclusive**, not failed.
5. Measure CPU and memory at the start, at regular intervals, and at the end. Do not infer resource use from event logs.
6. Exercise reconnect and cold-fallback behavior separately. Stop the primary before starting fallback; verify the provider transition and deduplication without concurrent formal writes.

## Start the current compact probe

The existing probe exercises the TikTokLive Python client path with its default Euler signing endpoint. It does not exercise Euler's separate managed WebSocket SDK or the v2 provider failover controller.

```powershell
.\.venv_tiktoklive\Scripts\python.exe src/legacy/collector_tiktoklive_probe.py <username> --output-root data/v2_provider_benchmark/tiktoklive_euler_signing
```

Keep the process running for the selected 2–4 hour window. Send Ctrl+C once to close the run and write `summary.json` cleanly.

## Measurements

| Area | Record |
|---|---|
| Connection | Total run duration, connected time/ratio, connect and disconnect count, reconnect count, reconnect duration |
| Events | Chat, Gift, Viewer, Follow, Share, Subscribe and Connection counts; last event time; longest event and Viewer-sample intervals |
| Viewer | Sample interval distribution, missing intervals, whether the field is concurrent viewer count |
| Gift | Gift ID/name, per-gift diamonds, repeat count, final-streak behavior, transaction/event identity, duplicate count |
| Identity | User ID coverage for Chat and Gift; unique ID coverage; nickname is display-only |
| Recovery | Primary retry outcome, fallback start time, gap start/end, event replay/duplicate observations |
| Resources | CPU and RSS samples/trend; API or signing quota before/after where available |

## Pass conditions

- The candidate runs continuously for 2–4 hours and recovers from a controlled disconnect.
- Viewer samples arrive with a measured cadence; report missing periods as gaps rather than zero viewers.
- Chat and Gift fields normalize correctly when those events occur; user ID is present and stable enough for accounting.
- A streakable Gift is counted once at completion, and replaying the same transaction does not add diamonds twice.
- A failed primary can transition to cold fallback with a recorded provider change and collection gap, without concurrent writes.
- CPU and memory show no continuing upward trend during the run; include the measurements in the report.
- Quota use is recorded and remains within the configured plan for the intended schedule.

## Result record

Copy this section for each provider run:

```text
Provider integration:
Package/version:
Python / OS / region:
Streamer / room ID:
Start UTC / end UTC:
Euler key configured (yes/no; do not record value):

Connected seconds / ratio:
Connections / disconnects / reconnects:
Longest event gap / Viewer sample interval p50 / max:
Event counts (Chat/Gift/Viewer/Follow/Share/Subscribe):
Chat user_id coverage:
Gift user_id coverage:
Streak validation:
Duplicate/replay validation:
Fallback transition and gap:
CPU samples/trend:
RSS samples/trend:
Quota before/after:

Result: PASS / FAIL / INCONCLUSIVE
Evidence and notes:
```

For the current compact TikTokLive probe output, generate a machine-readable summary after the run:

```powershell
.\.venv_tiktoklive\Scripts\python.exe -m src.provider_benchmark_report <session_dir> --output <session_dir>\benchmark-report.json
```

On Windows, sample the active probe process every minute for up to two hours. First identify its process ID with `Get-Process -Name python | Select-Object Id,CPU,WorkingSet64,StartTime,Path`, then run:

```powershell
powershell.exe -NoProfile -File tools/provider_benchmark_resources.ps1 -ProcessId <pid> -OutputPath <session_dir>\resource_samples.csv -IntervalSeconds 60 -DurationMinutes 120
```

The report reads `resource_samples.csv` when present. It labels missing Gift, deduplication, fallback, and resource criteria as unobserved or unmeasured; it does not convert missing evidence into a pass.

## Upstream references

- [TikTokLive Python README](https://github.com/isaackogan/TikTokLive/blob/master/README.md)
- [EulerStream TikTok LIVE API SDKs and plans](https://github.com/EulerStream/TikTok-Live-Api)
- [TikTools Python SDK](https://github.com/tiktool/tiktok-live-events)
