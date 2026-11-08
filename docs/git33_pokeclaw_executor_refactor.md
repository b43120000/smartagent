# Git33 — SmartAgent Executor 重構為 PokeClaw 式通用 Agent Loop

基線：Git32 `cb677907755a9e8aab6e640d70cd26c72083bad1`。

## 啟用與回復

Telegram Worker 在 Git32 起預設 `SMARTAGENT_EXECUTOR_ONLY_MODE=1`，Git33 進入新的 `WebAgent/pokeclaw_executor_loop.py`。獨立的 loop 可設定 `loop.executor_only_mode=True`。設為 `SMARTAGENT_EXECUTOR_ONLY_MODE=0` 才回到過往的 Legacy v10 行為。

**已停用：** Goal Driver 決策、Prerequisite Requirement／PR-A Graph 評估與 Planner 路由、Git32 六步專用 `directory_sequence_contract`、`sequence_progress` 與 `next_action_gate`。已保留：原始要求路徑解析、Runtime Tool Envelope、Scope/ACL/Write Ceiling Admission、真實工具執行與每輪 Blackboard 記錄。

## PokeClaw 式循環

1. Planner 收到原始要求、可用 Tool 清單、最近幾輪**實際** ToolResult。
2. Planner 回傳一個 `tool_call`、`finish` 或 `blocked`。
3. ToolRegistry/Admission 先核對工具與參數、寫入範圍、安全政策；拒絕也視為**工具嘗試結果**提供回 Planner，不得當成執行成功或負向檔案證明。
4. 真正執行工具，產生原始 Runtime ToolResult，投影成原始 PokeClaw 的 3 欄：
   - `isSuccess: boolean`
   - `data: string | null`
   - `error: string | null`
5. 同回合將 Action、Result、Runtime Evidence 寫入 `localdata/runtime/executor_run_blackboard/TASK-....json`，下一輪模型能收到最新結果，再決定後續行為。
6. `finish` 產生 `MODEL_REPORTED_COMPLETE_UNVERIFIED`，**不能謊稱 Runtime 已對整個 Goal 做過獨立驗證**。

這個 Agent Loop 不認識特定六步要求，既可用於 `observe_fact`、`create_directory`、`list_directory`，也可用於已註冊的 `run_command`、`read_file` 等工具。

## 三欄 ToolResult 的語意

| Runtime 事實 | Planner 接收到的 ToolResult | Blackboard 額外原始狀態 |
|---|---|---|
| `SUCCEEDED` + `file.exists=false` | `isSuccess=true`、`data` 含 false | `status=SUCCEEDED`、`MIN-EV` |
| `SUCCEEDED` + `file.exists=true` | `isSuccess=true`、`data` 含 true | `status=SUCCEEDED`、`MIN-EV` |
| `REJECTED`（未執行） | `isSuccess=false`、`error=ACTION_NOT_EXECUTED:...` | `executed=false`、Scope Cause |
| `FAILED`（如命令非零碼） | `isSuccess=false`、`error` 附 `exit_code` | `exit_code`、`timed_out`、`verification_status` |
| `UNKNOWN/RUNNING/PARTIAL` | `isSuccess=false`、`error=EXECUTION_OUTCOME_UNCERTAIN:...` | 原始 Status 原封保存；有副作用時**停止重送** |

PokeClaw `ToolResult.isSuccess` 代表工具是否成功，**不代表觀測的布林值**。

## Shared Blackboard 契約

`source/agent_core/executor_run_blackboard.py` 將每次工具提案與實際 ToolResult 持久化，包含：

- `task_id`、`request_id`、`task_epoch`、`request_sha256`
- 每輪 `ACTION_ADMITTED / TOOL_RESULT / MODEL_FINISH / MODEL_BLOCKED`
- PokeClaw 3 欄 `toolResult`、`round`、`toolName`、`action_id`
- `runtimeEvidence` 的真實狀態、執行旗標、結果 SHA256、`MIN-EV`、程序退出與驗證資訊
- `handoff_id`、`parent_dispatch_state=NOT_DISPATCHED`、`parent_consumption_state=NOT_APPLICABLE_EXECUTOR_ONLY`
- `goal_driver_state=DISABLED`、`prerequisite_state=DISABLED`

檔案以原子方式更新，每輪寫入並於 Terminal readback 核對；不允許不同 Request/Epoch 覆蓋已有紀錄。敏感 command／內容參數只保存雜湊或遮罩，不在 Blackboard 直接保存全文。

## Git32 路徑錯誤

`source/WebAgent/protocol_loop.py` 的 `extract_authorized_paths` 原本可能把 `E:\... 執行三層 Blackboard 整合驗證：` 解析成包含中文動詞的單一路徑，後續 `create_directory` 經過 `require_write` 仍被 `DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST` 擋下。

Git33 增加對後接「執行…驗證／測試／任務：」的自然語句邊界判定，讓 `explicit_request_paths` 保持單一實際目標。**沒有放寬 `SecurityContext.require_write` 或原始 Scope 授權**。GitHub Windows CI 有用真實暫存工作路徑建立與重新列出目錄的回歸，E 槽 Live 仍須確認。

## 完成狀態的限制

原始 PokeClaw 的 `finish` 只表達「模型回報完成」。Git33 保持這項語意，將真正的 ToolResult 保存於 Blackboard，並以：

- `executor_state=MODEL_REPORTED_COMPLETE_UNVERIFIED`
- `runtime_state=COMPLETED`
- `task_outcome=UNKNOWN`
- `verification_status=MODEL_REPORTED_COMPLETE_UNVERIFIED`

交付 Telegram。這不是六步任務已獲得獨立 Goal PASS 的宣稱；但能顯示已觀測的各工具結果與 Handoff ID，並清楚區分工具事實與模型總結。

後續若需整體 Goal PASS，應以獨立可驗證契約額外交付，而不是重新把「六步需求文字」寫回 Executor 的執行順序或條件。
