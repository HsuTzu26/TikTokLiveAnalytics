# TikTok LIVE 聊天室發送器

這是選用的本機瀏覽器操作功能。它不建立另一條 LIVE 收集連線，也不呼叫 TikTool 訊息發送 API；它使用 Playwright 操作專用 Chrome 中的 TikTok 網頁。

> 只在自己的直播間或已取得明確同意的直播間使用。請遵守 TikTok 當地適用的條款與規範。登入、驗證碼及任何安全檢查都必須由使用者手動處理；本工具不繞過驗證。

## 啟動與使用

1. 啟動 Streamlit，在側欄選擇「Chat sender」。
2. 按「開啟專用 Chrome」，在新視窗手動登入 TikTok。也可使用 **open_chat_sender.bat**。
3. 輸入 streamer username、單行訊息、發送間隔與排程上限，然後開啟指定直播間。
4. 確認 Chrome 位於指定的 LIVE 房間、帳號正確且直播影片正在播放，再按「檢查直播間」。
5. 勾選授權確認後，先按「確認單次發送」。只有在同一 streamer 的單次測試完成後，才能啟用定時發送。
6. 按「停止排程」只會停止聊天室排程，不會停止 Watcher 或 Collector。

訊息間隔最短 5 分鐘；單次排程上限 1–20 則（介面預設每 10 分鐘最多 3 則）。排程不會在 Worker 重啟後自動恢復。

## 發送狀態與限制

- **visible_echo** 表示頁面中觀察到相同文字，不保證 TikTok 伺服器已接受或所有觀眾都看得到。
- **submitted_unconfirmed** 表示已嘗試送出但無法確認。工具不會自動重送，避免重複留言；請先人工確認聊天室。
- 找不到唯一輸入框、直播結束、影片未播放、出現驗證或操作例外時，排程會停用並等待人工處理。
- 既有草稿不會被覆蓋；發送逾時不會觸發自動重試。
- 聊天室發送紀錄只記錄時間、操作、結果與 streamer username，不記錄留言全文或瀏覽器例外內容。

## 本機登入資料

專用 persistent Chrome profile 存在 **data/chat_sender/chrome_profile/**。其中可能含可登入 TikTok 的 Cookie 與其他瀏覽器狀態：

- 不要提交到 Git。
- 不要放入 ZIP、雲端硬碟或傳給他人。
- 若曾意外分享，請登出該 profile 並更新帳號安全憑證。

安裝與 Playwright 使用方式請見 [README](README.md) 與 [Playwright persistent context 文件](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)。
