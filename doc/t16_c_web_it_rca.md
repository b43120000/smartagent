# T16 query_project — C 版 Web IT RCA 與驗證紀錄

範圍：僅 `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`；正式 Web IT 經真實 Edge ChatGPT Web 與 C 版 Runtime 執行，`LOCAL_TEST` 僅替換訊息傳輸。未修改其他版本，未提交 Git。

## RCA-1：Python 模組來源混入其他版本

首次正式 Web IT 測試未通過。測試程序的 Python 搜尋路徑可能優先解析到 E 槽其他版本，而非本次 C 版。修復：在 `source/run_t16_c_it_w.py` 明確將本版 `source` 放在 `sys.path` 最前，並驗證 `agent_core`、`WebAgent`、`RemoteAgent` 的實際 `__file__` 均在 C 版。證據：`t16_c_web_it_first_execution_031`、`t16_c_web_it_import_fix_apply_034`、`t16_c_web_it_import_fix_verify_035`（PASS）。

## RCA-2：Remote Binding workspace 不一致

第二次實測以合成測試資料夾作為 sender 的 `workspace`，但 C 版 Remote Binding 綁定的是另一個實際工作區。Runtime 正確拒絕 `remote_task_workspace_binding_mismatch`，該次沒有執行 query_project action。修復：sender 的 `workspace` 改由 `load_binding(ROOT)['workspace']` 取得；合成專案根目錄仍由受限任務要求與 query_project 的 `project_root` 指定，沒有放寬 Runtime 安全檢查。證據：`t16_c_web_it_retest_037`、`t16_c_live_binding_workspace_inspect_042`、`t16_c_web_it_binding_fix_apply_049`、`t16_c_web_it_binding_import_verify_053`（PASS）。

## RCA-3：外層命令逾時造成驗收狀態未落盤

修復後的正式 Web IT 啟動成功，但外層 `run_command` 約 30 秒即逾時，導致測試器未能將持久化狀態由 `IN_PROGRESS` 更新為 PASS。外層逾時不是 Edge 任務失敗，不能直接重送以免重複執行。對既有任務進行唯讀檢查，發現任務為 COMPLETED、final `task_outcome=SUCCESS`，只有一個 query_project action，內含七個指定 queries。初版檢查器把字串型 journal result 誤當 dict 而拋出 AttributeError；後改以原生 `validate_happy(raw,root)` 逐項驗證字串結果。證據：`t16_c_web_it_bound_workspace_retest_054`、`t16_c_existing_web_task_inspect_execute_058`、`t16_c_web_it_journal_result_type_review_059`、`t16_c_existing_web_task_verification_execute_061`（PASS）。

## 已取得的正式 Web IT 驗收證據

既有任務 `TASK-CBC1FA5ABC8A0DD5EDF4`：COMPLETED／SUCCESS；恰好一個 query_project action；七項操作依序為 `list_tree`、`read_range`、`search_text`、`find_references`、`read_symbol`、`get_build_configuration`、`get_file_metadata`，全部由 `validate_happy` 驗收。檔案 SHA256：`e0498571e1385ec60acc98fd472ad89028b960e7f596a3ab89cbb76948ee9b26`；snapshot：`76515635882fa5f8199f6b44da21bfcfed9432b3780a13762830d36965543451`。無截斷、零附件。最終結果回寫以再次成功執行 `source/run_t16_c_verify_existing.py` 為先決條件；不能只憑此文件標記 PASS。

前置 T16 UT 與 access regression 已 PASS；IT-R 已 PASS（四輪）。最終工作樹範圍與測試狀態需另行核對。