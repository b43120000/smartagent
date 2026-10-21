# DOM 校準入口

完全無法使用點選校準時，請閱讀 [DOM 手動取得與填寫指南](dom.md)：依第 1～15 項功能查找 selector、設定檔位置與手動驗證方式。

每一個元件用一個批次檔完成「輸入 URL → 點選元件 → 儲存設定 → 自動功能驗證」。第 8～10 步沿用前面設定，在右上角按「開始驗證」。第 6～10 步支援 ChatGPT、Gemini、Claude、M365 Copilot 與 Other URL；貼上網址後自動選擇各自的 provider 設定。

## 6～10：純文字對話流程

先完成第 1～5 步，再依序執行以下 BAT。全部沿用 `Edit_workspace.bat` 的瀏覽器設定，與 Agent 共用登入，工具結束只關閉自己建立的分頁。使用測試對話與空白輸入框；這些測試會真的送出訊息。

| 入口 | 操作與通過標準 |
|---|---|
| `6.assistantContent.bat` | 點選模型回答的完整文字區，包含全部段落與程式碼，不含下方複製、評分等按鈕。兩輪不同標記驗證正文完整讀回及新回答歸屬。設定為相對於第 5 步回答容器的 selector。 |
| `7.stopButton.bat` | 工具先發出長回答測試；在生成期間點選「停止」按鈕，再按「儲存並測試」。程式實際按一次停止，確認生成控制消失，填入未送出的草稿確認送出按鈕恢復可用，再清除草稿。 |
| `8.generationState.bat` | 按「開始驗證」，自動觀察待機→生成→待機，並確認本輪正文持續穩定至少 5 秒才判定完成。沿用正文、停止按鈕及既有忙碌訊號，不需重新框選。 |
| `9.blockingState.bat` | 正常頁面按「開始驗證」，確認預設阻擋規則不誤判；若網站當下真的出現阻擋，可選「標記目前阻擋視窗」。支援登入失效、限流、對話已滿、斷線及生成錯誤；遇到阻擋交由使用者處理，不自動重送。 |
| `10.conversationLoopIT.bat` | 按「開始驗證」，讀取第 1～9 步設定，自動跑兩輪完整文字對話：各送一次、確認本輪使用者訊息、讀取對應新回答、驗證段落與程式碼、確認完成。兩輪識別資訊必須不同且不能讀到上一輪答案。 |

第 9 步不會刻意登出或觸發限流。真實阻擋未出現時，結果明確記錄 `real_blocker_observed: false`、`positive_validation: fixture_only`：真實頁面只通過正常狀態驗證，五類阻擋的正向案例由受控測試頁驗證，不能視為真實網站各類錯誤皆已實測。

設定存於 `source/agent_core/web_ui/providers/<provider>/dom_config.json`；Other URL 依來源存於 `providers/other/other_<來源雜湊>/dom_config.json`。第 6、7 步寫入 `elements.assistant_content`、`elements.stop`；第 8～10 步寫入 `capabilities.generation_state`、`blocking_state`、`conversation_loop`。Agent runtime 會載入正文、停止控制、完成等待時間與阻擋規則。修改前面的元件後，相關完成／整輪驗證會失效，需重新跑第 8、10 步。第 10 步是驗證入口，不新增另一個 DOM 元件。

每個網站須先完成自己的第 1～5 步。第 6 步可先從本輪回答外框的內容判斷是否已能選取，再由你點選正文並驗證，不要求事先已有正文 selector。第 7 步也不要求網站的停止按鈕已符合內建 selector；請在生成期間直接標記，再由所選控制驗證停止效果。第 9 步可選一般錯誤提示框，不限具有 `role=alert/dialog` 的元素，且不會把已校準的對話區內引用的錯誤訊息當成網站錯誤。

第 6～9 步在頁面開啟後立即顯示右上角提示窗，列出本步操作與三種狀態：紅色「選取不符」、黃色「初步檢查通過，待功能驗證」、綠色「功能驗證通過」。第 6 步等待輸入框、送出測試問題及等待本輪回答期間也會顯示進度，準備完成才切換到正文選取面板。第 7 步準備長回答後切換到停止按鈕選取面板。儲存後提示窗保留，直到功能測試通過或顯示失敗原因；失敗時按「關閉」才結束，不會立即消失。

第 8 步沿用第 6、7 步已選的元件，自動觀察狀態，不需重新框選；第 9 步可驗證正常頁面，或手動標記實際阻擋提示。黃色只代表初步條件符合，仍須等功能驗證完成，才能視為本步通過。

支援入口與共用流程不代表各真實網站均已驗證通過。ChatGPT 已有真實 Web IT 紀錄；其他網站及 Other URL 請依序手動執行第 6～10 步，以各步的「功能驗證通過」及第 10 步成功紀錄為準。訊息歸屬或識別資訊若不足，仍會停止並留下證據，不會放寬成只要讀到相同文字就算成功。

這一組流程不包含檔案上傳、下載、圖片生成或工具執行的端到端驗證。

保留的測試腳本：

- `source/agent_core/web_ui/tests/test_dialogue_calibration.py`：五類網站的受控瀏覽器頁面跑第 6～10 步、真正選取面板與初步檢查、實際停止、設定失效規則、五類阻擋正向與負向案例、訊息內引用錯誤文字排除。
- `source/agent_core/web_ui/tests/live_dialogue_it.py`：實際啟動編號 BAT 並在 ChatGPT 選取及驗證；保存結果與截圖於 `localdata/logs/manual_dom_it/dialogue_*/`。

```powershell
$env:PYTHONPATH = "$PWD\source"
.\.venv\Scripts\python.exe -B -m agent_core.web_ui.tests.test_dialogue_calibration
.\.venv\Scripts\python.exe -B -m agent_core.web_ui.tests.live_dialogue_it --url https://chatgpt.com/ --stages 6 7 8 9 10
```

## 1.writeSendBox.bat：輸入框

1. 雙擊 `1.writeSendBox.bat`，貼上目標網頁 URL。
2. 必要時先登入，按網頁右上角「開始選取」。
3. 點一下訊息輸入框，確認綠框，再按「儲存輸入框」。
4. 同一個網頁自動填入範本並讀回驗證，不送出訊息。
5. 成功後在頁面顯示「輸入測試成功」，按「關閉測試」結束。

設定放在 `source/agent_core/web_ui/providers/<provider>/dom_config.json`。既有草稿不會被覆寫，請使用空白輸入框。錯誤時終端機會顯示原因及可用的診斷紀錄路徑。

### Other URL（其他網站）

同樣執行 `1.writeSendBox.bat`，直接貼上四個既有 provider 以外的 HTTP/HTTPS 網址即可，不需新增 Python provider。網址輸入視窗已列出 Other URL，程式會自動判別。第 2～5 步沿用相同入口與順序。

其他網站設定存放於 `source/agent_core/web_ui/providers/other/other_<網站來源 SHA256>/dom_config.json`。網站來源按協定、主機與連接埠區分；同站不同對話路徑共用設定，不同來源分開保存。設定內的 `source_origin` 可確認對應網站。瀏覽器登入資料也按來源隔離；登入完成回到原網站後再選取。

通用 web_ui adapter 只使用該網站已校準的 selector，沒有設定時會要求先校準。WebCopilot 可載入 Other 網站自己的 DOM 設定，使用已校準的輸入框與送出鈕擷取使用者輸入；至少先完成第 1、2 步，其餘對話能力仍需依序驗證。此支援不會把 Other 網站冒充成 ChatGPT，也不會新增通用模型選單項目。第 1～5 步仍不包含停止控制、最終回答內容與完整回覆完成判定，不能視為任意網站已完整適配。

Gemini 等啟用 Trusted Types 的頁面，標記面板以 DOM API 建立，不使用 `innerHTML`，也不修改網站的安全政策。

資料夾內的批次檔依功能順序命名。根目錄的 `writeSendBox.bat` 是「使用既有 config，只做輸入驗證」；根目錄 `adapterUI.bat` 的選單 2 也能進入第 1 步整合流程。

## 2～5：送出與訊息校準

每個批次檔只貼一次 URL，載入前面已完成的設定，在同一分頁完成點選與功能驗證。第 2～5 步會真的送出無害的校準問題，請使用測試對話；不會在送出結果不明時自動重送。

| 入口 | 需要先完成 | 操作與驗證 |
|---|---|---|
| `2.sendButton.bat` | 1 | 自動填入問題，點選可用送出按鈕；按「儲存並測試」才送出。確認問題出現在對話、模型回覆測試標記，觀察控制變化，並填入下一則草稿驗證按鈕恢復可用；最後清除這則未送出的測試草稿。 |
| `3.userTurn.bat` | 1、2 | 自動送出問題，點選整則使用者訊息容器；可擴大／縮小範圍。再次送出新問題，確認同一 selector 能找到新的使用者訊息，且不會匹配模型回答。 |
| `4.userContent.bat` | 1～3 | 自動送出問題，點選使用者訊息內的正文。保存相對於 user turn 的 selector，再以另一則問題驗證正文完整讀回及 runtime 解析。 |
| `5.assistantTurn.bat` | 1～4 | 自動送出問題，點選模型回答容器。保存前先記錄既有回答基線、送出另一則問題，透過 runtime 的 `latest_owned_assistant` 證明抓到本輪的新回答而非舊答案。 |

每一步只更新自己負責的 config 元件並保留歷史；驗證未通過不發布新 selector。重新更換 user turn 容器時，舊的相對 user content 與 assistant 歸屬驗證會失效，須重做第 4、5 步；更換 user content 時須重做第 5 步。只有 1～5 完成時仍屬部分校準，尚未涵蓋停止、忙碌、最終正文與完成判定。

輸入框若有你原有的草稿會停止；只有工具的固定範本或格式完整的校準草稿可被下一步替換。選取送出按鈕本身不會觸發送出。人機驗證、登入、限流或未回覆不算測試成功；需要你在頁面處理，逾時會留下失敗證據。

## 保留的測試腳本

`source/agent_core/web_ui/tests/test_components.py`：設定合併、點選、相對定位、送出一次與 runtime 歸屬測試。

`source/agent_core/web_ui/tests/test_trusted_other.py`：強制 Trusted Types 下的輸入框／元件面板、Other URL 來源隔離、輸入讀回與第 2～5 步點選驗證。

`source/agent_core/web_ui/tests/live_components_it.py`：真的啟動編號批次檔，在真實網頁點選並執行第 2～5 步。結果、選取與驗證截圖存於 `localdata/logs/manual_dom_it/`。

```powershell
$env:PYTHONPATH = "$PWD\source"
.\.venv\Scripts\python.exe -B -m agent_core.web_ui.tests.live_components_it --url https://chatgpt.com/ --stages 2 3 4 5
```

維護者可使用 `--url <URL>`、`--cdp-endpoint <endpoint>`、`--no-wait`、`--screenshot <path>`。新入口不需要先執行舊的人工標記流程。

第 3 步預期框選單一則使用者提問的完整文字（包含所有換行與段落），不包含模型回答、其他訊息或輸入框。元素名稱不限定；若 `p.query-text-line` 已包含完整提問即可使用。第 4 步允許選取同一元素，會以相對 selector `:scope` 表示直接讀取該訊息本身。提示窗已列出框選範圍、本輪標記與儲存操作。


## 統一瀏覽器設定

執行根目錄的 `Edit_workspace.bat` → `[5] 瀏覽器設定`，先選 Chrome 或 Edge，再選瀏覽器 1～5，預設 Chrome／瀏覽器 1。

設定儲存在 `config/browser_session.json` 的 `browser`、`instance` 欄位。Agent、WebCopilot、校準第 1～10 步及 Other URL 都使用同一個設定。每個品牌／編號有獨立登入資料夾（`chrome_1`～`chrome_5`、`edge_1`～`edge_5`）與獨立瀏覽器程序，不是用分頁區分編號。選定同一個編號後，Agent 與校準可在該瀏覽器建立各自的工作分頁並共用登入。

設定在下次啟動時生效；已執行的 Agent／校準需結束後重新啟動。切換不會關閉舊瀏覽器或搬移舊登入資料；新的環境第一次需登入網站一次，再次選回相同品牌及編號會保留原登入。原先的 `chatgpt_edge_instance2` 資料夾保持原樣。

程式會自動連線所選環境的 CDP；不存在時啟動相同品牌及 Profile。不需要個人 Chrome 開啟遠端偵錯。找不到所選瀏覽器會報錯，不會偷偷改用另一個品牌或登入環境。Chrome 1～5 的連線埠為 12801～12805，Edge 為 12811～12815，不會誤連舊 1273／1275。

維護者明確指定 `--cdp-endpoint` 或遠端 worker endpoint 時仍可覆寫連線。`existing_chrome` 個人瀏覽器連線模式保留為維護用途；在選單儲存瀏覽器設定時會恢復一般管理模式 `legacy`。

測試：`test_browser_preferences.py` 驗證選單、十種組合與四個 provider 共用連線；`live_browser_preferences_it.py` 使用已安裝的 Chrome／Edge 和隔離的測試資料夾，驗證 Chrome 1、Chrome 2、Edge 1 的 Agent／校準輸入、登入 Cookie 隔離及重開後持久化。測試不送出訊息，結果存於 `localdata/logs/manual_dom_it/browser_settings_*/`。

## 選取時的初步檢查與三種狀態

校準第 1～5 步的右上角提示窗會顯示：

- **選取不符**（紅色）：列出失敗原因，不能儲存；可重新選取或調整範圍。
- **初步檢查通過，待功能驗證**（黃色）：目前元件、內容與定位符合要求，才能繼續儲存及功能測試；不代表已校準成功。
- **功能驗證通過**（綠色）：輸入框已完成寫入讀回，或元件已完成原有送出／連續兩輪驗證。

第 2～5 步使用各 provider 的實際解析規則檢查本輪內容、單則訊息範圍、選取元素與 selector 是否對應，以及回答識別資訊。歷史訊息可多筆命中，但本輪必須可唯一辨認。第 3 步若只能依訊息順序辨認，提示窗會明確標示仍待兩輪驗證。初步檢查不輸入、不送出、不寫 config；按儲存時會再次檢查，避免元件更新或舊的選取結果被誤用。

測試：`test_selection_preflight.py`；真實網站可使用 `live_components_it --stages 3 --check-invalid` 或 `--stages 5 --check-invalid` 驗證錯誤選取、初步通過與最終通過，截圖與結果存於 `localdata/logs/manual_dom_it/`。
