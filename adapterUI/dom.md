# DOM：完全手動取得與填寫指南

適用目錄：`C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`。
本文件按「需要哪個 DOM」排列。DOM 設定填的是 CSS selector，不是按鈕圖示的 Unicode、圖片網址，也不一定是 HTML id。優先用穩定的 `data-testid`、`aria-label`、role 與固定 id；不要填每次重新整理都改變的亂數 id。

## 先找到要修改的檔案

- ChatGPT：`source\agent_core\web_ui\providers\chatgpt\dom_config.json`
- Gemini：`source\agent_core\web_ui\providers\gemini\dom_config.json`
- Claude：`source\agent_core\web_ui\providers\claude\dom_config.json`
- M365 與 `copilot.cloud.microsoft`：`source\agent_core\web_ui\providers\m365\dom_config.json`（同一份）
- Other：`source\agent_core\web_ui\providers\other\other_<來源 SHA256>\dom_config.json`。每個協定／主機／連接埠一份，不能把所有網站都放在 `providers\other\dom_config.json`。

檢查檔案的 `source_origin` 是否對應目標網站；同網站不同對話通常共用 DOM。Other 的路徑請從根目錄 PowerShell 執行以下命令取得，不需自己計算 hash：

```powershell
$env:PYTHONPATH = "$PWD\source"
.\.venv\Scripts\python.exe -B -c "from agent_core.web_ui.provider_registry import provider_from_url; from agent_core.web_ui.dom_config import config_path; u=input('URL: '); p=provider_from_url(u); print(p); print(config_path(p))"
```

修改前備份原檔；結束正在使用它的 Agent／校準程序，改完後重新啟動。以 UTF-8 儲存，JSON 使用英文直引號，反斜線必須寫成 `\\`。不要修改 Python 程式裡的內建 selector 來代替個別網站設定。

## 如何從開啟的 WebGPT 取得 selector

1. 開啟目標頁面，必要時登入。按 F12 → Elements／元素，按 Ctrl+Shift+C，再點選需要的輸入框、圖示按鈕或訊息。
2. 圖示通常是 `svg` 或 `path`；在 Elements 往上找到實際可點擊的 `button` 或 `a`。訊息也要分清楚「整則訊息外框」與「正文」。
3. 例如看到 `<button data-testid="send-button" aria-label="傳送">`，可選 `button[data-testid="send-button"]`。若只有固定 id `composer`，可用 `#composer`。這只是例子，必須用你網站實際出現的屬性。
4. 右鍵 → Copy → Copy selector 可當起點，但長串 `nth-child` 很脆弱，盡量改成穩定屬性。Console 執行 `document.querySelectorAll('button[data-testid="send-button"]').length` 檢查數量，再用 `inspect(document.querySelector('button[data-testid="send-button"]'))` 看是否為目標。
5. 輸入框／送出／附件入口應只有一個可見目標；訊息容器可以有多筆，每筆必須對應單一 user 或 assistant。不要在 selector 中放本輪測試文字、時間、DOMPROBE 或整段回答。
6. 至少重新整理、送兩輪不同問題後再確認。只靠外觀相同，不代表讀回與訊息歸屬正確。若在 iframe，先於 DevTools 選對 frame；目前一般 CSS selector 不會自動跨 iframe 或封閉 Shadow DOM。

## DOM 1：對話輸入框 composer

填 `elements.composer.selector`。選可編輯的 `textarea`、`input` 或 `contenteditable` 元件；不要選外層整條工具列。例：`textarea#prompt` 或 `[contenteditable="true"][role="textbox"]`。要能點擊、輸入、讀回相同文字，且不會選到搜尋框。

## DOM 2：送出按鈕 send

填 `elements.send.selector`。選實際 button，不能選裡面的 SVG。空輸入框時 disabled／隱藏很常見，應填入草稿後再觀察。不能把「空白所以無法送出」當成模型仍在生成。不要按第二次來處理送出結果不明。

## DOM 3：使用者訊息外框 user_turn

填 `elements.user_turn.selector`。每一筆命中應完整包含單一使用者訊息，可包含這則的時間／頭像；不得包含模型回答、別則提問或整個對話。例：`div[data-testid="user-message"]`。如果只能選到正文仍需驗證可否作為單則訊息錨點，不能僅因為框大就判通過。

## DOM 4：使用者訊息正文 user_content

填 `elements.user_content.selector`，`scope` 為 `user_turn`。selector **相對於第 3 步外框**，例如 `span[data-testid="user-message-content"]`，只讀正文，排除時間、頭像、操作鈕。若第 3 步選取本身就是完整正文，可用 `:scope`。不要寫依賴另一則訊息的全頁路徑。

## DOM 5：模型回答外框 assistant_turn

填 `elements.assistant_turn.selector`。每筆是一則 assistant 回答的完整容器，可包含該則操作鈕；不能含 user 或其他回答。不要硬寫目前那則的唯一 message id；selector 要能同時找到歷史和新回答，程式再判斷歸屬。

## DOM 6：模型回答正文 assistant_content

填 `elements.assistant_content.selector`，`scope` 為 `assistant_turn`。在第 5 步容器內取得所有段落、換行、表格與程式碼，不包含讚／複製等控制。不能只選最後一段或單一 `p`。例：`.markdown`；若外框等於正文用 `:scope`。

## DOM 7：停止生成按鈕 stop

填 `elements.stop.selector`（JSON 是字串）。要在模型生成期間檢查，選實際停止按鈕。例：`button[aria-label="停止回應"]`。送出鈕消失只是線索，不能直接當作完成；停止後圖示殘留也不等於內容仍在生成。

## DOM 8：回覆狀態 generation_state（沒有新的框選 DOM）

沿用正文與 stop。`capabilities.generation_state` 的 `strategy` 是 `stop_busy_and_stable_text`，`quiet_seconds` 介於 2～30。必須觀察本輪生成、停止控制與正文持續穩定；僅把 JSON 寫成完成，不能證明這步通過。

## DOM 9：阻擋提示 blocking_state

在 `capabilities.blocking_state.rules` 填 `kind`、`selectors`、`tokens`。只選真正的登入、限流、網路、對話已滿或網站錯誤提示；不要選回答裡引用的錯誤文字。`kind` 可為 `rate_limit`、`conversation_full`、`login_required`、`disconnected`、`provider_error`。不要為了通過而清空所有阻擋檢查。

## DOM 10：完整對話迴圈 conversation_loop（沒有新的 DOM）

沿用 1～9，測兩輪送出、讀取本輪回答與完成狀態。`capabilities.conversation_loop` 是驗證紀錄，不能只填旗標取代測試。

## DOM 11：附件入口 entry

填 `files.entry`。選輸入框旁「＋／附件」的 button，點一下後可能顯示選單或直接開選檔視窗。通過紀錄是 `files.entry_verified`。

## DOM 12：選檔與上傳 input／upload_action

填 `files.input`，例如 `input[type="file"]`，這個 DOM 常是隱藏的，不是原生 Windows 檔案視窗。若需先按選單，另填 `files.upload_action`（從電腦上傳等 menuitem/button）。上傳後要觀察實際選到的小型無敏感檔案，通過紀錄是 `upload_verified`。

## DOM 13：附件卡片、移除、忙碌與錯誤

先真的上傳測試檔，才有可選附件；不要選歷史訊息中的檔案。

- `files.chip`：目前輸入區單一附件卡片，包含檔名及移除鈕。
- `files.remove`：相對附件卡片的移除 button。
- `files.busy`：附件上傳／處理中指示。
- `files.error`：附件失敗提示。
- `files.attachment_probe`：由實際觀察建立，completion 為 `busy_idle`、`explicit_ready` 或 `send_transition`；不要憑空填寫 ready 屬性。

必須確認檔名、上傳完成／失敗、可送出及清理不損壞原草稿。`attachment_inventory_verified` 與 `attachment_verified` 是成功後的紀錄。

## DOM 14：本輪產生檔案的下載入口 download

填 `files.download`，**相對於本輪 assistant_turn** 的連結／按鈕。不要填上傳入口、歷史回答下載鈕或固定 blob URL。必須實際下載並核對檔案內容，才可記錄 `download_verified`。網站不支援產生檔案時，這步不能宣稱通過。

## DOM 15：上傳下載完整迴圈（沒有新的 DOM）

沿用 11～14：只上傳專用測試資料夾自產小檔，模型讀出檔內獨有標記、產生新檔、下載、逐字比對。成功才記錄 `files.roundtrip`；不能只因畫面出現附件或連結就算成功。

## 手改 JSON 的邊界與檢查

請優先修改已存在的 `dom_config.json` 對應 selector，保留 schema、provider、source_origin、revision 與其他欄位。第一次完全手建時需符合 `SMARTAGENT_MANUAL_DOM_V1` schema，至少有 composer；只新增任意 selector 文字檔，runtime 不會載入。

每個 `elements` 項目需有 `selector`、`scope`、`validation` 等資料；驗證鍵依序為 composer=`editable_unique`、send=`click_verified`、user_turn=`marker_verified`、user_content=`content_verified`、assistant_turn=`ownership_verified`、assistant_content=`content_verified`、stop=`stop_verified`。目前載入器要求對應旗標為 true；**這是載入條件，不是替你執行驗證**。不要為繞過檢查而全填 true。完全手動時須實際核對相應行為並另記測試紀錄；若做不到，仍屬未完成。

改第 3／4／5 步會影響相對正文及訊息歸屬；改附件入口／卡片／下載 DOM 會影響後續檔案驗證。不要沿用舊的能力通過旗標當作新 selector 已通過，需重做受影響步驟。透過 adapterUI 儲存時程式會自動處理失效；純手改不會自動處理。

根目錄 PowerShell 檢查是否讀得到並符合 schema（只檢查設定，不會證明網頁行為）：

```powershell
$env:PYTHONPATH = "$PWD\source"
.\.venv\Scripts\python.exe -B -c "from agent_core.web_ui.provider_registry import provider_from_url; from agent_core.web_ui.dom_config import load_dom_config,config_path; p=provider_from_url(input('URL: ')); print(config_path(p)); d=load_dom_config(p); print('CONFIG_LOADED' if d else 'CONFIG_MISSING'); print(list((d or {}).get('elements',{})))"
```

設定失敗不要刪除所有檢查；保留錯誤文字及 `localdata\logs\manual_dom_it\` 內 result.json、selection.json、診斷 JSON 供追查。不要上傳 cookie、瀏覽器 profile 或內部網頁截圖到公開 Git。
## 無對話 ID 的網站入口

Other URL 可用首頁、路徑或 hash 路由，程式會產生網址識別；同來源共用校準設定，但不同對話網址仍分開綁定。M365／Copilot 等內建網站若貼的是未含 ID 的入口（例如 https://copilot.cloud.microsoft/chat/conversation），WebCopilot 會先開自己的分頁，等待在該分頁選取既有對話、取得正式對話 URL，才建立 session。若逾時，請貼該對話的完整 URL 後重啟；不會先對不明對話送出初始化。

## 第一次手建設定：最小範例

如果原檔不存在，可以從下面的「只有輸入框」範例開始。先用上面的命令取得實際 provider 名稱與路徑；把 provider、source_origin、selector 換成自己的值。`source_origin` 只包含協定、主機與必要連接埠，不含對話路徑、查詢參數或登入憑證。

只有親自確認 selector 唯一、可見而且可編輯後，才可把 `editable_unique` 記為 true。下面不是一份已完成的對話適配；WebCopilot 正常收發還需要 send、user_turn、user_content、assistant_turn 與 assistant_content 等項目及行為驗證。

```json
{
  "schema": "SMARTAGENT_MANUAL_DOM_V1",
  "provider": "other_請替換成命令輸出的完整雜湊",
  "revision": "manual-20261021-1",
  "created_at": "2026-10-21T00:00:00+00:00",
  "source_origin": "https://internal.example",
  "scope": "composer_only",
  "elements": {
    "composer": {
      "selector": "textarea#composer",
      "tag": "textarea",
      "kind": "input",
      "scope": "page",
      "validation": { "editable_unique": true, "full_conversation": false }
    }
  },
  "capabilities": {},
  "files": {}
}
```

第 2～7 步的 JSON 都放在 `elements` 內，結構同 composer，名稱與驗證鍵參照各 DOM 小節；`user_content.scope` 為 `user_turn`、`assistant_content.scope` 為 `assistant_turn`，其餘為 `page`。每次修改 revision 可換成新的唯一字串。完整生命週期與檔案設定有額外相依欄位，請以現有校準輸出的結構及本文件欄位說明為準，不要直接複製另一個網站的通過紀錄。