# 聊天室發送（測試版）

1. Streamlit 側欄選擇 `Chat sender`，按「開啟專用 Chrome」。也可雙擊 `open_chat_sender.bat`。
2. 在專用 Chrome 手動登入 TikTok。不會使用／複製平常 Chrome 的登入設定檔。
3. 在 Streamlit 輸入 username，開啟指定直播間；手動確認帳號、直播與播放狀態，按「檢查直播間」。
4. 預覽訊息並勾選確認，按「確認單次發送」。這個按鈕會真的發送留言。
5. 頁面看到留言且狀態為 `visible_echo` 後，才可啟用排程。第一則在設定間隔後發送；不補發錯過的時段。

排程預設關閉，每次最多 20 則、間隔至少 5 分鐘。重啟不恢復排程；關閉專用 Chrome 結束 worker。
「停止排程」會阻止後續發送，但已在送出中的訊息無法撤回。整個 worker 不控制 Watcher。

`visible_echo` 僅代表 DOM 出現相同文字，不保證 TikTok 伺服器送達。
結果不明、無法確認影片播放、輸入框不唯一、驗證或下播都會停用排程，需人工檢查。
直播狀態偵測是保守的網頁啟發式，不是官方狀態來源；網頁改版可能須調整 selector。
不要繞過驗證或限制。若網站不允許自動化，應停用此功能。

本機登入資料、狀態與台灣時間紀錄存於 `data/chat_sender/`（已被 Git 忽略）。
不記錄 Cookie、完整頁面或瀏覽器錯誤內容。紀錄不自動從分析剔除，請避免用測試留言評估互動成效。

依賴安裝：`.venv\Scripts\python.exe -m pip install "playwright>=1.50,<2"`。
使用本機已安裝的 Chrome（`channel='chrome'`），不必下載 Playwright Chromium。
專用 persistent context 的依據：[Playwright BrowserType](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)。
