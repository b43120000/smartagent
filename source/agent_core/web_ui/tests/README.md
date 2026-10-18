# DOM 元件功能測試

目前完成第一個元件：輸入框（composer）。尚未標註送出、訊息、回答與生成狀態。

整合入口：安裝根目錄 `adapterUI/1.writeSendBox.bat`。只貼一次 URL，在同一頁完成手動點選、儲存及自動驗證。根目錄 `adapterUI.bat` 的選單 2 也使用這個整合流程；以下獨立標記流程可用 `adapterUI.bat --manual` 呼叫。第 2～5 步入口與驗收方式見 `adapterUI/README.md`。

## 人工標記

1. 開啟安裝根目錄的 `adapterUI.bat --manual`。
2. 在視窗貼上 ChatGPT、Gemini、Claude 或 M365 URL。
3. 網頁開啟後，必要時完成登入。右上角按「開始選取」。
4. 點一下訊息輸入框，確認綠框範圍，再按「儲存輸入框」。
5. 設定儲存在 `web_ui/providers/<provider>/dom_config.json`。重新標記會將舊檔存入同目錄的 `dom_config.history/`。

選取期間只捕捉點擊，不會觸發網頁按鈕。選錯可重新選取；Esc 可離開選取狀態。「取消」或關閉頁面不儲存。

這份設定只覆蓋 provider 的 `composer_selector`，其他既有 profile 欄位繼續使用原設定；不代表整套對話已校準。SmartAgent 下次建立 adapter 時會載入。移走 `dom_config.json` 即恢復原來輸入框設定；損壞的人工設定會明確報錯。

有運作中的 ChatGPT/M365 CDP 瀏覽器時，工具建立自己的新分頁，完成後只關自己的分頁。沒有 CDP 時，使用 SmartAgent 原有瀏覽器登入設定；若該設定正在使用，請先關閉 SmartAgent 再操作，不要刪除瀏覽器鎖檔。

第一版支援主文件內的 textarea、文字 input、contenteditable（含點擊其子元素）。iframe 與網站自訂 Shadow DOM 輸入框尚未支援。只接受唯一定位，不儲存座標、臨時節點標記或位置序號。

## 獨立輸入框測試

開啟安裝根目錄的 `writeSendBox.bat`，貼上同一 provider 的 URL。工具會：

1. 重新開啟網頁並讀取該 provider 的 config。
2. 確認 runtime adapter 也載入同一個輸入框 selector。
3. 寫入內建範本文字，透過 provider 的 composer 驗證讀回結果。
4. 保留頁面供查看，按頁面右上角「關閉測試」結束。

不點送出、不按 Enter 送出。非測試範本的現有草稿會阻止測試；請使用空白對話或先自行清空。相同範本可重複測試。單行 input 使用單行範本。

工具自行啟動瀏覽器時，會沿用啟動時的空白分頁導向 URL，不另外留下空白頁。連接既有瀏覽器時仍開自己的分頁，保留使用者原有分頁。

M365 使用聚焦後的鍵盤輸入，讀回時排除編輯器標為 `aria-hidden` 的裝飾節點；使用者文字內的不可見字元仍會參與比對。初始化若丟棄輸入，只在確認輸入框完全空白時有限重試，並要求文字連續穩定至少 0.5 秒；部分文字或不同草稿不會被重填。失敗時會顯示 `DIAGNOSTICS` 路徑，內含結果 JSON 與截圖。

## 維護者執行方式

在安裝根目錄 PowerShell：

```powershell
$env:PYTHONPATH = "$PWD\source"
.\.venv\Scripts\python.exe -B -m unittest agent_core.web_ui.tests.test_manual_composer -v
```

元件測試使用攔截的測試頁面及暫存 config，不改動真實 provider 設定。

真實網站 IT（會更新該 provider 的真實輸入框 config，並以 `writeSendBox.bat` 重新開啟真實網頁）：

```powershell
.\.venv\Scripts\python.exe -B -m agent_core.web_ui.tests.live_composer_it --url https://chatgpt.com/
```

測試紀錄、結果 JSON、選取與寫入截圖存於 `localdata/logs/manual_dom_it/<UTC時間>/`。真人點選由 Playwright 的實際滑鼠點擊模擬；不靠模型產生 selector。自動 IT 要求頁面有且只有一個可見的 textarea/contenteditable，以免猜錯測試目標。

CLI 入口：

```powershell
.\adapterUI.bat --manual --url https://chatgpt.com/
.\writeSendBox.bat --url https://chatgpt.com/
```

兩個 Python 入口皆接受 `--cdp-endpoint`。`writeSendBox.bat` 額外接受 `--no-wait`（驗證後關頁）與 `--screenshot <path>`。自動化測試可設 `SMARTAGENT_TEST_NO_PAUSE=1` 跳過批次檔最後的 pause。
