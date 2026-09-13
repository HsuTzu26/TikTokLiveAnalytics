# TikTok LIVE Analytics 專案修正與開發規劃表

> 專案目標：建立可長時間穩定監控指定 TikTok 主播的 LIVE Analytics 系統，能自動偵測開播／關播、持續收集 Viewer／Join／Chat／Like／Gift／Follow 等事件，並透過 Streamlit 即時顯示與查詢歷史直播紀錄。

## 1. 目前專案狀態

| 模組 | 目前狀態 | 判定 |
|---|---|---|
| Viewer 收集 | 已可收集 | ✅ |
| Join / Entry Source | 已可收集 | ✅ |
| Chat | 已可收集 | ✅ |
| Like | 已可收集 | ✅ |
| Gift / Diamond | 已可收集 | ✅ |
| Follow / Social | 已可收集 | ✅ |
| User metadata | 已可收集 | ✅ |
| Connection metadata | 已可收集 | ✅ |
| Keepalive patch | 已證實可大幅改善 socket stability | ✅ |
| Watcher v0.2 lifecycle | LIVE 後停止額外 probe 的方向正確 | ✅ |
| Streamlit Dashboard | 第一版架構已建立 | ✅ / Prototype |
| Event dedup | Chat / Gift dedup key 存在 correctness bug | ❌ P0 |
| 30 秒 window gap handling | Disconnect 時段可能被誤解成 0 event | ❌ P0 |
| Viewer Average | 目前為 sample mean，需改 time-weighted ACU | ⚠️ P0 |
| Logical LIVE session | 目前主要依 room_id，仍需更穩定 session identity | ⚠️ P1 |
| Live End detection | 仍可加強 control-based detection | ⚠️ P1 |
| Watcher single-instance protection | 尚未加入 | ⚠️ P1 |
| Dependency / environment reproducibility | keepalive patch 尚未正式版本化 | ⚠️ P1 |
| Automated tests | 尚未建立 | ❌ |
| SQLite / persistent analytics index | 尚未建立 | ⏳ P2 |

## 2. 開發優先順序

### Phase 1 — P0：資料正確性修正

> 原則：在下一場正式長時間直播收集前完成。

| 項目 | 修改內容 | 目的 | 驗收標準 |
|---|---|---|---|
| P0-1 Event Dedup | 改為 `event_type + msg_id` 為 primary identity | 避免真正 Chat / Gift 被錯誤刪除 | 已知 regression sample 的 Chat / Gift totals 與 raw `msg_id` dedup 一致 |
| P0-2 Gift Dedup | `transaction_id` 僅作 transaction correlation，不再作唯一 dedup key | 保留 streak intermediate / repeatEnd event | repeatEnd 不會因同 transaction_id 被丟棄 |
| P0-3 Dashboard Dedup | Dashboard 使用與 Collector 完全相同的 identity logic | 避免 Collector 正確但 Dashboard 再次錯算 | Collector / Dashboard 統計一致 |
| P0-4 Shared Event Identity | 建立 `src/event_identity.py` | 避免多份 dedup 邏輯 diverge | Collector / Dashboard / Merger 共用 |
| P0-5 Coverage-aware Windows | 30 秒 window 新增 `coverage_seconds`, `coverage_ratio`, `valid_window` | Disconnect gap 不再被當成 0 engagement | coverage 不足的 window 標示 invalid |
| P0-6 Viewer ACU | 改為 time-weighted average viewer | 提供較合理的 Average Concurrent Viewers | 使用 viewer × Δt / total observed time |
| P0-7 Validator 強化 | 新增 incomplete streak、open streak、duplicate msg_id 等檢查 | Validator 不只驗 arithmetic | 可以辨識「資料缺失」與「計算錯誤」 |

### Phase 1 預期輸出

```text
src/
├── collector.py
├── event_identity.py
├── analytics.py
└── validator.py

tests/
├── test_event_identity.py
├── test_chat_dedup.py
├── test_gift_dedup.py
├── test_gift_streak.py
└── test_coverage.py
```

## 3. Phase 2 — Watcher / Session Lifecycle 穩定化

> 目標：讓一場 3–5 小時直播盡量維持為一個 logical LIVE session。

| 項目 | 修改內容 | 目的 | 驗收標準 |
|---|---|---|---|
| P1-1 Watcher State Machine | `WAITING → LIVE → COLLECTING → END → WAITING` | 避免 LIVE 中額外 probe | Collecting 狀態下不再發 LIVE probe |
| P1-2 Collector-owned Reconnect | Disconnect 僅由 Collector 處理 | 不因短暫斷線重啟整個 session | reconnect 不建立新 logical session |
| P1-3 Logical Session ID | Watcher 在開播時產生 `logical_session_id` | 同一場即使 Collector restart 仍可合併 | 相同直播所有 segment 共用 ID |
| P1-4 Segment Model | Collector restart 時寫入 `segments/001`, `002`… | 保留 raw forensic trace | Dashboard 視為同一場 LIVE |
| P1-5 Single-instance Lock | `watcher.lock + PID` | 防止重複啟動兩個 Watcher | 第二個 Watcher 自動拒絕啟動 |
| P1-6 Crash Recovery | Collector crash → 等待 → 單次 probe → restart or wait | 避免永久中斷 | Collector crash 後可自動恢復 |
| P1-7 Live End Detection | 優先 Control / LiveEnd signal，文字 error 作 fallback | 關播後快速 finalize | 關播後不持續無限 reconnect |
| P1-8 Restart Backoff | 對 repeated crash / 429 設定 delay | 避免 restart storm | 不會短時間大量重啟 |

### 建議 Logical Session 結構

```text
data/
└── raw/
    └── chloe_o723/
        └── <logical_session_id>/
            ├── manifest.json
            └── segments/
                ├── 001/
                │   ├── events.ndjson
                │   ├── users.ndjson
                │   ├── diagnostics.ndjson
                │   └── session.json
                ├── 002/
                └── ...
```

## 4. Phase 3 — Streamlit Dashboard v1

> Streamlit 只做 UI / Analytics，不直接擁有 TikTok WebSocket。

### 即時監控頁面

| KPI | 顯示內容 |
|---|---|
| LIVE Status | LIVE / OFFLINE |
| Current Viewer | 最新 viewer count |
| Average Viewer | Time-weighted observed ACU |
| Peak Viewer | Observed PCU |
| Coverage | Whole-session / observed coverage |
| Likes | 累計 Likes |
| Unique Likers | 不重複按讚者 |
| Join | Join events |
| Unique Entrants | 不重複進房者 |
| Comments | Chat count |
| Unique Chatters | 不重複留言者 |
| New Follows | Follow events / unique follow users |
| Diamonds | Confirmed observed Diamonds |
| Gifts | Counted gifts |
| Unique Gifters | 不重複送禮者 |

### 即時圖表

```text
Viewer timeline
Join / Entry timeline
Likes / 30 sec
Chat / 30 sec
Gift / Diamonds / 30 sec
Cumulative Diamonds
Coverage timeline
```

### 分析 Tabs

```text
Overview
Traffic
Engagement
Gift
Audience
Data Quality
Recent Chat
```

### 歷史紀錄

每場 LIVE 顯示：

```text
Date
Start / End
Duration
Logical Session ID
Room ID(s)
ACU
PCU
Likes
Unique Entrants
Comments
New Follows
Diamonds
Unique Gifters
Coverage
Data Quality Status
```

## 5. Phase 4 — Session Merger / Processed Dataset

> Raw data 永遠不直接修改，分析資料另外生成。

### Input

```text
raw logical session
    ↓
all segments
```

### Processing

```text
global dedup
timestamp sorting
connection interval reconstruction
coverage calculation
gift streak validation
user merge
30-second aggregation
```

### Output

```text
data/
└── processed/
    └── chloe_o723/
        └── <logical_session_id>/
            ├── live_summary.json
            ├── merged_events.ndjson
            ├── merged_users.ndjson
            ├── timeseries_30s.csv
            ├── traffic_sources.csv
            ├── gifts.csv
            ├── gifters.csv
            └── data_quality.json
```

## 6. Phase 5 — Data Quality Standard

### Session Quality Classification

| Coverage | 等級 | 建議用途 |
|---:|---|---|
| `< 80%` | Incomplete | Debug / exploratory only |
| `80–90%` | Partial | 部分描述性分析 |
| `90–95%` | Usable | 一般分析 |
| `≥ 95%` | High Confidence | 正式 LIVE KPI |

### 每場必須保存

```text
collector_total_seconds
socket_connected_seconds
socket_uptime_ratio
whole_session_coverage
socket_gap_seconds
reconnect_count
segment_count
duplicate_events_dropped
sdk_error_count
invalid_30s_windows
open_gift_streaks
```

## 7. Phase 6 — SQLite Analytics Store

> 當資料量開始累積後導入；Raw NDJSON 仍保留。

### Raw

```text
NDJSON
= source of truth
```

### SQLite

```text
sessions
segments
users
events_index
timeseries_30s
traffic_sources
gift_summary
gifter_summary
```

### 目的

- Streamlit 不需要每 5 秒重新掃描所有 NDJSON。
- 即時 Dashboard 查詢速度更快。
- 容易進行跨場直播比較。
- 完全本機，無需額外雲端成本。

## 8. 專案結構整理

完成 P0/P1 後，不再使用：

```text
collector_v0_3.py
collector_v0_3_1.py
collector_v0_3_2.py
watcher_v0_2.py
...
```

改為：

```text
TikTokLiveAnalytics/
│
├── src/
│   ├── collector.py
│   ├── watcher.py
│   ├── analytics.py
│   ├── validator.py
│   ├── event_identity.py
│   ├── session.py
│   └── storage.py
│
├── app/
│   └── dashboard.py
│
├── tests/
│   ├── fixtures/
│   ├── test_event_identity.py
│   ├── test_gifts.py
│   ├── test_coverage.py
│   └── test_session.py
│
├── data/
│   ├── raw/
│   ├── processed/
│   └── watcher/
│
├── archive/
│   └── prototypes/
│
├── watchlist.json
├── requirements.txt
└── README.md
```

版本交由 Git 管理，而不是靠檔名 `_v0_3_2`。

## 9. Environment / Reproducibility

正式固定：

```text
Python 3.12
tiktok-live-events 1.2.4
websockets 13.1
tzdata
streamlit
pandas
matplotlib
```

Keepalive：

```text
ping_interval=None
```

必須正式固化到 repository / patch，而不是只修改目前 `.venv`。

## 10. 建議執行順序

```text
STEP 1
修 Event Identity / Dedup
        ↓
STEP 2
修 Gift streak + Validator
        ↓
STEP 3
加入 coverage-aware windows
        ↓
STEP 4
Viewer 改 time-weighted ACU
        ↓
STEP 5
重構 Watcher state machine
        ↓
STEP 6
加入 logical_session_id
        ↓
STEP 7
加入 Watcher lock / crash recovery
        ↓
STEP 8
完成 Streamlit Dashboard v1
        ↓
STEP 9
完整跑 Chloe 一場 LIVE
        ↓
STEP 10
確認 Coverage ≥ 95%
        ↓
STEP 11
建立 Session Merger / Processed Dataset
        ↓
STEP 12
資料量增加後導入 SQLite
```

## 11. 下一場正式測試驗收條件

- [ ] 從開播自動偵測並啟動 Collector
- [ ] Collecting 時 Watcher 不再額外 probe
- [ ] WebSocket reconnect 不建立新 logical session
- [ ] Collector restart 仍保留相同 `logical_session_id`
- [ ] Chat dedup 不再依 `message_uuid`
- [ ] Gift dedup 不再依單一 `transaction_id`
- [ ] Gift streak `repeatEnd` 正確保存
- [ ] 30 秒 window 能識別 disconnect gap
- [ ] Average Viewer 使用 time-weighted ACU
- [ ] Dashboard 即時更新
- [ ] 關播後 Collector 正常 finalize
- [ ] Watcher 自動回到 WAITING
- [ ] Whole-session coverage ≥ 90%，目標 ≥ 95%
- [ ] Likes / Gifts / Diamonds / Follows / Viewer 可輸出完整 summary
- [ ] 歷史 session 可在 Dashboard 回看

## 12. 最終目標

```text
指定 TikTok ID
       ↓
Watcher 長時間掛著
       ↓
自動偵測 LIVE
       ↓
建立 Logical LIVE Session
       ↓
Collector 持續收集
       ↓
Disconnect 自動 reconnect
       ↓
Streamlit 即時顯示
       ↓
主播關播
       ↓
自動 finalize
       ↓
Session Merger / Validation
       ↓
LIVE Analytics Report
       ↓
保存歷史紀錄
       ↓
Watcher 繼續等待下一場
```

最終核心分析維度：

```text
TRAFFIC
Viewer / ACU / PCU / Join / Entry Source

ENGAGEMENT
Like / Unique Liker / Chat / Follow

MONETIZATION
Gift / Diamond / Unique Gifter / Gifter Concentration

AUDIENCE
User / Badge / Fan / Gifter Level

QUALITY
Coverage / Disconnect / Reconnect / Missing Windows
```

## 13. 專案當前最重要原則

1. **Correctness before features**：先確保 event 不會被錯誤 dedup。
2. **Raw data immutable**：Raw event 永遠保留，不直接覆寫。
3. **Logical session ≠ process**：Collector restart 不代表新直播。
4. **Disconnect ≠ Offline**：socket disconnect 不應直接結束 LIVE session。
5. **Missing ≠ Zero**：沒有連線的時間不能視為 0 event。
6. **Coverage-aware analytics**：所有 LIVE KPI 都必須搭配 coverage。
7. **Watcher owns lifecycle; Collector owns connection; Streamlit owns presentation.**
