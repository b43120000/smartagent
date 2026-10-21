# Runtime 1–6 驗證與修復紀錄

日期：2026-10-14。修改與測試包：`C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`。

後續完整規劃：見 [38 工具 E／V／R UT 與 IT 規劃](runtime_result_migration.md#38-工具-evr-ut-與-it-完整驗證規劃2026-10-14)。
此處 1–6 的局部 PASS 保留；不代表新增的 38 工具矩陣已執行。

驗證分層：固定合法 action 的隔離測試（UT／受控整合）與正式網頁 Planner IT 分開計算。網頁 IT 經 LOCAL_TEST 模擬 Telegram ingress、正式 supervisor／worker、真實網頁模型及 Runtime；不傳送 Telegram 網路訊息，因此不含 Telegram transport IT。

| 測項 | 原狀態／未通過現象 | 已定位 RCA | 修復 | 本次固定流程 | 本次網頁 IT |
| --- | --- | --- | --- | --- | --- |
| 1 INSPECT，不附 verify | Get-Location 成功後仍被要求驗證；曾另進錯誤 shell；唯讀任務產生不必要 plan_id | run_command 被一律視為 mutation；成功觀察輸出無獨立准入；「不要修改」誤認為規劃意圖 | 只允許可證明的 observation 命令以 SUCCEEDED／UNVERIFIED 完成；GENERAL 不獲此權限；修正规劃分類 | PASS | PASS，一個 Get-Location，無額外 verify／plan_id |
| 2 MUTATE，相對路徑＋同 cwd 驗證 | 使用者先前回報 PASS | 無新增失敗證據 | 未改動此測項 | 本次未重跑 | 沿用先前 PASS，非本次重新驗證 |
| 3 刻意後置驗證 FAIL | SUCCEEDED／FAIL 正確，但負向測試不能以達標結案 | 將實際 postcondition FAIL 與負向測試的 assertion outcome 混為一談 | 事前 declared expected_verification；Runtime 另計 expectation_status，不覆寫原 FAIL；拒絕驗證程序錯誤冒充預期 FAIL | PASS | PASS，一個 MUTATE，SUCCEEDED／FAIL；expectation_status=PASS；任務 SUCCESS |
| 4 跨 request FAIL→PASS 驗證歷史 | verifies_action_id_unknown；兩筆正式歷史為 0 | 一般新 request 不載入原 action；必須明確接續並檢查 owner／workspace | 使用正式 FOLLOWUP_VERIFY；唯讀准入、匯入可信 action journal、將有效驗證更新寫回來源；禁止原 MUTATE 重播 | PASS | PASS，兩個 VERIFY；原 FAIL 保留，最新有效 PASS；無寫入／附件 |
| 5 delete_path→關聯 VERIFY→建立標記 | scope 將「建立…測試資料夾」黏入路徑，第一個 list 就拒絕 | unquoted path suffix 未截斷；修正後固定測試又發現非 run_command 的驗證目標被送入 operation parser | 限定修正建立資料夾語句的路徑尾綴；非命令目標依執行成功＋驗證 PASS 更新有效終態；保留一次性安全批准 | PASS，Temp 隔離完整鏈，含刪除及標記驗證 | PASS，正式任務 COMPLETED／SUCCESS；刪除、關聯 VERIFY、標記檔及最終驗證均完成 |
| 6 BUILD＋APK | 使用者先前回報 exit=0、APK 存在且非零，PASS | 無新增失敗證據 | 未重新建置 | 本次未重跑 | 沿用先前 PASS，非本次重新驗證 |

## 正式網頁 IT 證據

- 測項 1 修前重現：`RR-LOCAL_TEST-A304F019C103E521`／`TASK-CFAAEC2A7D1C9A18613E`，成功觀察後仍被擋，最後中斷。
- 測項 1 修後：`RR-LOCAL_TEST-4B4866778ACD0AA6`／`TASK-D736F138BB52BC617828`；2 輪，約 147 秒，SUCCEEDED／UNVERIFIED，驗收 SUCCESS。
- 測項 3：`RR-LOCAL_TEST-05D2FB0BBF26EF53`／`TASK-78ED7AB7B7CA3F81E6F1`；3 輪，約 198 秒；原 action `negative-probe-v9-mutate-002`。檔案仍為 ACTUAL_VALUE。
- 測項 4：`RR-LOCAL_TEST-D9E4F0C91B3B9355`／`TASK-1C899BF9C650FA17C886`；6 輪，4 個 accepted ACK，約 365 秒。正式補驗證使用 `後續驗證 <測項3 request_id>` 入口，而非只靠自然語言「上一筆」。執行一個 read_range 與兩個 VERIFY；第 2 輪負向 assertion 未綁定成功契約、第 4 輪誤填 expected_verification=PASS，均拒絕後修正，拒絕的 action 沒有執行。後續已在工具包明列正常 PASS 不提供 expected_verification；本項通過不代表零 repair 對話。
- 測項 4 WRONG_VALUE：`negative-followup-verify-wrong-003`／`VER-AB86BA449111`，原始觀測 FAIL、預期符合 PASS。
- 測項 4 ACTUAL_VALUE：`negative-followup-verify-actual-005`／`VER-FEB92DF0ED6B`，觀測 PASS。
- 已直接讀取持久化 continuation journal：來源 action 原始 verification_status=FAIL；effective_verification_status=PASS；effective_verification_id=VER-FEB92DF0ED6B。歷史中的 `observed_verification_status` 是實際驗證結果；`status` 可表示 expected assertion 的結果，不能混讀。
- 測項 5：`RR-LOCAL_TEST-5F1D73B58EB091D9`／`TASK-7B1C364D557366C99D8C`，已完成。第 1 輪 list_directory 成功；第 2 輪建立空目錄 SUCCEEDED／PASS；第 3 輪 inspect_directory 及精確路徑／屬性檢查成功。第 4 輪安全 VERIFY 的 ReparsePoint 檢查誤用 `[System.IO.FileAttributes]::None`，實際 stdout=False，Runtime 正確記錄 FAIL；第 5 輪改用數值位元遮罩比對後 PASS。這是模型產生的檢查式問題，未修改 Runtime 來放行 FAIL。
- 測項 5 第 6 輪正式一次性安全確認：`APR-2640020CC24FB0D0053C`，delete action `delete-probe-delete-006`，目標為本次新建空目錄 `E:\card\runtime_it\disposable_delete_probe`；manifest 項目數 0、大小 0 bytes。使用者明確回覆「批准」後，核對 request／task／action／目標身分、目錄仍空且非 ReparsePoint，提交單次批准。批准狀態已 CONSUMED、目錄已刪除、永久批准名單仍為空。
- 測項 5 第 7 輪 `delete-probe-absence-verify-007` 成功關聯 `delete-probe-delete-006`，verification_status=PASS，驗證 ID `VER-BEEFA36E1731`。持久化原 delete 的 effective_verification_status=PASS、effective_terminal_eligible=true。
- 第 8 輪才執行 `delete-probe-marker-write-008`，建立 `E:\card\runtime_it\delete_verified.txt`，208 bytes，內容含 DELETE_VERIFIED、實際 delete／VERIFY action ID 及 PASS。第 9 輪 `delete-probe-marker-verify-009` 關聯 marker write，verification_status=PASS，ID `VER-970B15F23B9B`；原 write_file 有效驗證亦為 PASS。第 10 輪正式結案 COMPLETED／SUCCESS。
- 測項 5 合計 10 個實際工具 action、10 輪 accepted ACK，task 約 935 秒（包含約 206 秒等待使用者批准）。觀察用 sender 的 900 秒等待期限先到，sender_exit_code=2；它未取消 Runtime 工作。本次未重送 task，改讀取同一 task 的持久化結果及 worker TASK_COMPLETED，證實最終 SUCCESS。不可把 sender timeout 誤記為 Runtime 任務失敗。

正式 worker log 位於 `localdata/runtime/remote_workers/<task_id>/runtime.jsonl`。

## 本次驗證命令

執行包 `.venv\Scripts\python.exe`，`PYTHONPATH=source`：

```text
python -m WebAgent.tests.validate_failed_matrix_regressions
python -m WebAgent.tests.validate_delete_dependency_matrix
python -m WebAgent.tests.validate_action_loop_runtime_state
python -m WebAgent.tests.validate_task_continuation
```

以上最後一次重跑均 PASS。另已通過 authorized path、action-scoped admission、command field repair、final render context 的針對性回歸。這些不是全 Runtime 100% coverage。

## 安全與界線

- 不放寬 ADS／授權範圍；不永久批准刪除；不把 tool queued／拒絕當完成。
- 不信任模型 operation 標籤本身：觀察命令須 Runtime 可辨識；明顯 mutation 冒充 INSPECT 會拒絕。
- 負向 expected FAIL 必須事前宣告並綁定完成契約；timeout、權限錯誤、verify 程序錯誤不算 assertion PASS。
- 本次修改在執行包，未 sync_back 至開發包，未建立 Git checkpoint。
