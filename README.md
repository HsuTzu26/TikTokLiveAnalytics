# TikTok Live Analytics

以 Python 收集 TikTok LIVE 即時事件，並將直播資料保存為 NDJSON，再產生彙總、禮物驗證與圖表。

## 開發環境

專案預期使用 Python 3.12 與根目錄的 `.venv`：

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
```

`tiktok-live-events` 是獨立的巢狀 Git 倉庫，會以 editable install 提供
`tiktok_live_events` 模組；修改 SDK 原始碼後不需重新打包。

## 主要入口

- `src/collector.py`：長時間收集器（目前 `watchlist.json` 使用的版本）。
- data/raw/<session>/raw_events.ndjson：保留 SDK 通用 event callback 的完整原始 payload。
- `src/watcher.py`：依 `watchlist.json` 探測直播並啟停收集器。
- `src/analyzer.py`：將 `events.ndjson` 彙總成時間序列 CSV 與摘要 JSON。
- `src/validate_session.py`：檢查禮物連刷與 diamond 計算一致性。
- `src/plot_session.py`：由分析 CSV 產生 PNG 圖表。

範例：

```powershell
python src\collector.py <username>
python src\watcher.py --config watchlist.json
python -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
python src\analyzer.py data\raw\<session>
python src\validate_session.py data\raw\<session>
python src\plot_session.py data\raw\<session>
```

收集結果寫入 `data/raw/<session>/`。該目錄及 watcher log/state 都是本機產物，
不納入根 Git 倉庫。

## 版本控制

根目錄 Git 只追蹤分析程式、設定與文件；`tiktok-live-events/` 保留自己的 Git
歷史。請在變更前先檢查：

```powershell
git status
git -C tiktok-live-events status
```

目前不會自動提交、推送或修改巢狀 SDK 倉庫的既有變更。
## Runtime behavior

- Watcher log timestamps use Asia/Taipei; event/session files keep both UTC and local timestamps.
- probe_timeout and HTTP 429 are non-authoritative. When no collector is active, the watcher starts a resilient collector candidate that keeps retrying with exponential backoff (up to 60 seconds).
- Only the SDK response is not currently live is treated as authoritative offline.
- Legacy collector versions are kept under src/legacy/; src/collector.py is the only supported collector entry point.

## Dashboard 與記錄

啟動 Streamlit：

```powershell
python -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
```

每個 session 會產生 `collector.log`；Watcher 會寫入 `data/watcher/watcher.log`。
Probe timeout 或 HTTP 429 只會記錄並保留健康 Collector，不會觸發停止。

## Dashboard analysis

The Streamlit dashboard displays all event times in Asia/Taipei. Select a streamer, then either choose an available session or enter its exact Session ID (for example 20260913_195928_chloe_o723_). Trend charts use timestamped X axes with explicit Taiwan-time and metric labels.

The merge_daily_sessions.py utility can consolidate sessions into one daily folder per streamer and archives source folders under data/raw/archive/ while preserving source_session_id on every record.

The sidebar also accepts a new streamer username and updates watchlist.json; the running watcher reloads it on the next polling cycle.
Use “Remove from watchlist” to delete a streamer from future tracking; existing raw sessions are intentionally preserved. When a collector exits with `live_end` or `offline_confirmed`, the watcher automatically refreshes `data/raw/YYYYMMDD_<username>/`, archives source sessions under `data/raw/archive/YYYYMMDD/`, and regenerates the 60-second summary and plots.

The dashboard starts the watcher without pre-creating its PID file; `src/watcher.py` owns that file so adding/enabling streamers does not trigger a false “already running” exit.

The analyzer writes both window_start_local and window_start_utc; plotting prefers the Taiwan-time column. The collector finalizes a session after three consecutive authoritative is not currently live responses, while HTTP 429 and probe timeouts remain retryable.
## Analytics and health monitoring

The dashboard now includes multi-session comparison, gift leaderboards with single/repeat/mixed sending patterns, entry-source traffic analysis, and follow/share/subscribe summaries. Select multiple sessions from the sidebar to compare viewer, engagement, and monetization metrics.
Audience flow analysis now includes join rate per minute, viewer growth, viewer volatility, and early/mid/late live-phase comparisons. These are derived metrics from periodic viewer samples and member entry events.
The `Live tracking` tab refreshes the newest running session and shows current viewers, TikTok cumulative `totalLikes`, observed Like batches, captured diamonds, chat, gifts, and the latest event stream. Gift diamonds are derived from confirmed gift events (`diamondCount * repeatCount`); TikTok does not expose a room-wide cumulative diamond field in the current LIVE event payload.

`src/health_monitor.py` writes `health.json` with connection, reconnect, disconnect, event completeness, unknown-event, SDK error, and socket quality metrics. The watcher refreshes health reports every 60 seconds for active collectors and writes a final report after daily aggregation.

Examples:

```powershell
.\.venv\Scripts\python.exe src\health_monitor.py --raw-root data\raw --all
.\.venv\Scripts\python.exe src\health_monitor.py --raw-root data\raw --session 20260914_103005_pubg.esports.official
```

