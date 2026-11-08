# Git30 — Executor 狀態與因果證據邊界稽核

Base: Git29 `e5ad2c29e383ed554dd5e976c37a23e4ce5d71c8`.

## 契約

- Executor 終態只能是 `VERIFIED / BLOCKED / NEEDS_PREREQUISITE`。
- `UNKNOWN / RUNNING` 可以出現在原始 ToolResult；必須進入有界 Resolution，不得當作 Executor 終態。
- `BLOCKED` 指明確的執行／驗證阻礙；**不代表**先前可能發生的副作用沒有發生。
- `SCOPE_ALLOWED` 只代表此階段的 Scope Preflight 允許，不能取代 Tool Execution、Goal Verification 或其他政策。
- Runtime 對於已執行的 Action Evidence 使用 `MIN-EV-...`；對於執行前遭拒的 Action Admission 使用 `MIN-ADM-...`。兩者不可互換，拒絕不得偽造 ToolResult。

## 已找到並修正的 Executor 核心失真

| 來源 | 舊問題 | Git30 修正 |
|---|---|---|
| `action_readiness._error_code` | 大量錯誤降成 `OTHER_SCOPE_REJECTION` | 分類 `DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST`、`DIRECTORY_PARENT_NOT_EXISTING_DIRECTORY`、`PATH_OUTSIDE_AUTHORIZED_ROOTS` 等；分類不了時明示 `UNMAPPED_SCOPE_PREFLIGHT_CAUSE` 與 digest，絕不假稱原因已知 |
| `tools.preview_tool_scope(create_directory)` | `Path.is_dir()` 壓平不存在、非目錄、權限拒絕 | `stat()` 分辨原因，獨立記錄 `write_authorization_verdict`、`target_binding_verdict`、`parent_directory_verdict`；不執行 Mutation |
| `minimal_admission` | 只返還縮寫的 Scope 拒絕 | `_last_minimal_admission_diagnostic` 保存同一次 Preflight 的分層代碼、原因雜湊及前提；每次 Admission 清空防止跨 Action 污染 |
| `minimal_core_loop` | Admission 拒絕在最終 `BLOCKED` 被新模型文字覆蓋 | 保留每次 `MIN-ADM`、Action/Scope failure stage、模型原始提案 hash；ToolResult 則在 `MIN-EV` |
| `minimal_executor_contract.blocked_disposition` | 先前成功讀取可能遮蔽隨後的實際 Action Admission 拒絕 | 按 Round/時間因果優先最新拒絕，必要前提缺失路由 `NEEDS_PREREQUISITE`；不據未知結果推定成功 |
| `executor_blackboard` | 複合 `reason_code` 大小寫失真，`UNCLASSIFIED_BLOCKER` 遮蔽根因 | 保留正規化複合原因與 `root_cause_code`；另外保留 `reason_sha256`、`causal_basis`、`evidence_refs` 與 `admission_rejection_refs` |
| `minimal_resolution` | `RESOLUTION_ACTION_DENIED` 不知道是哪個 Admission 原因 | 在 Resolution 子任務留下確切拒絕代碼、拒絕證據及 stage；不重試可能已有副作用的操作 |
| `tri_layer_handoff` | 缺真正父任務時自動拿自己當 parent | 標記 `ROOT_TASK_PARENT_NOT_BOUND`、`NOT_DISPATCHED`；只給建議 owner，不謊稱已完成 Parent 交付；已綁定時仍保留獨立的 Executor／Prerequisite／Goal Stage 狀態 |

## 工具層尚需逐 Producer 驗證的通用代碼

下列是**已知工具邊界分類**，不是 Task Terminal，不能拿來當已完成事實：

| 代碼 | 所屬範圍 | 合法意義／下一步 |
|---|---|---|
| `INVALID_RESULT_ENVELOPE`, `UNCLASSIFIED_PRODUCER_RESULT` | 原始 ToolResult | Runtime 未取得符合契約的 ToolReceipt，必須 Resolution／安全阻擋；不能憑文字宣告執行完成 |
| `EXECUTION_STATE_UNKNOWN` | 例外出口 | 無法確認副作用；不得再次執行 Mutation，應重新觀測 |
| `PREFLIGHT_REJECTED`, `TOOL_FAILED` | 部分舊 Producer Adapter | 尚非充分的根因代碼；需讀原始受限錯誤 Evidence、分 Producer 增加 Cause Taxonomy |
| `PLANNER_UNCERTAINTY_NOT_VERIFIED` | Executor 決策控制 | Planner 沒有取得或提出可採納的證據；至多一次受限重決策後，可產生明確的 `BLOCKED` 控制性終態 |
| `RESOLUTION_EVIDENCE_EXHAUSTED` | Resolution 迴圈 | 觀測預算已耗盡或安全觀測不可行；標成 `BLOCKED`，保留 `last_observed_tool_status` 及原始副作用可能性 |
| `UNKNOWN`（Tri-One Task Acceptance） | 整體 Goal 的非終態驗收 | 不代表 Executor 終態是 UNKNOWN。三層欄位不可共用 |

若未來遇到未映射 Scope 原因，必須保留 `UNMAPPED_SCOPE_PREFLIGHT_CAUSE` 及 `cause_digest`，回報 Runtime contract gap，而不是擴大權限或推測檔案系統狀態。

## Git30 Live 必查

1. `observe_fact` 實際觀測存在性，保留 `MIN-EV-...`。
2. `create_directory` Admission 回傳精確的 `scope_reason_code` 和 `failure_stage`。
3. 若被拒：`executed=false`，建立前後無副作用，`MIN-ADM-...` 另存。
4. 若父目錄存在、合法目標寫入 ACL 已授權：正常放行建立，依實際 ToolResult 驗證。
5. `executor_blackboard_terminal` 的 `reason_code / root_cause_code / evidence_refs / admission_rejection_refs` 均可追溯。
6. 沒有真實父子任務綁定時，`parent_binding_status` 必須為 `ROOT_TASK_PARENT_NOT_BOUND`；禁止宣稱 Prerequisite Worker 已接續。
