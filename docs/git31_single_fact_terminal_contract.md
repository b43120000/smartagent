# Git31 — Single Fact Query Terminal (Runtime-owned)

Base: Git30 `25a0c9b27e4094d2d5b0a7eac7074fab0df02429`.

## Live RCA 修復目標

使用者：`這個資料夾存在嗎?\nE:\SmartAgent_TriLayer_Blackboard_Test_20261109`

若 Runtime `observe_fact` 傳回可信的 `file.exists=false`、`observation_status=CONFIRMED`、`source=filesystem.stat`、`freshness=CURRENT`，原本工具執行成功、但整體 Goal `PAUSED / UNKNOWN`。Git31 為單目標 existence question 增加嚴格的 `SINGLE_FACT_QUERY`，不擴大任何目錄 ACL／Scope／Mutation 授權。

## 三層狀態獨立

| 狀態層 | 欄位 | 負向查詢完成示例 |
|---|---|---|
| ToolResult | `status`、`reason` | `SUCCEEDED / FACT_OBSERVED` |
| Fact Observation | `fact_observed_value`、`fact_observation_status` | `false / CONFIRMED` |
| Executor | `terminal_status` | `VERIFIED` |
| Goal Terminal Acceptance | `verification_status`、`runtime_state` | `PASS / COMPLETED` |
| Prerequisite | `prerequisite_readiness_verdict` | `NOT_EVALUATED`（不自行推定） |
| Goal Stage | `goal_stage_verification_verdict` | `NOT_EVALUATED`（不自行推定） |

`false` 不是 `FAILED`；`true` 也不是 `VERIFIED`。成功取決於使用者請求的 Predicate 是否經嚴格證據證實。

## Runtime 約束

1. 整份 Goal 必須只是單一、完整匹配的存在性問句，唯一 Windows 絕對路徑要與 Action 完全綁定。混合寫入、另一個目標、部署、查詢內容不適用。
2. `_confirmed_exists_fact()` 確認 Action ID、ToolResult/Action identity、原始目標、`filesystem.stat`、`CURRENT`、`CONFIRMED` 及原始 `bool`；拒絕推測。
3. `file.exists=false` 可直接證明指定路徑不存在，適用單一資料夾／檔案／路徑查詢。
4. `file.exists=true` 只證明路徑存在。資料夾需要另外可信的 `list_directory`／`inspect_directory` 類型證據，檔案也不能僅憑布林 true 宣稱是一般檔案。
5. `accept_runtime_terminal` 對 `SINGLE_FACT_QUERY` 重放當前 ActionResult Ledger；驗證 request/task/epoch/action、原始回覆 digest、Proof 理由與證據身分。
6. Executor 接受後即自動完成，不消耗額外模型 Finish，亦不允許用模型矛盾文字覆蓋 Runtime 的事實摘要。
7. Blackboard `fact_observation` 經 Acceptance 綁定後持久化，並在 `executor_handoff.executor_fact_observation` 交付；不將其他兩層獨立狀態標記完成。

## Live 測試

執行單次：

```text
這個資料夾存在嗎?
E:\SmartAgent_TriLayer_Blackboard_Test_20261109
```

若觀測到 `false / CONFIRMED`，預期一個 `observe_fact` 後直接 `VERIFIED / COMPLETED / PASS`，且 `fact_observed_value=false`；未觀測或狀態 `UNKNOWN` 時不得宣稱成功。

CI 含 Windows 與 Ubuntu 的負向觀測成功、正向類型未定、目前/過期 Evidence、Action ID 異常、複合 Goal、無工具模型宣稱成功、Git28～Git30 回歸與 Manifest。
