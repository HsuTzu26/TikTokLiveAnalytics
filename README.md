# TikTok LIVE Analytics

[English README](README.en.md) | 繁體中文

以 Python 與 Streamlit 建立的本機直播監控、事件收集與分析工具。主要目標是持續收集直播事件、保存原始紀錄，再以 dashboard、趨勢圖與報表呈現實際觀察到的資料。

> 本專案是獨立的社群專案，不是 TikTok 或 TikTool 的官方產品，也未獲其背書。TikTok、TikTok LIVE 等名稱與商標屬其各自權利人。

## 功能

- Watcher 監控多位 streamer，偵測直播狀態並管理 Collector；遇到暫時探測失敗或限流時採退避策略。
- Collector 透過上游 LIVE 事件 SDK 收集聊天室、進場、觀看人數、Like、Gift、Follow、Share、Subscribe 等可取得事件，保存 NDJSON。
- Streamlit dashboard 顯示即時狀態、直播趨勢、多場比較、觀眾進場來源、Gift 排行、系統健康度與歷史報表。
- 直播結束後可整理每日場次資料；支援 CSV 與含圖表的 HTML 報告。
- 可選擇啟用雙語聊天室 TTS；TTS 讀取既有 Collector 檔案，不會另外建立 TikTok 直播連線。
- 可選擇使用專用 Chrome 與 Playwright，在人工登入、確認與授權下操作聊天室發送器。

## 資料來源、API 與責任界線

| 功能 | 實際來源 | 本專案的使用方式與責任界線 |
|---|---|---|
| LIVE 即時事件 | TikTool 維護的 **tiktok-live-events Python SDK**，連線至 TikTool WebSocket 服務 **wss://api.tik.tools** | 這不是 TikTok 官方 LIVE API。本專案使用上游 SDK 收取服務提供的事件；事件種類、可用性、限制與 schema 變更會受上游服務影響，無法保證每場或每個事件完整。 |
| 房間、排行榜與 Gift catalog 快照 | TikTool REST API：**https://api.tik.tools** | **src/snapshots.py** 呼叫 **/webcast/room_info**、**/webcast/rankings**、**/webcast/gift_info**。需設定 **TIKTOOL_API_KEY** 才會啟用 REST 快照；服務方案、配額與回應由 TikTool 管理。 |
| 排名資料的可選登入資訊 | 使用者提供的 TikTok Cookie header | 只有設定 **TIKTOK_COOKIE_HEADER** 時，程式才會將它作為 **x-cookie-header** 傳給 TikTool API。Cookie 等同敏感登入憑證；請自行評估是否提供，勿提交至 Git，也勿分享含登入資料的瀏覽器設定檔。 |
| 直播狀態輔助探測 | TikTok 公開 LIVE 網頁 | 程式解析公開頁面 HTML 作為輔助資訊，不是穩定或官方 API；TikTok 改版、地區或流量限制可能使探測失敗。 |
| 聊天室訊息發送 | TikTok 網頁 UI + 使用者本機 Chrome，透過 Playwright 操作 | 不使用 TikTool 發送 API，也不宣稱呼叫 TikTok 官方發送 API。登入、驗證與直播間確認由使用者手動完成；請只在自己或已取得授權的直播間使用，並自行確認遵守平台條款與當地規範。 |
| 聊天室語音 | **edge-tts** 開源 Python 套件呼叫 Microsoft Edge 線上語音服務 | 不是本機離線語音。被接受並送去合成的留言文字會離開本機並傳給語音服務；該服務的可用性與限制不由本專案控制。 |

因此，本專案**沒有直接使用 TikTok 官方開發者 LIVE API**。TikTok 公開頁面與網頁 UI 是資料探測／人工登入操作來源；核心事件與快照依賴的是 TikTool 第三方 SDK／服務。請分別閱讀 TikTool API 條款、TikTok 平台規範及各軟體套件授權。

### API 金鑰與本機資料

- **TIKTOOL_API_KEY**：提供給 TikTool SDK／API；快照功能需要此值。不要寫入原始碼或提交到 Git。
- **TIKTOK_COOKIE_HEADER**：選用；排名請求會將其送至 TikTool。請把它視為可登入帳號的機密資料。
- 收集事件、Watcher 狀態、TTS 狀態與聊天室發送器資料存放在 **data/**，此目錄由 **.gitignore** 排除，不應推送至 GitHub。
- **data/chat_sender/chrome_profile/** 含專用 Chrome 的登入狀態與 Cookie，**不要壓縮分享或上傳**。
- TTS 會將通過篩選的聊天室文字送往 Edge 線上語音服務。使用前請考慮觀眾留言的隱私與告知義務。

## Windows 安裝與啟動

需要 Python 3.12。上游 SDK 是獨立 Git 專案，不會被複製進本 repo；先在此專案根目錄另外 clone：

~~~powershell
git clone https://github.com/tiktool/tiktok-live-events.git
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

啟動 dashboard：

~~~powershell
.\.venv\Scripts\python.exe -m streamlit run app\dashboard.py --server.address 127.0.0.1 --server.port 8501
~~~

也可以使用專案內的 **run_dashboard.bat**。在 Streamlit 側欄的 Watcher 頁面新增／啟用 streamer；個別 Collector 會由 Watcher 管理。不要另外重複啟動同一個 Collector。

執行測試：

~~~powershell
.\.venv\Scripts\python.exe -m pytest
~~~

## 分析數字如何解讀

- 報表呈現的是程式實際收到並寫入的事件，不是 TikTok 官方整場總數；開播後才開始收集、連線中斷、上游限流或資料來源缺漏都會讓數字偏低。
- Viewer 趨勢是定時樣本；觀眾進場事件不等於不重複觀眾，也無法推算每位觀眾完整觀看時長。
- Diamonds 由已捕獲 Gift 事件彙算，不等同 TikTok 後台的最終結算或主播實際可提領收入。
- 連線健康度反映 Collector 的觀察期間，不是整場直播涵蓋率保證。

所有報表日期與圖表時間以 Asia/Taipei 為主。原始事件、系統狀態與快照以 NDJSON／JSON 存放於 **data/**；該資料目錄不屬於 GitHub 原始碼 repo。

## 專案文件與參考資料

### 專案內文件

- [專案規劃與分析](TikTok_LIVE_Analytics_Project_Plan.md)
- [雙語即時 TTS 技術設計（專案提供的 DOCX）](docs/TikTok_LIVE_RealTime_Bilingual_TTS_Technical_Design.docx)
- [TTS 執行方式與限制](docs/TTS_RUNTIME.md)
- [聊天室發送器操作與安全說明](CHAT_SENDER.md)

### 上游專案與技術文件

- [TikTool tiktok-live-events 原始碼與 Python SDK 文件](https://github.com/tiktool/tiktok-live-events)
- [TikTool LIVE API 文件](https://tik.tools/docs) 與 [WebSocket 指南](https://tik.tools/guides/tiktok-live-websocket)
- [Playwright Python：persistent browser context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [edge-tts 開源套件](https://github.com/rany2/edge-tts)
- [Streamlit 官方文件](https://docs.streamlit.io/)
- [Altair 官方文件](https://altair-viz.github.io/)

## 致謝

感謝 TikTool 團隊維護 tiktok-live-events SDK 與 API 文件；感謝 edge-tts、Streamlit、Playwright、Pandas、Altair、Matplotlib 與 Pygame 的維護者及開源貢獻者。本專案使用各上游提供的程式與文件，並不擁有或重新授權其服務、商標或第三方元件。

## 授權

本專案原創程式碼依 [MIT License](LICENSE) 授權。上游 SDK／套件、專案內引用或由專案提供者提供的參考文件，以及直播資料與媒體，仍依各自權利人與適用條款處理；根目錄 MIT 不會自動改變其授權，也不授予 TikTok 或 TikTool 服務、資料與商標的權利。
