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

- `src/collector_v0_3_1.py`：長時間收集器（目前 `watchlist.json` 使用的版本）。
- `src/watcher.py`：依 `watchlist.json` 探測直播並啟停收集器。
- `src/analyzer.py`：將 `events.ndjson` 彙總成時間序列 CSV 與摘要 JSON。
- `src/validate_session.py`：檢查禮物連刷與 diamond 計算一致性。
- `src/plot_session.py`：由分析 CSV 產生 PNG 圖表。

範例：

```powershell
python src\collector_v0_3_1.py <username>
python src\watcher.py --config watchlist.json
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
