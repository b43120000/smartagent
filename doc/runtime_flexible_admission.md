# Runtime 彈性回覆與讀取准入

## 本版實作

- 已有有效 Progress 時，自動合併省略的計畫欄位，不需要 PATCH 標記。決策、證據、終態與阻塞理由不能沿用。
- 確定性的數字字串、欄位別名及 step status 正規化；衝突或非法新值不可默默忽略。
- Batch field diagnostics 回覆缺漏欄位、預期型別及 Runtime 已知值。修復狀態保存在 task progress 的 runtime-owned `repair_state`，本地完整紀錄與有限 prompt 摘要分開。
- 有有效計畫且明確 CONTINUE 時，非關鍵 Progress 問題可暫緩；只有 list_directory、inspect_directory、find_file、query_project 的全讀取批次可重新走完整准入。
- 不允許混合寫入／刪除、final_response、矛盾終態、偽造 evidence 或不符合 scope/prerequisite 的 action。失敗的 Progress 不會落地；下輪提供工具結果與精準修復要求。
- 相同欄位仍未修好、讀取證據也不變時暫停。新 action/query ID 或讀取耗時變化不等於新來源 evidence。
- delete_path、list_directory、find_file、write_file 已遷移至結構化結果；其他工具保留既有結果契約，並非所有工具都已遷移。
- 真實結果先分類再做模型輸出縮減，避免截斷造成狀態遺失；stage 與一般 action loop 使用相同的結構化結果狀態。
- 新 request 清除暫存驗證、補救與定位狀態，不得借用上一個 request 的 PASS。工具成功不等於任務成功；既有終態驗收仍保留。

## 主要模組

- `source/agent_core/progress_reply_contract.py`：增量合併與正規化。
- `source/agent_core/progress_recovery.py`：精準診斷、讀取 fallback、進展與來源 fingerprint。
- `source/WebAgent/protocol_loop.py`：完整准入、真實執行、修復及終態 lifecycle。
- `source/agent_core/task_progress.py`：有效計畫及本地修復紀錄。
- `source/agent_core/tool_result_contract.py`：結果 envelope 與舊格式轉換。

## 驗證與限制

新增 `WebAgent.tests.validate_flexible_progress_admission` 驗證三輪、兩個真實讀取、Progress 修復後結案、混合寫入拒絕、偽造證據拒絕、跨 request 隔離及重複讀取防空轉。

網頁／Telegram IT 尚未執行。廣域回歸的 `validate_protocol_same_round_repair`（缺少舊 class）和 `validate_task_plan_schema_recovery`（notes.txt semantic-map fixture）在 HEAD 基準也失敗，不能列為 PASS；修復這兩個既有測試另列工作。
