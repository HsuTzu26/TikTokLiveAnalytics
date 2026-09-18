# TikTok Live Analytics

??Python ??? TikTok LIVE ????哨?颲???寡??謆????謕??殉次蹌?NDJSON???嚗??塗???蹓賣??????謘踱??

## ??赤???

???????輯撒??Python 3.12 ??軋僕?獢???`.venv`??

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
```

`tiktok-live-events` ????∴???撕? Git ????????editable install ???
`tiktok_live_events` ????????SDK ?賹????????????????

## ????鈭

- `src/collector.py`?契??蹇????????獢? `watchlist.json` ?輯撒?????蝛???
- data/raw/<session>/raw_events.ndjson?垢???SDK ?謍船? event callback ????皜???payload??
- `src/watcher.py`?垢? `watchlist.json` ?嚗貉?皝?????謚恃??????
- `src/analyzer.py`?城? `events.ndjson` ?塗??????????CSV ?????JSON??
- `src/validate_session.py`?垮??鈭行??????diamond ?殷????皝?撢?
- `src/plot_session.py`?垓???? CSV ?嚗? PNG ?謘踱??

?哨????

```powershell
python src\collector.py <username>
python src\watcher.py --config watchlist.json
python -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
python src\analyzer.py data\raw\<session>
python src\validate_session.py data\raw\<session>
python src\plot_session.py data\raw\<session>
```

????荒??? `data/raw/<session>/`?蹇??獢???watcher log/state ?鞈???蟡??嚗對??
????鈭止僕 Git ??????

## ??秧??對?

?撖抆???Git ??曇?拇???????蹓曇澈?堊奕???刻麾?洩tiktok-live-events/` ?踐????豢???Git
????蹇?????皜?????隡?

```powershell
git status
git -C tiktok-live-events status
```

?獢?????????摹?蹓澗??蹓??賣??嚗? SDK ???????????瘞玲?
## Runtime behavior

- Watcher log timestamps use Asia/Taipei; event/session files keep both UTC and local timestamps.
- probe_timeout and HTTP 429 are non-authoritative. When no collector is active, the watcher starts a resilient collector candidate that keeps retrying with exponential backoff (up to 60 seconds).
- Only the SDK response is not currently live is treated as authoritative offline.
- Legacy collector versions are kept under src/legacy/; src/collector.py is the only supported collector entry point.

## Dashboard ?????

?賹? Streamlit??

```powershell
python -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
```

?伍??session ??畸??`collector.log`?活atcher ??萇???`data/watcher/watcher.log`??
Probe timeout ??HTTP 429 ????殉死????謕遙??Collector????詨??瞉??撐??

## Dashboard analysis

The Streamlit dashboard displays all event times in Asia/Taipei. Select a streamer, then either choose an available session or enter its exact Session ID (for example 20260913_195928_chloe_o723_). Trend charts use timestamped X axes with explicit Taiwan-time and metric labels.

The merge_daily_sessions.py utility can consolidate sessions into one daily folder per streamer and archives source folders under data/raw/archive/ while preserving source_session_id on every record.

The sidebar also accepts a new streamer username and updates watchlist.json; the running watcher reloads it on the next polling cycle.
Use ?謜鈹move from watchlist??to delete a streamer from future tracking; existing raw sessions are intentionally preserved. When a collector exits with `live_end` or `offline_confirmed`, the watcher automatically refreshes `data/raw/YYYYMMDD_<username>/`, archives source sessions under `data/raw/archive/YYYYMMDD/`, and regenerates the 60-second summary and plots.

The dashboard starts the watcher without pre-creating its PID file; `src/watcher.py` owns that file so adding/enabling streamers does not trigger a false ?謓ready running??exit.

The analyzer writes both window_start_local and window_start_utc; plotting prefers the Taiwan-time column. The collector finalizes a session after three consecutive authoritative is not currently live responses, while HTTP 429 and probe timeouts remain retryable.
## Analytics and health monitoring

The dashboard now includes multi-session comparison, gift leaderboards with single/repeat/mixed sending patterns, entry-source traffic analysis, and follow/share/subscribe summaries. Select multiple sessions from the sidebar to compare viewer, engagement, and monetization metrics.
Audience flow analysis now includes join rate per minute, viewer growth, viewer volatility, and early/mid/late live-phase comparisons. These are derived metrics from periodic viewer samples and member entry events.
Gift analysis now includes Top 1/5/10 diamond concentration, peak gift minute, and same-minute gift/chat/viewer relation.
Follow/share/subscribe analysis includes Taiwan-time per-minute trends, unique actors, observed events per minute, and events per 100 captured joins. Per-100-join values are explicitly labeled as proxies and are not causal or unique-viewer conversion rates.
The `Live tracking` tab refreshes the newest running session and shows current viewers, TikTok cumulative `totalLikes`, observed Like batches, captured diamonds, chat, gifts, and the latest event stream. Gift diamonds are derived from confirmed gift events (`diamondCount * repeatCount`); TikTok does not expose a room-wide cumulative diamond field in the current LIVE event payload.

`src/health_monitor.py` writes `health.json` with connection, reconnect, disconnect, event completeness, unknown-event, SDK error, and socket quality metrics. The watcher refreshes health reports every 60 seconds for active collectors and writes a final report after daily aggregation.

New Collector sessions also append `snapshots.ndjson` every 60 seconds and preserve WebSocket ranking events in `rankings.ndjson`. Set `TIKTOOL_API_KEY` to enable REST room-info, rankings, and gift-catalog snapshots. Optionally set `TIKTOK_COOKIE_HEADER` for authenticated ranking data. Secrets are read from environment variables and are never written to session files or logs. The Streamlit `Snapshots & rankings` tab supports both current snapshots and historical ranking changes.

Examples:

```powershell
.\.venv\Scripts\python.exe src\health_monitor.py --raw-root data\raw --all
.\.venv\Scripts\python.exe src\health_monitor.py --raw-root data\raw --session 20260914_103005_pubg.esports.official
```

# Reports UI

Select **Reports** in the sidebar Page control. Choose a streamer and Week,
Month, or Custom date range, then click **Generate report**. Weeks run Monday
through Sunday; all boundaries use Asia/Taipei. For the initial Chloe report,
select Custom and 2026-09-10 through 2026-09-16.

Reports include daily and room summaries, viewer and interaction trends,
captured Diamonds, gifter ranking, and observed entry sources. Download daily
CSV or an HTML report with charts (chart scripts require internet access).
Overlapping merged/archive/source events are deduplicated using source session
and sequence. Cross-midnight broadcasts remain grouped by room ID.

These are captured-data reports, not official TikTok totals. Viewer averages
are sample-based; first/last events are not confirmed broadcast duration.
Coverage ratio, scheduled generation, and official post-LIVE reconciliation
are not implemented in this first version. Reports are generated on demand
and remain in the browser session until regenerated.
