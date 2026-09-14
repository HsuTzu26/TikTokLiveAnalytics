# TikTok Live Analytics

隞?Python ?園? TikTok LIVE ?單?鈭辣嚗蒂撠?剛???摮 NDJSON嚗??Ｙ?敶蜇?旨?拚?霅??”??

## ??啣?

撠???雿輻 Python 3.12 ??桅???`.venv`嚗?

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
```

`tiktok-live-events` ?舐蝡?撌Ｙ? Git ?澈嚗?隞?editable install ??
`tiktok_live_events` 璅∠?嚗耨??SDK ??蝣澆?銝??????

## 銝餉??亙

- `src/collector.py`嚗???園??剁??桀? `watchlist.json` 雿輻???穿???
- data/raw/<session>/raw_events.ndjson嚗???SDK ? event callback ???游?憪?payload??
- `src/watcher.py`嚗? `watchlist.json` ?Ｘ葫?湔銝血?????
- `src/analyzer.py`嚗? `events.ndjson` 敶蜇??????CSV ??閬?JSON??
- `src/validate_session.py`嚗炎?亦旨?拚???diamond 閮?銝?湔扼?
- `src/plot_session.py`嚗?? CSV ?Ｙ? PNG ?”??

蝭?嚗?

```powershell
python src\collector.py <username>
python src\watcher.py --config watchlist.json
python -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
python src\analyzer.py data\raw\<session>
python src\validate_session.py data\raw\<session>
python src\plot_session.py data\raw\<session>
```

?園?蝯?撖怠 `data/raw/<session>/`?府?桅???watcher log/state ?賣?祆??Ｙ嚗?
銝??交 Git ?澈??

## ??批

?寧??Git ?芾蕭頩文???撘身摰??辣嚗tiktok-live-events/` 靽??芸楛??Git
甇瑕???刻??游??炎?伐?

```powershell
git status
git -C tiktok-live-events status
```

?桀?銝??芸??漱???靽格撌Ｙ? SDK ?澈????氬?
## Runtime behavior

- Watcher log timestamps use Asia/Taipei; event/session files keep both UTC and local timestamps.
- probe_timeout and HTTP 429 are non-authoritative. When no collector is active, the watcher starts a resilient collector candidate that keeps retrying with exponential backoff (up to 60 seconds).
- Only the SDK response is not currently live is treated as authoritative offline.
- Legacy collector versions are kept under src/legacy/; src/collector.py is the only supported collector entry point.

## Dashboard ????

?? Streamlit嚗?

```powershell
python -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
```

瘥?session ???`collector.log`嚗atcher ?神??`data/watcher/watcher.log`??
Probe timeout ??HTTP 429 ?芣?閮?銝虫??摨?Collector嚗??孛?澆?甇Ｕ?

## Dashboard analysis

The Streamlit dashboard displays all event times in Asia/Taipei. Select a streamer, then either choose an available session or enter its exact Session ID (for example 20260913_195928_chloe_o723_). Trend charts use timestamped X axes with explicit Taiwan-time and metric labels.

The merge_daily_sessions.py utility can consolidate sessions into one daily folder per streamer and archives source folders under data/raw/archive/ while preserving source_session_id on every record.

The sidebar also accepts a new streamer username and updates watchlist.json; the running watcher reloads it on the next polling cycle.
Use ?emove from watchlist??to delete a streamer from future tracking; existing raw sessions are intentionally preserved. When a collector exits with `live_end` or `offline_confirmed`, the watcher automatically refreshes `data/raw/YYYYMMDD_<username>/`, archives source sessions under `data/raw/archive/YYYYMMDD/`, and regenerates the 60-second summary and plots.

The dashboard starts the watcher without pre-creating its PID file; `src/watcher.py` owns that file so adding/enabling streamers does not trigger a false ?lready running??exit.

The analyzer writes both window_start_local and window_start_utc; plotting prefers the Taiwan-time column. The collector finalizes a session after three consecutive authoritative is not currently live responses, while HTTP 429 and probe timeouts remain retryable.
## Analytics and health monitoring

The dashboard now includes multi-session comparison, gift leaderboards with single/repeat/mixed sending patterns, entry-source traffic analysis, and follow/share/subscribe summaries. Select multiple sessions from the sidebar to compare viewer, engagement, and monetization metrics.
Audience flow analysis now includes join rate per minute, viewer growth, viewer volatility, and early/mid/late live-phase comparisons. These are derived metrics from periodic viewer samples and member entry events.
Gift analysis now includes Top 1/5/10 diamond concentration, peak gift minute, and same-minute gift/chat/viewer relation.
Follow/share/subscribe analysis includes Taiwan-time per-minute trends, unique actors, observed events per minute, and events per 100 captured joins. Per-100-join values are explicitly labeled as proxies and are not causal or unique-viewer conversion rates.
The `Live tracking` tab refreshes the newest running session and shows current viewers, TikTok cumulative `totalLikes`, observed Like batches, captured diamonds, chat, gifts, and the latest event stream. Gift diamonds are derived from confirmed gift events (`diamondCount * repeatCount`); TikTok does not expose a room-wide cumulative diamond field in the current LIVE event payload.

`src/health_monitor.py` writes `health.json` with connection, reconnect, disconnect, event completeness, unknown-event, SDK error, and socket quality metrics. The watcher refreshes health reports every 60 seconds for active collectors and writes a final report after daily aggregation.

Examples:

```powershell
.\.venv\Scripts\python.exe src\health_monitor.py --raw-root data\raw --all
.\.venv\Scripts\python.exe src\health_monitor.py --raw-root data\raw --session 20260914_103005_pubg.esports.official
```

