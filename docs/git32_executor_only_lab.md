# Git32 Executor-only Lab: 六步 Evidence / Blackboard 驗證

基線：Git31 `3227409a11a100c5745416d9a1dc15978e213127`。

## 模式

Telegram Worker 對新任務預設 `SMARTAGENT_EXECUTOR_ONLY_MODE=1`。設定 `SMARTAGENT_EXECUTOR_ONLY_MODE=0` 可以還原原本 Goal Driver / Prerequisite 規劃。直接使用 Minimal Core 的非 Telegram 呼叫維持原本預設，亦可傳入 `loop.executor_only_mode=True`。

只暫停 Goal Driver / Prerequisite **決策**，不是取消使用者需求、寫入授權或 Goal Terminal Verifier。原始 Scope Admission、`SecurityContext.require_write()`、父目錄先決條件、工具執行與獨立 Runtime Terminal Proof 全部保留。

## 本次明確測試類型

針對單一路徑、六步編號的 Blackboard 整合驗證任務，Runtime 使用一個有限、嚴格的 `EXECUTOR_ONLY_DIRECTORY_SEQUENCE` 驗收契約，不為不相關的複合要求自動宣告成功。

1. `INITIAL_CHECK`：實際 `observe_fact` 或 `inspect_directory`。已存在且實際證明是目錄可直接驗收；`file.exists=true` 仍需目錄型別證據。
2. `CREATE_REQUIRED`：只有確認缺少時，才允許提案 `create_directory`；仍需通過原始 ACL/Scope Admission。
3. `POSTCREATE_TYPE_VERIFY`：建立後必須再經不同的唯讀 Action 證明目錄存在且物件型別正確，不能拿剛剛建立的 ToolResult 假裝獨立驗證。
4. `COMPLETE_CREATED / COMPLETE_REUSED`：Runtime 重放所有 Action / ToolResult / SHA256 與當前 Task ID、Request ID、Epoch；若不符，禁止 Terminal。
5. `VERIFIED`：儲存 Executor Blackboard、再次 readback 完整驗證、才回覆 Telegram `COMPLETED / SUCCESS`。

`UNKNOWN / RUNNING` 原始 ToolResult 仍必須走 Git28 的有界唯讀 Resolution，不得再次執行可能有副作用的 Mutation。未證實成功不能宣告 `VERIFIED`。

## 各層狀態隔離

| 層級 | 欄位 | 正向預期 |
|---|---|---|
| Tool | `result_status` | `SUCCEEDED` |
| Executor | `terminal_status` | `VERIFIED` |
| Runtime Terminal | `verification_status` / `task_outcome` | `PASS / SUCCESS` |
| Blackboard | `verification_action_refs` | 真正可重放的 Action ID |
| Blackboard | `tool_evidence_refs` | 真正執行的 `MIN-EV-` ID |
| Prerequisite | `prerequisite_handoff_state` | `DISABLED_EXECUTOR_ONLY` |
| Goal Stage | `goal_stage_handoff_state` | `DISABLED_EXECUTOR_ONLY` |
| Parent Consumption | `parent_consumption.status` | `NOT_APPLICABLE_EXECUTOR_ONLY` |
| Parent Dispatch | `parent_dispatch_state` | `NOT_DISPATCHED` |

`Handoff ID` 是已寫入的路由/狀態記錄身分，**不代表其他 Agent 已消費**。

## 驗證界線

本版目標是把指定「check → conditional create → independent verify → report」的 Executor 路徑跑實，不代表已替所有腳本、編譯或程式功能分析建立充分的 Task-specific Verifier。後續可基於同一 Evidence/Terminal 介面為 `run_command` 建立完整 `process_state`、`exit_code`、`timed_out`、`verification_status`、stderr/stdout 完整性與 success_criteria 契約；在完成前，不得只依模型文字或 `exit_code=0` 宣告執行結果有效。

完整測項可沿用使用者原始六步 Telegram 指令建立新 Task，必須先確保 E 槽寫入授權已由原有 Runtime Scope 設定批准。
