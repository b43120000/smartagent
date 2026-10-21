# 共用任務接續流程

Telegram 與 WebCopilot 使用 `agent_core/task_continuation.py`，不是讓模型從舊對話猜測任務。每次接續建立新的 task/request/epoch，保留原 task 的來源資訊。

## 使用方式

中斷、執行失敗、驗收失敗、部分完成或結果未確認後接著原目標執行：

```text
接續工作 TASK-原任務ID
```

也可直接貼上紅／橙完整通知，或單獨貼 task_id／request_id。調整任務時，在通知最後追加：

```text
調整需求：只讀確認目前產物，不要重做已執行的修改。
```

調整後產生新的 task/request/epoch，保留來源與 action 歷史，重新建立 Progress／完成契約。
調整需求中的其他 ID、`要求：` 等文字屬於需求內容，不改變接續來源。
不改需求時延續原目標；非成功的 COMPLETED 任務也可接續，但重新規劃而非沿用舊終態。

## 五種終態顯示

| 條件 | 顯示 |
|---|---|
| COMPLETED＋SUCCESS | 🟢 已完成 |
| COMPLETED＋FAILED | 🔴 已結束（驗收失敗） |
| COMPLETED＋PARTIAL／UNKNOWN／缺少或無效 outcome | 🟠 部分完成／結果未確認 |
| TASK_FAILED | 🔴 執行失敗 |
| TASK_INTERRUPTED／取消／暫停 | 🟠 任務中斷 |

非綠終態均附上 task/request 與接續說明；收到通知後不會自動重送。
等待／執行中的黃、藍提示不是終態，仍保留，且活躍工作不可再啟動一份。
只有明確 SUCCESS 才亮綠燈；單純檔案取得／排隊的 fast path 不假裝已證明交付。

也可指定 request_id、event_id 或 Runtime 發出的 WEBACK ID，或貼上「接續上一個工作或指定 ACK ID 工作」及完整錯誤訊息。只有唯一可信任務才會接受；多個候選時需指定 ID。只重新貼原始需求仍視為新任務，不自動猜測接續。

已結束的任務補驗證（同樣可對中斷的任務補驗證）：

```text
後續驗證 RR-TELEGRAM-原requestID
需求：針對原 MUTATE action negative-probe-mutate-001，先以新的 VERIFY 檢查 WRONG_VALUE，預期 FAIL；再以另一個新的 VERIFY 檢查 ACTUAL_VALUE，預期 PASS。兩次 verifies_action_id 都關聯原 action，不得寫入。回報兩筆驗證歷史與最新有效驗證狀態。
```

## 儲存與安全

- 本包 runtime root 的 `task_continuations.json` 使用程序鎖與原子替換；不搬到另一包、不作 sync_back 的 source。
- 工具執行前保存 EXECUTING 意圖，執行後保存實際結果、action cache、Progress、pending repairs 和尚未執行的批次。協議初始化失敗也保存接續來源。
- 同一 owner、instance 和 canonical Workspace 才可查找。Telegram owner 使用 sender_id + chat_id；WebCopilot 是本機 OS owner。共用核心不代表自動開放跨使用者／跨介面授權。
- 舊 action 只能經明確且通過檢查的任務關聯匯入。保留來源 task/request/epoch，進入新 request 時 evidence 標為 STALE；新的唯讀 VERIFY 才能取得目前狀態。
- 已執行的相同 ID 只回傳歷史 cache，不重新產生「新執行證據」。相同 ID 改 payload 拒絕；新 ID 重播同一原操作也拒絕。
- FOLLOWUP_VERIFY 限定讀取工具及可證明為唯讀的命令（包含 verify 內的子命令），不接受寫入命令假冒 VERIFY。
- UNKNOWN / 執行中的原操作不能盲目重播；先核對或等待使用者處理。不能承諾當機後的外部 side effect 可 exactly-once。
- 新驗證保留 FAIL/PASS 歷史並更新原 action 的 effective status；原始 execution_status、verification_status 與舊任務結案不改寫。
- 合法範圍從原使用者需求／可信 structured scope 取得，後續工具仍經目前權限與安全預檢；不重用刪除批准或執行批准。
- Journal 上限 64 MiB，滿時 fail closed，不刪掉歷史後偷偷重播。

## 舊版資料與邊界

舊版 TaskState 若有完整且身分相符的 action result（tool、digest、executed、request/epoch），可做後續驗證。缺少執行前 journal 的舊版任務不可宣稱能安全 RESUME；請使用後續唯讀驗證。無有效 action 紀錄不能從模型文字補造。明確 SUCCESS 的已完成任務不能用 RESUME 重做；非成功結案可在 journal 完整時接續。

若程序意外離開，紀錄會保留。來源綁定與接續保留同一筆原子寫入；僅確定原 PID 已離開才釋放 RUNNING／REGISTERED 保留，仍在執行的任務拒絕第二份。不以逾時猜測沒有執行。

兩端的結果由共用 formatter 提供原始需求、Base、結果／中斷原因、task/request、來源 task、接續指令。WebCopilot 自然語言回填失敗時只記錄 presentation unavailable；本地 journal 已保存，不靠再次執行工具來補回覆。

## 驗證

`python -m WebAgent.tests.validate_task_continuation`：固定 planner、模擬命令 capture 的 Runtime integration，涵蓋 FAIL→PASS、歷史更新、RESUME、不重播、owner/workspace、四種 ID、舊版資料與安全拒絕。另有既有 Action Loop／Progress／修復／渲染回歸。

`python -m WebAgent.tests.validate_terminal_status_resume`：五種終態、缺失 outcome、完整通知原樣／追加需求、CRLF、巢狀 ID／標籤、非成功結案重設契約、保留舊 action、不重播、owner 與成功任務拒絕、上一個可接續任務選擇。

2026-10-14 網頁 IT：以 LOCAL_TEST 模擬 Telegram 接收，貼上原中斷任務通知並追加只讀 list_directory 需求。
來源 TASK-CFAAEC2A7D1C9A18613E；新 request RR-LOCAL_TEST-3C7C67E6F67223FB／task TASK-175E1F08363D1EECFB03。
結果 COMPLETED／SUCCESS，2 輪、1 個實際 list_directory、0 拒絕，約 151 秒；未重跑舊 Get-Location，無寫入、刪除或附件。
新的 parser 巢狀欄位防護在該 IT worker 載入之後修正，已由針對性 UT 及獨立重現驗證；不是額外一次網頁 IT。
Telegram 真實網路傳送與 WebCopilot DOM 呈現未在這次 IT 驗證，不稱全面端到端覆蓋。
