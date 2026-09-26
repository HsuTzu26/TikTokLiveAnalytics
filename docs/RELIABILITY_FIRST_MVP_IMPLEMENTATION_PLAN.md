# Reliability-First MVP Implementation Plan

Source of truth: `TikTokLiveAnalytics_Reliability_First_MVP_Spec.docx`.

Target branch: `refactor/v2-architecture`.

## Execution order

### Phase 0 — stabilization

1. **TASK-01 — Raw capture opt-in**
   - Add an explicit collector debug flag; omit `raw_events.ndjson` in normal runs.
   - Record the capture setting in `session.json`.
   - Verify the default and opt-in paths with focused tests.
2. **TASK-02 — Queue semantics**
   - Use timestamp/sequence FIFO ordering for live events.
   - Apply an 8-second configurable TTL to Chat only; never expire captured Gifts.
   - Record `expired_chat` and backend delivery failures explicitly.
3. **TASK-03 — Speech content**
   - Keep Chat speech limited to cleaned message content.
   - Keep Gift speech as sender, gift name, and quantity.
4. **TASK-04 — TTS observability**
   - Publish queue depth, oldest wait age, skip counters, backend errors, and bounded p50/p95 event-to-playback latency in worker state and UI.
5. **TASK-05 — Async regression coverage**
   - Update worker tests to await queue operations and cover TTL, durable Gifts, chronological order, retries, and metrics.
6. **TASK-06 — Lightweight live state**
   - Write atomic per-session state from deduplicated normalized events, plus the latest TTS health snapshot.
7. **TASK-07 — Incremental live dashboard reads**
   - Prefer live state and bounded incremental event tails on live refreshes.
   - Keep full NDJSON scans for historical analysis and explicit reports.
8. **TASK-08 — Bounded logs**
   - Rotate collector and watcher logs with configurable defaults (5 MiB, three backups).

**Phase 0 exit:** focused regression suite passes; live state and TTS health are visible; raw payload capture is off by default; an operator can run and record a four-hour soak with resource and delivery metrics. The code changes and unit coverage are in place; the real-LIVE four-hour evidence gate remains pending.

### Phase 0.5 — dependency insurance

- Define the normalized provider boundary and wrap the existing TikTools provider without behavior changes.
- Add the minimal Euler adapter and keep it out of automatic production failover until smoke-tested.

### Phase 1 — reliability and lightweight runtime

- Add adaptive speech rate without changing FIFO semantics, health-only HTTP watchdog signals, resource metrics, disconnect gap tracking, and soak instrumentation.
- Confirm behavior with an eight-hour soak after Phase 0's four-hour gate.

### Later phases

- Streamer-awareness, smarter Gift speech, and provider failover stay deferred until the MVP reliability evidence exists.

## Baseline observations

- The collector currently opens `raw_events.ndjson` for every session and writes the complete SDK event from its catch-all callback.
- The TTS worker currently applies the same five-second stale-item rule to Chat and Gift, and its metrics expose only an average latency.
- Live dashboard refreshes call the historical session loader and scan full event files.
- Watcher and collector logs currently append without a size bound.

## Implementation tracking

| Task | Status | Notes |
| --- | --- | --- |
| TASK-01 | Done | Raw capture is off unless `--capture-raw` is passed directly or `capture_raw_events: true` is set in the Watcher config; session metadata records the choice. |
| TASK-02 | Done | Timestamp-ordered queue; Chat TTL defaults to 8 seconds; Gift does not expire; shutdown/backend failures are counted. |
| TASK-03 | Done | Chat contains message text only; Gift speech retains sender, gift, and quantity. |
| TASK-04 | Done | Worker state and UI expose queue, oldest age, skips/errors, and bounded p50/p95 latency. |
| TASK-05 | Done | Async queue behavior and failure semantics have regression coverage. |
| TASK-06 | Done | Collector writes bounded atomic `live_state.json`, including measured disconnect gaps; TTS keeps its own atomic worker state. |
| TASK-07 | Done | Live dashboard uses compact state by default; complete NDJSON analysis requires an explicit toggle/session selection. The home page lists recent Collector and LIVE sessions from summary metadata, and historical reports aggregate both `data/raw` and `data/v2_provider_benchmark` sources. |
| TASK-08 | Done | Collector and watcher logs rotate at configurable limits; defaults are 5 MiB and three backups. |

## Validation gate

- Unit/regression suite: 46 tests pass. The local environment lacks `tiktok_live_events`, so the test command supplied a temporary import stub; no test opened a LIVE connection.
- A four-hour real-LIVE soak has not been run. Record RSS, CPU, session disk growth, queue max/age, p50/p95, skips, Gift failures, disconnects, and reconnects before beginning Phase 0.5.
