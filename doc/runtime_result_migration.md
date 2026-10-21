# Runtime 工具結果遷移與 IT 紀錄

工作包：C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1。
基準 checkpoint：9a2bde4d577fae10b82d787af50ed3fbd6a97bdf。

## 2026-10-16：ChatGPT composer 長輸入期限（真 ChatGPT IT 尚未通過）

案例 `RR-TELEGRAM-4DB1E7C5FC25CB92`／`TASK-5E390F5BC352EEC5082A`：第三輪輸入 43,556 字元／51,106 UTF-8 bytes，停在 `composer_insert_begin`，沒有 `composer_insert_returned` 或 submit；最後由使用者中斷。這段不是模型補欄位空轉：前兩輪已回覆，尚未下發寫入 HTML action。原 timeout 在同步 `keyboard.insert_text()` 返回後才開始，不能限制該呼叫本身。

修改：僅 retained-CDP ChatGPT 的 composer path 改用獨立 helper；Windows 先 suspended 啟動、加入 KILL_ON_JOB_CLOSE Job Object，再放行 stdin／Playwright。整段 write 上限 30 秒，獨立 readonly observer 上限 8 秒；必要程序終止與鎖等待另有短暫有界寬限。只終止自己建立的 helper/driver，不關閉 retained Edge。完成後及真正 click 前都以 URL、user/assistant anchors/counts、generation 與完整 composer 比對；不採信 unsafe composer bypass。此期限不代表其他 DOM、附件、生成等待都已加上硬期限。

逾時、部分草稿、對話改變或不可判定：持久化 request-page quarantine，禁止送出／清空／重填，ask 不再進同步 UI cleanup 或把無關新 turn 當成本輪已送達。journal 只存雜湊、長度、anchors，不存 prompt 本文。已證明在 baseline 前失敗的 CDP connection error 為 FAILED_PRE_WRITE，保持 READY_IDLE，但本輪不自動重試；修好連線後允許新的明確請求。變更不重播已完成工具，不改 Gemini/Claude/M365 或 isolated-browser 的舊 writer 路徑。未知草稿尚無自動解鎖／自動接讀已送出回覆的功能，需人工釐清或換對話。

獨立複審後另補：已備妥草稿／送出保留狀態禁止其他 helper 覆寫；final observer 在 page process lock 下轉為 SUBMIT_RESERVED，實際 click 完成才標 SENT，防止 observer 與 click 間被另一個 writer 取代。scope 未確認時連下一個 ask 的 pending-submit reconciliation 也拒絕，不能把其他 user turn 當成自己的送達證據。TARGET_NOT_UNIQUE 等尚無 baseline 的結果同樣分為 prewrite failure，不永久封鎖已修好分頁的後續明確請求。

| 驗證 | 狀態 | 證據／限制 |
|---|---|---|
| 新 composer UT | PASS | `validate_composer_operation.py`：完整／部分／未知／turn 改變、quarantine、不重送、取消、真 sleeping child 期限、prewrite 分流、其他 provider 不變。 |
| 既有 composer／submit 回歸 | PASS | unsafe bypass、mismatch diagnostics、rich-link matching、submit identity、Gemini send control。 |
| 離線真 Edge/CDP 同尺寸輸入 | PASS | `localdata/runtime_it/matrix38/composer_fixture_it.json`：43,556 chars／51,106 bytes，原文精確讀回、1 user/1 assistant anchors、pre-click observer、受限清除。它是受控 DOM，不是 ChatGPT E2E。 |
| 真 driver 卡死故障注入 | PASS | 同上：只在測試專用頁面部分輸入後卡死，helper/driver 終止、原 Edge 存活、草稿不重播；另驗證真 assistant DOM 改變被拒。 |
| 既有使用者 Edge／ChatGPT | BLOCKED | `localdata/runtime_it/matrix38/composer_deadline_it.json`：1272 CDP WebSocket 已建立，但 Playwright 初始化在 5 秒及獨立 15 秒 probe 都逾時。尚未輸入測試內容、尚未 submit，未重啟或清 profile。需使用者同意重啟此專用 Edge 或修復連線後，再做同尺寸真頁面測試。 |
| 舊 protocol recovery readback 測試 | BASELINE_FAIL | `validate_protocol_recovery_readback.py` 在本次工作樹及 HEAD 原碼均於 fake scraper 缺 `_web_ui` 失敗；不列為 PASS，也不為本次修復改動不相關測試 fixture。 |

整體：實作與有界離線驗證完成，真 ChatGPT IT 未完成，不宣稱原使用者任務已完成或所有瀏覽器等待已排除。T01–T38 原矩陣通過數不因本項額外傳輸測試而變動。未自動 sync-back、update、Git commit 或 push。

## 固定契約

SMARTAGENT_TOOL_RESULT_V1 使用 Runtime 狀態與業務狀態分層。
JSON 工具保留既有 schema/status/snapshot/ACK/plan/query 欄位，新增 _runtime_result。
文字工具保留 transcript，最後只有一個 [RUNTIME_TOOL_RESULT]。
共用狀態：SUCCEEDED、FAILED、REJECTED、UNKNOWN、QUEUED、RUNNING、PARTIAL。
排隊及程序執行中不是終態成功。拒絕不是已執行。
Command 的執行、驗證及預期失敗 assertion 分開；模型敘述不是客觀證據。

## 階段順序

| 階段 | 實作 | UT | 本機 Telegram 模擬＋真實網頁 IT |
|---|---|---|---|
| P0 讀取與命令 | 已實作 | PASS：validate_p0_read_command_contract | 正向 PASS；負向工具拒絕正確，Task 補救 FAILED |
| P0 附件／下載／編輯／bundle | 已實作 | PASS：validate_p0_artifact_contract | 真實下載／網頁編輯／bundle 未測；排隊不視為交付 |
| P1 分塊／project／semantic／計畫與驗證 | 已實作 | PASS：validate_p1_result_contract，25 工具契約覆蓋；indexed query 回歸 PASS | 初測 PARTIAL；整合續接 commit／驗證／index／query 成功 |
| P2 搜尋／summary／Drive／executor | 已實作 | PASS：validate_p2_result_contract，4 工具 | summary/search PASS；Drive/executor 外部服務未測 |
| P0＋P1＋P2 整合 | 已完成 | — | SUCCESS：6 rounds／5 個實際工具 action／0 協議拒絕 |

依使用者確認不驗證真實 Telegram 網路。IT 用 local_telegram_sender 的正式接收與排隊、
真實網頁 Planner 和 Runtime，結果寫本機。此方式不等於 Telegram sendDocument 已交付。
每次記錄 request/task/event、round/action/repair 數與客觀檔案或命令證據。
有失敗或能力未配置時明確標記 FAILED/NOT_TESTED，不稱全面通過。

## 真實 Runtime／網頁 IT 紀錄（2026-10-13）

round 是 planner_round_started 次數；action 是 tool_started 次數；不含 Progress/control block。
拒絕次數不等於逐欄 bridge 次數。以下數量取自 p0_it_launch_cb4e3a50.stdout.log。

| request（RR-LOCAL_TEST- 前綴） | round | 實際工具 action | 協議拒絕 | 結果 |
|---|---:|---:|---:|---|
| 3E82D8D510741EFB | 6 | 2：list_directory、run_command | 3 | SUCCESS，命令實際 exit 0／驗證 PASS |
| 696395E10E17C9A5 | 5 | 4：read_file、upload_file、return_artifact、read_file | 3 | FAILED；缺檔與 scope 拒絕正確，但後續 REPLAN 沒收斂 |
| 66926E8FBBB2EB5E | 3 | 3：begin、chunk、chunk | 0 | PARTIAL；OPEN／next_offset=8，目的檔案不存在，未 commit |
| EA4A6860F4888550 | 7 | 0 | 7 | FAILED；save_session_summary 被 Web Direct 拒絕清單阻擋 |
| E00C0DA61ED2C5C7 | 2 | 2：save_session_summary、web_search | 0 | SUCCESS；保存讀回 PASS、搜尋 RESPONDED／1 筆 |
| B97250B551CDF148 | 6 | 5：commit、VERIFY、INDEX_ONLY、read_range、summary | 0 | SUCCESS：接續原 P1 write_id，不重寫 chunks |
| BF195071334C7227 | 4 | 3：query、query、read_file | 1 | FAILED：prompt 路徑與後續欄位黏連，ADS 安全拒絕；非 EOF 驗收 |
| D69F28456BFD3DDA | 2 | 1：query_project | 0 | SUCCESS：內容／EOF／cursor 原始欄位符合預期 |

P2 修正：只從 Web Direct 拒絕清單移除已有 schema、安全檢查與本機實作的
save_session_summary。ask_executor／execute_artifact_bundle／web_edit_file 保持該介面原拒絕政策；
它們的 producer UT 通過不代表可由 Web Direct 呼叫。P2 UT 現在由 context.execute
實際經過協議能力入口，另確認上述三項沒有被順帶開放。

整合測試交易：IT-P1-20261013-A；目的檔案為
C:\Users\user\Desktop\remoteagent\runtime_it\tool_results_p1_20261013\chunks.txt。
暫存 8 bytes 不等於目的檔案已保存。原 P1 的 PARTIAL 紀錄保留，不改寫成 PASS。

整合的客觀產物：chunks.txt=8 bytes，完整 bytes 為 ABCDEFGH，
SHA256=9ac2197d9258257b1ae8463e4214e4cd0a578bc1517f2415928b91be4283fc48。
獨立 VERIFY 關聯原 commit action，execution=SUCCEEDED、verification=PASS、exit_code=0。
INDEX_ONLY：file_count=1、total_bytes=8、attachments_uploaded=0。
最終 event=EVT-4FA43C2BBB0BA4BF355B；整合 outcome=SUCCESS。

## IT／回歸揭露的欄位調整

- read_file 預檢改用 stat 保留 PermissionError，不以 is_file 吞錯後猜不存在。
- read_range：end_line 超過 EOF 不代表截斷。實際還有行才 truncated=true 並給 next_cursor。
  新增一行檔／250 行檔、240 行頁面上限與 EOF 回歸。
- query_project 已知 root／query 欄位錯誤回 PREFLIGHT_REJECTED，executed=false、
  possible_partial_execution=false；未知可能部分執行的錯誤仍 UNKNOWN，不鼓勵盲目重試。
  早期例外出口同樣保留 request_id／task_id／action_id，UT 檢查拒絕回合身分不遺失。
- query 的 max_bytes 包括新增 _runtime_result，不以加入封套為由突破原預算。
  沿用原生分頁裁切，保留已存在 response_truncated；如必須移除整個結果，標 PARTIAL，
  不允許以遺失結果作 terminal success。
- 舊 indexed-query 測試原本期待 ValueError／文字拒絕前綴，改為驗證結構化 REJECTED、
  原始 detail 與未執行狀態；整組現在 PASS，沒有取消安全判定。
- 額外 EOF IT 的單行 prompt 把中文逗號後的 snapshot/query 併入路徑，安全拒絕正確；
  這是尚未修改的自然語言路徑抽取相容性缺口，不能稱該 IT 通過。重測將精確路徑獨立成行。

EOF 重測實際欄位：status=READY，content="1: ABCDEFGH"，total_lines=1，
truncated=false，next_cursor=null，attachments_uploaded=0。
查詢 Runtime execution=SUCCEEDED、verification=UNKNOWN；沒有將 UNKNOWN 改寫成 PASS。
此測試成功指查詢欄位符合指定契約，不表示一般修改操作可以跳過 postcondition verification。

清理：E:\card\.agents\agent_session_summary.md 在測試前不存在。
整合測試後以 SHA256 與 request marker 確認僅屬本次測試，再移至執行包
localdata/runtime_it/backups/p012-summary-20261013.md，保留可恢復紀錄，未移除其他摘要或目錄。
程式碼只改 C 槽執行包；未 sync-back、未改 E 槽開發 source、未建立新的 Git commit。

整體覆蓋仍為 PARTIAL：上述成功是已列明的本機模擬／真實 Runtime 路徑，不代表所有
P0/P1/P2 工具的網頁及外部服務 IT 全覆蓋。負向 REPLAN 收斂与未測項目仍保留待辦。

未測範圍：真實 Telegram 網路交付、Google Drive OAuth／交付、Local AI executor、
fresh 圖片下載、網頁編輯 provider、真實 bundle 端到端。不能宣稱全部工具 IT 覆蓋。
舊 validate_evidence_backed_completion 的精確字典斷言在 HEAD 基準亦失敗，未列 PASS。

## 38 工具 E／V／R UT 與 IT 完整驗證規劃（2026-10-14）

此節追加至原 P0／P1／P2 規劃，保留歷史失敗與局部 PASS，不覆寫前述紀錄。
工具數來自執行包 `tool_capabilities.get_allowed_tools("remote")`，共 38 個 action tools。
`report_progress`、`final_response`、`turn_commit` 是控制協議，另做跨工具測試，不混入 38 個工具數。
`ask_executor`、`execute_artifact_bundle`、`web_edit_file` 不在這個介面的 allowlist，不能為湊覆蓋而開放。
本節是規劃，沒有宣稱新 38 工具矩陣已跑完；只有實測與客觀證據才能填 PASS。

### 環境與驗證方法

- 程式、測試腳本與實作修復一律在 `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`；不 sync_back、不改 E 槽開發包、不自動更新或重啟其他包。
- UT 使用隔離 Temp fixtures；IT 由上述執行包執行，產物放在已授權 `E:\card\runtime_it\matrix38\<run_id>` 的新測試範圍。這是測試資料，不是 E 槽開發 source；不碰 HDRViewer、既有文件或正式產物。
- 固定合法 canonical action，先確認 tool／operation／schema／identity／授權與目標參數正確，再進入 Runtime 驗證。正向測試不以模型隨機輸出作基準。
- IT-R：固定 planner/action，經正式協議 loop／tool context／實際執行器和真實本機 I/O，驗證 Runtime 自身。外部 SDK mock 只能算 UT，不算真實外部服務 IT。
- IT-W：LOCAL_TEST 模擬 Telegram ingress＋真實網頁 Planner＋Runtime；先確認模型 action 正確才計入 Runtime 驗收。模型格式錯誤另記 protocol failure，不誤歸類為 Runtime 執行錯誤。獨立 action 可批次，相依 action 必須保留驗證屏障。
- 本次任務不要求真實 Telegram 網路傳送。`return_artifact` 因此須拆開 Runtime queue IT 與實際 sendDocument／sendPhoto 交付 IT；後者未執行不能宣稱已交付。Drive／網頁附件／下載必須各自測真實服務。
- 測前固定 source revision／dirty diff digest、protocol manifest、ACL 模式、Workspace、provider、登入與配額；先測目前使用的 ChatGPT。其他 provider 不能由 ChatGPT 的 PASS 推定。
- 輸入／預期、實際 request/task/action/verification/artifact/snapshot/plan ID、前後內容／SHA256、實際 command/cwd、checker 呼叫證據、原始回覆及工具呼叫次數都需保存。只記 log 字樣不算實際效果證據。
- 刪除只針對本次新建且已確認的測試目錄，保留單次安全批准；取消或逾時不略過批准、不永久授權。外部測試只使用合成資料，未配置服務時 BLOCKED_ENV，不自動索取或改寫憑證。

### 三個獨立通過條件

| 層次 | 通過條件 | 不可當作通過的情況 |
|---|---|---|
| E：執行結果 | 合法正向 action 真正到達執行器，實際目標／command／cwd 與輸入一致，產生可觀測效果或真正取得讀取結果 | 只有 tool_started、模型說做了、命令 exit=0 但該工具要求的效果未出現 |
| V：檢查狀態 | 對應 checker／前後置條件真的執行；以獨立 fixture ground truth 對照其判定。刻意錯條件應如實 FAIL；checker 程序錯誤不能冒充 PASS | 有 verify 欄位卻沒呼叫；只沿用歷史 PASS；把 checker exception 當條件符合 |
| R：回覆狀態 | Runtime 原始結構、action ledger、最新 effective status、任務 outcome、最後呈現一致，保留原始／最新／歷史的區別 | 拒絕記成 SUCCEEDED、RUNNING／QUEUED 當完成、歷史 FAIL 蓋過最新 PASS、缺 outcome 亮綠燈 |

讀取／查詢工具不一律要求新增 run_command VERIFY。V 可由工具本來的資料／scope／snapshot 檢查與測試端獨立對照完成。
若工具沒有內建 postcondition checker，明確記「測試端對照已執行／Runtime 未提供內建驗證」；不能把測試端 PASS 偽造為 Runtime `verification_status=PASS`。
例如查詢的 `UNKNOWN`、觀察命令的 `UNVERIFIED` 可能是正確回覆，需依該工具契約判定。
正常負向測試也能測試 PASS：拒絕應 executed=false、錯誤條件應 FAIL；不表示被拒 action 有執行或業務目標已成功。

每列新矩陣都要分別記 `UT-E／UT-V／UT-R`、`IT-R-E／V／R` 與 `IT-W-E／V／R`。
本機工具至少含成功與可控失敗；交易／副作用工具再含 replay／crash／部分完成。外部服務亦含錯誤回覆／拒絕，不以憑證缺失充當完整負向 IT。

### 逐工具規劃表

「既有」僅列已知局部證據，不是新矩陣整項 PASS。以下所有新矩陣的 UT、IT-R、IT-W 初始狀態均為 **PENDING**。
表內 IT 欄包括要真正執行的操作及獨立驗收；R 均另套上述 ledger／回覆／五終態一致性規則。
RCA／最終解法的「待實測」不是預先假設 Runtime 有 bug；發現失敗後才填證據支持的根因與實際修復。

| 編號／工具 | UT：固定合法 action 的 E／V／R | IT：實際操作與客觀檢查 | 既有覆蓋／仍需補測 | RCA（失敗後標註） | 最終解法與重驗證 |
|---|---|---|---|---|---|
| T01 `read_file` | 真實 fixture 讀取；空檔／UTF-8／大檔界線、缺檔及 PermissionError；對照 bytes／截斷／ref；排隊不算讀取完成 | 讀已知內容與缺檔，讀取輸出對照檔案 SHA／encoding；確認按內容契約而非意外上傳 | 契約 UT；IT 曾正確拒絕但補救未收斂，補正向／權限／長檔 | 歷史：PermissionError 曾被檔案存在性判斷吞掉；新失敗待實測 | 既有 stat 保留錯誤分類；新矩陣重驗證三態與輸出 |
| T02 `list_directory` | 限定目錄、空目錄、檔案／子目錄、缺目錄、範圍拒絕；獨立清單對照，未擴大 scope | 固定第一層 fixture 清單並與實際 filesystem 比較；缺路徑 executed=false；獨立 action 不被其他失敗擋 | 正向網頁 IT 已有；補邊界 | 歷史：自然語言尾綴黏入授權路徑 | 既有尾綴／接續目標解析修復；補空白、Unicode、拒絕重驗證 |
| T03 `inspect_directory` | 多 paths、recursive/sample_limit、缺項與混合結果；確認採樣／計數與 partial | 檢查已知樹的 metadata、大小、recursive 邊界，逐項與 filesystem 對照 | 刪除前正向 IT 有；補多路徑／partial | 待實測 | 待 RCA 後填寫 |
| T04 `find_file` | root/name/max_results、同名、無結果、超額、非法 root；驗證 matcher 與 scope 真有執行 | 合成巢狀同名樹，精確列出匹配路徑；查不到是正常空結果，不擴大 root | 共用契約 UT；補完整 IT | 待實測 | 待 RCA 後填寫 |
| T05 `write_file` | 真正寫入、UTF-8、內容預算、權限拒絕／寫入失敗；讀回精確 bytes；未寫不能 SUCCEEDED | 指定新檔，獨立讀回 hash／大小；改寫 fixture 並驗證；不同 action 不誤串 | marker 寫入＋關聯驗證 IT 有；補錯誤／界線 | 歷史：關聯非命令目標誤進 operation parser | 既有非命令有效驗證更新修復；補寫入失敗重驗證 |
| T06 `delete_path` | fresh 空目錄、精確 manifest／安全批准；拒絕／批准逾時／目標變更；執行與檢查呼叫分開 | 僅新建 fixture，單次批准後刪除；關聯 VERIFY 目標不存在才建立 marker；拒絕時目標仍存在 | 完整正向網頁 IT 有；補拒絕／TOCTOU 邊界 | 歷史：路徑尾綴誤解析、delete 被當 run_command 驗證 | 既有兩項修復；不放寬安全批准，補負向重驗證 |
| T07 `begin_file_write` | write_id 身分、encoding、overwrite、目的地 hash；真建暫存交易而非最終檔 | 開啟交易，確認 OPEN、目的檔未寫入、交易 metadata 真保存；collision 不吞掉舊交易 | 正向 UT／IT 有；補身分衝突 | 待實測 | 待 RCA 後填寫 |
| T08 `write_file_chunk` | 真 append、UTF-8 byte offset、自動 offset、index／SHA／size；重複、跳 offset、wrong chunk 不誤接收 | 多輪短 chunk；檢查 next_offset、暫存 bytes／hash；重啟後接續，不把 chunk_received 當檔案完成 | 正向兩塊 IT 有；補異常／crash | 歷史測項曾 offset 不連續；新根因待實測 | 既有 Runtime 計算 offset 路徑需重驗證，不讓模型猜 byte offset |
| T09 `commit_file_write` | expected_size／SHA／缺塊、destination 衝突、原子提交；獨立讀回 final bytes；replay 不重寫 | 完整提交並 VERIFY；不完整交易不能產生 final file；提交後 crash/retry 查核同一交易 | 正向 commit→VERIFY IT 有；補原子性／失敗 | 待實測 | 待 RCA 後填寫 |
| T10 `abort_file_write` | 取消正確 write_id；不傷既有 final／其他交易；重複 abort／commit 後 abort 的契約 | 建交易收部分 chunk 後取消；檢查臨時資源與終態，原目的檔不變，後續不能偷 commit | 原 UT 有 native abort；網頁 IT 待補 | 待實測 | 待 RCA 後填寫 |
| T11 `run_command` | 10 operations、實際 command/cwd、exit／stdout/stderr／process；verify 真呼叫；負向 assertion、無產物、timeout 與 replay | 真正 INSPECT／MUTATE／BUILD／TEST／VERIFY／Git／PROCESS／TRANSFER／GENERAL；客觀檢查檔案／PID／commit；exit=0 無預期產物仍不得驗收成功 | 1–6 矩陣部分通過；10 operation 與錯誤分支仍待補 | 已定位：觀察被当修改、預期 FAIL 與驗收混淆；其他待實測 | 既有 observation proof／expectation 分層；按 operation 補正負向 |
| T12 `aggregate_verification` | 每個 checker 真呼叫、cwd 一致；全部 PASS／混合 FAIL／程序錯誤；關聯與 history 精確更新 | 針對同一 fixture 多檢查；故意錯一项後修驗證，不重做原 mutation；對照每項原始結果 | 曾用錯 shell；新全矩陣待測 | 歷史：Get-Location 在驗證執行環境無法辨識；精確新根因待測 | 依實際 shell/cwd 契約定位，不繞過 checker |
| T13 `inspect_project_scope` | 小專案 root、inventory／排除項、上限與安全範圍；不冒充 list_directory | 合成小專案，對照實際 files/count/bytes；確認掃描只在指定 root，無附件 | 共用契約 UT；完整 IT 待補 | 待實測 | 待 RCA 後填寫 |
| T14 `inspect_project_working_set` | exact paths、Git／filesystem binding、缺檔、無關檔不納入；snapshot checks 真執行 | 任務只選兩檔；改無關檔／選中檔各一次，對照 working set 身分與變動，無全案阻塞 | 共用契約 UT；完整 IT 待補 | 待實測 | 待 RCA 後填寫 |
| T15 `project_sync` | INDEX_ONLY／DELTA／FULL_BUNDLE、base snapshot、容量／授權／partial；strategy 所對應流程真正執行 | 小專案 INDEX_ONLY 不上傳；改一檔做 DELTA；FULL_BUNDLE 僅用一次小合成 bundle 測必要上傳與 ACK | INDEX_ONLY 網頁 IT 有；DELTA／bundle 完整待補 | 歷史：附件額度／ACK／身分問題；本矩陣根因待重現 | 保留 Runtime hash 權責；拒絕與配額如實回覆，不能靠重傳掩蓋 |
| T16 `query_project` | read_range／read_symbol／search 等現有 operation；EOF／cursor／max_bytes、snapshot 過期、缺檔、批次 partial | 同 fixture 多查詢；逐字對照來源與版本，查詢失敗不丟合法獨立結果；無 source 附件 | read_range／EOF IT 有；其他 operation 待補 | 既有 EOF/truncated 與 preflight 身分問題已修；新失敗待測 | 原生分頁／結構化拒絕；不能改 hash 或放寬 scope |
| T17 `compare_project_snapshot` | unchanged／新增／修改／刪除、非法 known_snapshot；比對程序真執行 | 保存基準後改 fixture，核对準確差異清單／scope，不用現在快照冒充舊 baseline | 共用契約 UT；完整 IT 待補 | 待實測 | 待 RCA 後填寫 |
| T18 `build_project_bundle` | 實際 bundle／manifest、max_bytes/max_files、排除敏感檔、過程失敗不假成功 | 小合成專案打包；獨立解包對照 manifest／檔案 bytes；建包不等於已上傳 | UT／IT-R／Web PASS | stale SHA、輸出scope及leaf link風險 | fresh SHA、精準scope、原子part替換及實讀驗證；詳見T18證據 |
| T19 `build_project_delta` | 正確 base_snapshot_id、changed/deleted entries、無變更及 base 拒絕；patch inventory 驗證 | fixture 基準後增改刪；獨立解讀 delta 核对只含應有變化，來源版本正確 | UT／IT-R／Web PASS | scope未限制、原生驗證未發布、深層junction越界 | request scope、manifest replay、lexical端點保護；詳見T19證據 |
| T20 `extract_project_dependencies` | 真 parser 來源pattern、已知觀察、缺設定／unsupported；不把猜測記成 confirmed | 小 CMake／Python／include fixture；Gradle未支援需明列，保留來源SHA／行號 | UT／IT-R／Web PASS | parent scope擴大、warning卻標成功 | freshSHA／request scope、source checks、PARTIAL與FAILED；詳見T20證據 |
| T21 `inspect_project_ledger` | 真讀持久化 ledger、limit／無紀錄、snapshot/provenance；不虛造歷史 | project_sync→query→inspect，對照真 ledger file，讀取不修改來源紀錄 | PASS：UT／IT-R／Web；首測拒絕保留 | JSON鄰接欄位冒充ADS；讀錯誤吞成EMPTY；索引與歷史非同次讀取；損壞索引欄位逸出 | JSON字串邊界保留ADS；讀失敗明確FAILED；同一history生成回覆；索引未確認回PARTIAL不改磁碟；strict欄位檢查 |
| T22 `query_project_history` | event_types／snapshot/path/time/limit filtering 真執行，空結果／邊界 | 固定幾次 fixture 事件，逐種篩選核对 IDs/順序/數量；不同專案不串資料 | PASS：UT／IT-R／Web（6查詢） | 共用history讀取錯誤原會吞成空；不存在專案也可回空成功 | 只允許缺少ledger代表無歷史；缺專案拒絕；讀錯誤FAILED；filter/順序/0與500上限對照持久紀錄 |
| T23 `inspect_semantic_map` | 已存在／未建立／局部 paths、source revision／stale 狀態；不把未載入當已分析 | 小 source 的已知 map 讀取，修改 source 後核对 stale／有效範圍與 snapshot | PASS：UT／IT-R／Web（3 actions／2 rounds） | 同size/mtime誤FRESH、損壞吞成MISSING、純觀察誤觸強制補救 | fresh bytes；讀失敗FAILED不冒充部分修改；scope限定；只有真正plan prerequisite才強制補救 |
| T24 `update_semantic_map` | schema、path、snapshot、source SHA 檢查真呼叫；成功持久化，輸入拒絕不留更新 | 提交小 map，獨立讀回；錯版本／SHA 應拒絕且原 map 不變，不陷入全案 re-sync | PASS：UT／IT-R／Web（1 action／2 rounds） | 局部更新刪其他rows／抹global語意；UPDATED沒有保存驗證證據；讀回FAIL可被FRESH繞過 | 保留範圍外rows及global欄位；真讀回JSON/Markdown；FAIL鎖住plan恢復，不靠FRESH漂白 |
| T25 `update_semantic_map_file` | 真讀 fixture JSON／expected_sha256；等價 inline 結果、錯 hash／缺檔拒絕 | 小 JSON 檔更新後讀回；提交前改檔應拒絕，不能照舊檔內容假執行 | PASS：根目錄與.agents UT／IT-R／Web；磁碟audit PASS | 根目錄patch本身進snapshot；最後前置檢查後的外部變動仍可誤PASS | 精確v5「只扣這一份patch」快照等同原base才轉接正常更新；全來源／Git仍參與，保存後全快照改變回PARTIAL/FAIL |
| T26 `propose_task_plan` | canonical plan 真校驗／freeze；plan_id／source scope／snapshot 保存；VALID 不等於已實作 | 合成一檔任務提出完整 plan，讀 ledger 核对 PLAN_FROZEN；未 execute 前檔案必須不變 | PASS：UT／IT-R／Web | 純txt錯套semantic要求；錯型別逸出；scope漏綁；existing hash未重算；可預測tmp | 限source semantic、精準拒絕、Runtime-owned scope、existing hash/ID/state重檢、獨占隨機temp、request-scoped workspace |
| T27 `propose_task_plan_file` | 真讀檔、expected_sha256、schema checks；與 inline plan 等效 | 合成 plan JSON 提案／讀 ledger；缺檔、檔案改動、hash 不符不得 freeze | PASS：UT、真 Runtime loop、Edge/ChatGPT IT；1 action／2 rounds | 檔案版未使用 Runtime request_scope，與 inline 不一致 | 統一可信身分、request-scoped workspace、絕對路徑讀授權；保存後核對 hash／scope／來源未修改 |
| T28 `repair_task_plan` | 指定 rejected plan 的 repair 真校驗；只補允許範圍，plan identity／replay checks | 刻意建立可控拒絕 fixture 再合法 repair；確認新 frozen plan 與原拒絕歷史，不逐欄無限補問 | PASS：UT／IT-R／Web | 舊UT的next_action前提衝突；Web引用拒絕及修復兩筆時，歷史拒絕誤擋SUCCESS | fixture對齊宣告；只讓精確匹配Runtime指定且實際PLAN_FROZEN的repair取代原未執行拒絕；保留歷史與無關FAIL |
| T29 `execute_frozen_plan` | 正確 plan_id／scope／snapshot、子 action 真執行，dependency、拒絕／partial／replay | 執行合成 frozen plan，獨立核对每個檔案／子 action 與驗證；重複 ID 不重做副作用 | PASS：14項UT、IT-R；Web原執行PASS，切Edge後唯讀接續1 action／2 rounds結案SUCCESS | 原3項已修；原結案STALLED與後續verify.type入口／接續guard不一致已留歷史，不冒稱原單次直通 | 穩定identity、canonical target、例外回滾及實讀；協議／接續guard對齊executor的action/type別名且拒絕衝突；不含crash恢復 |
| T30 `validate_edit_plan` | required fields／source body／snapshot／SHA／BEFORE 檢查真執行；VALID 不可修改 source | 完整單檔編輯計畫驗證；獨立確認來源未變。wrong BEFORE／stale snapshot 拒絕 | UT／IT-R／Web PASS | wrong BEFORE仍VALID、duplicates、path/scope及錯誤UNKNOWN | fresh snapshot、與apply共用path安全规则、唯一BEFORE、編碼拒絕與完整validation狀態；詳見T30證據 |
| T31 `apply_edit_plan` | 精確 BEFORE→AFTER、precheck、backup／rollback／partial；未套用不得 APPLIED | 只改 fixture 單檔，對照前後 bytes／hash／範圍；注入可控故障核对 rollback，再独立 VERIFY | PASS（UT／IT-R／Web；本次排除crash） | CRLF／hardlink／rollback狀態、linked VERIFY循環依賴、錯resource root | 精確bytes、原子publish與如實回滾；唯讀linked驗證解除循環；canonical resource；crash延期、不宣稱支援 |
| T32 `upload_file` | 真文件檢查／provider call 次數、附件身分、selected/processing/ready；拒絕不能當已上傳 | 一份小合成 txt，確認附件 ready 且實際模型可讀 request marker；額度不足 BLOCKED，不盲目重傳 | PASS：UT／native loop／真Web；使用者核准單次重測後取得CONSUMED及隨機marker | 先前上傳限制拒絕未必為真額度用盡；原因尚未完整證實。explicit upload未接receipt的SW缺口已修 | 共用可信receipt機制及去重身分；真Web僅1次upload_file、SHA一致、模型標記回讀一致，歷史BLOCKED保留 |
| T33 `upload_files` | 多檔 inventory／單項失敗／unexpected／dedupe；呼叫与逐檔狀態真實 | 兩份小 marker fixture，一次批次；模型讀回各 marker，核对 manifest；一項錯誤保留 partial | PASS：UT／native／真Edge-ChatGPT Web；1 action、2筆CONSUMED | 原本把Progress的上傳失敗字樣當成provider錯誤 | 錯誤詞只掃alert/dialog與附件error，不掃composer文字；READY及receipt安全條件不變；真Web帶觸發詞仍通過 |
| T34 `download_artifact` | fresh request/artifact provenance、真 fetch／保存、輸出路徑／完整 bytes；錯圖／保存失敗 | 一張新 PNG 原檔下載到 fixture，獨立檢查 magic／dimensions／bytes／hash；旧圖拒絕、頁面重繪不錯殺已登記同圖 | PASS：契約UT＋17 provenance項；新增SW優先Web IT，1下載／2 rounds／無補救，直接SUCCESS | 下載例外／驗證狀態已修；合法Progress搶先返回而漏自動下載；已有計畫時Runtime Progress缺當輪欄位 | UI idle＋fresh＋既定保存需求後優先補唯一下載；保留混合actions與停止決策；有過download則不盲目自動重試；原授權與驗證不變 |
| T35 `return_artifact` | 來源存在／hash／request identity、真 enqueue、缺檔拒絕、重複交付；QUEUED≠DELIVERED | Runtime queue IT 對照 queue record 與檔案；真Telegram文件回傳、message/file ID與原檔回讀 | PASS（API送達＋原檔回讀）；UT、IT-R、Web queue PASS；手機UI人工未確認 | queue及completion遺失身分、準備失敗留下dedup已修；原先只驗queue，已補真sendDocument | 檔案檢查後publish queue/dedup，保留身分；正式adapter真傳文件取得message/file ID，getFile回讀SHA一致；不倒寫原QUEUED歷史 |
| T36 `google_drive_upload` | OAuth／client 调用、file ID／大小／hash、錯誤分類；mock 不算真傳 | 使用已配置專用測試 folder 上傳合成檔，獨立查 metadata／必要時下載比對；失效授權／網路錯誤測試 | 契約 UT；外部 IT 未測 | 未配置時 BLOCKED_ENV，不判 Runtime bug | 不新增權限或記錄 token；配置齊全才測真交付 |
| T37 `web_search` | 真查詢入口與正常／空／錯誤回應；query 不被替換、格式與來源資料保留 | 固定 query，記錄provider實際回應與可驗來源；無結果如實回覆，不虛造命中 | PASS：UT／native真HTTP／真Web；單次查詢4筆結果與來源一致 | 舊版丟失來源URL及巢狀topics；空query也誤當已執行網路查詢 | 保留query／provider／request URL／bounded results；空query REJECTED，格式錯與網路錯分層；PASS僅provider回應，不證明來源內容正確 |
| T38 `save_session_summary` | 真寫入 summary、decisions／pending 等欄位；讀回／權限錯誤／其他工作隔離 | 合成 unique request marker，保存後獨立讀回與hash比較；不覆蓋正式摘要，保留備份／還原 | PASS：UT／native／真Web；1次保存、SHA與全部欄位讀回一致；原狀態還原PASS | 本輪未發現新production缺陷；既有denylist精準開放有效 | 補完整負向及workspace隔離UT；寫前失敗FAILED、寫後讀回失敗PARTIAL不冒充PASS；未修改此producer |

### `run_command` 必須展開的 10 個 operation

| operation | 真正操作／客觀驗收 |
|---|---|
| INSPECT | 已知 readonly stdout、cwd、exit；沒有額外 verify 時可 UNVERIFIED；不能用 mutation 假冒 |
| MUTATE | 精確寫入 fixture，exist/content/hash；exit=0 但漏產物的負向案例；不重做主操作修驗證 |
| BUILD | 合成最小專案產物與 source/config 對應；成功、真編譯失敗、錯產物路徑；不使用 HDRViewer 作首次全面測試 |
| TEST | 固定 pass/fail assertion 與實際 test 數；預期失敗不篡改實際 FAIL；無測試執行不得假 PASS |
| VERIFY | 關聯既有 action／condition，真檢查；FAIL→PASS 更新與歷史保留、不同 request 需正式接續 |
| GIT_INSPECT | 合成 Git repo 的 status／HEAD／branch 對照；沒有任何 commit／工作樹修改 |
| GIT_MUTATE | 合成 repo 的真 commit；HEAD、message、diff 客觀對照；不 commit 本執行包或開發包 |
| PROCESS | 合成短命 child；RUNNING→EXITED、PID與exit、poll/cancel；父程序啟動成功不能當 child 已完成 |
| TRANSFER | 合成 fixture 的真正傳輸結果、bytes/hash／remote receipt；發起／排隊不等於交付 |
| GENERAL | 實際 stdout／exit 如實記錄，但沒有正式驗收證據不能自動宣告任務 SUCCESS |

### 每項失敗的正式紀錄欄位

除了上表，逐子案例記錄：`case_id`、tool／operation、action 合法性確認、source revision、輸入與 ground truth、
UT-E/V/R、IT-R-E/V/R、IT-W-E/V/R、預期／實際 execution/verification/outcome、
request/task/action/verification 等身分、原始檔案或 process/queue/remote 證據、首次失敗 log、
**RCA、最終解法、修改檔案／symbol、修前重現、修後同案例重驗證、相關回歸、殘餘風險**。
RCA 必須區分 Runtime 邏輯、checker／executor、工具輸入、provider DOM、配額／環境、輸出呈現；不一律歸咎模型。
尚未實測=PENDING；缺服務／批准=BLOCKED；實測未符預期=FAIL；證據不足=UNKNOWN。預期拒絕或預期 FAIL 正確可使測試 PASS，但保留業務原始拒絕／FAIL。

### 執行順序與跨工具 IT

1. T01–T12：本機 I/O、命令、交易與驗證；先 UT 再 IT-R，再同組 IT-W。
2. T13–T25：小專案 scope／working set／sync／query／snapshot／ledger／semantic；確認基本情況不走附件。
3. T26–T31：提案→修復→freeze→執行／validate→apply→verify；每個步驟證據可回查。
4. T32–T38：附件／下載／交付／搜尋／summary；外部服務缺失獨立 BLOCKED，不擋住本機組驗證。
5. 整合 A：讀取→編輯計畫→套用→VERIFY→如實結案；故障時獨立讀取仍能執行。
6. 整合 B：分塊→crash／接續→commit→VERIFY；既有 chunk 與副作用不得盲目重播。
7. 整合 C：project index→精確查詢→DELTA／semantic更新→source版本對照；只同步 task working set。
8. 整合 D：fresh artifact→download→保存驗證→return queue；實際服務 delivery 另驗，不把 queue當達標。
9. 整合 E：紅／橙通知完整貼回＋調整需求→新 task／新契約→沿用歷史、只執行新必要 action。
10. 整合 F：五種終態、缺 outcome、數字字串正規化、Progress缺漏與合法讀取分開准入、相依 action屏障、錯誤 checker、舊 evidence拒絕、重複無進展停住而非無限 loop。

每個組別通過才推進相依整合；失敗先留原紀錄→定位 RCA→只修控制點→同案例重測→相關回歸。
所有必要子案例的 E／V／R 都有證據，才能把該工具標為該組 coverage PASS；BLOCKED與未測分支不得從分母消失。
「38 工具具名都有測試」只代表工具清單覆蓋，不是所有參數、operation、錯誤、provider與時序的 100% 保證。

### 2026-10-14 逐項實測進度（執行包）

依使用者最新要求：每一項必要 UT、IT-R、IT-W 全部通過，才進下一項；環境阻塞也先停在該項，不跳號。

| 工具 | UT | IT-R | IT-W 直接讀取 | IT-W 大檔實際上傳 | RCA／最終解法 |
|---|---|---|---|---|---|
| T01 `read_file` | PASS | PASS | PASS | PASS | Web 分支原本連小文字也改道附件；另有實際送達後 ledger 仍留 QUEUED 的缺口。改為一般 UTF-8 文字 ≤1,000,000 bytes 由 Runtime 讀取；結果過長用 LOCAL_REF，不自動上傳。更大檔案／二進位文件保留附件路徑；只有本輪匹配 request／epoch／path／SHA 的 CONSUMED 收據才能更新交付狀態，保留 staging 歷史、content_read=false、TRANSPORT_ONLY。bounded read 防止 stat 後檔案變大造成無界讀取。 |
| T02 `list_directory` | PASS | PASS | PASS（換 URL 後重測） | 不適用（不得上傳） | 缺目錄舊結果泛稱 FILESYSTEM_OPERATION_FAILED/executed=true，scope 拒絕缺共用 identity，child is_file/is_dir 可吞錯而假成功。修正 parent/child 單次 stat 與分類；成功保留文字並附第一層 entries／resolved_path；缺目錄、非目錄、ACCESS_DENIED、部分列舉分開，保留原 scope 拒絕標記與限制。原初始化失敗紀錄保留，不冒充工具執行失敗。 |
| T03 `inspect_directory` | PASS | PASS | PASS | 不適用（不得上傳） | 根目錄 exists/is_dir 布林探測會隱藏存取錯誤；改 explicit stat，FileNotFound／PermissionError 分開保留。非遞迴 2 files/2 dirs/10 bytes、遞迴 3 files/2 dirs/21 bytes 相符；真 Windows 目錄鎖定回報 PARTIAL，不假完整成功。 |
| T04 `find_file` | PASS | PASS | PASS | 不適用（不得上傳） | os.walk 吞掉 PermissionError 曾造成 SUCCEEDED/未找到。改 onerror 與完整性記錄、typed matches/root/limit/truncated/errors；明確 root scope 防止擴大。Windows junction 在下降前排除，外指／循環 junction 實測不遍歷；相同搜尋不同 action_id 不算新 evidence。 |
| T05 `write_file` | PASS | PASS | PASS | 不適用（不得上傳） | Windows write_text 曾把 LF 改成 CRLF；改原樣 newline 與有限 readback，比對 UTF-8 bytes。共同結果保留 identity，只有真讀回相符才 PASS；拒絕前未開檔、部分寫入／readback mismatch 分開，不假成功。 |
| T06 `delete_path` | PASS | PASS | PASS（使用者單次核准） | 不適用（不得上傳） | UT／本機 IT 的批准僅限隔離 fixture。正式批准過期後不延長舊批准；使用者單次核准同一未刪目標，重跑新 request，fresh manifest 相符後批准新紀錄。1 次真刪除，後續 scoped VERIFY PASS，無永久授權。 |
| T07 `begin_file_write` | PASS | PASS | PASS | 不適用 | 測試初版漏了取消後 postcondition VERIFY，且唯讀 cwd 未在測試需求中明列；補上真實 scoped VERIFY 與明確 parent。Runtime guard 未放寬，交易 OPEN 不冒充已寫檔。 |
| T08 `write_file_chunk` | PASS | PASS | PASS（獨立 staging 讀回） | 不適用 | UTF-8 bytes、去重、錯誤分塊、restart/crash 分支通過。首輪 metadata-only 為 UNKNOWN；觀察器干擾造成的 UNKNOWN／逾時保留。修 checker 不碰 manifest，讀回 14/27 bytes 後停止；新交易 Web PASS。 |
| T09 `commit_file_write` | PASS | PASS | PASS（真 predicate） | 不適用 | 修正 verify.type/contains 被忽略而 exit=0 假驗證；負向 UT 與新網頁 native file_exists/file_contains 都實測。29 bytes、UTF-8 raw readback/SHA 相符，原子／CAS／重播／crash 覆蓋通過。 |
| T10 `abort_file_write` | PASS | PASS | PASS | 不適用 | 真 staging 取消、原檔保存、其他交易不動；wrong ID／commit 後 abort／late chunk/commit 拒絕，重複 abort 不傷原檔。 |
| T11 `run_command` | PASS（十種 operation） | PASS（含 GENERAL 補救） | PASS（A/B/C/D） | 不適用 | BUILD/TEST 已合法 expected_failure，loop 卻追討未使用 verify；修為 canonical-valid 負向契約可入場，仍保留 FAILED/FAIL。GENERAL 補救漏 condition_id，真 VERIFY 後仍空轉；補完整指令綁定，未改自動 eligibility。A checker 誤讀帳本已修；D 額外一次合法唯讀条件驗證，未重做 GENERAL。 |
| T12 `aggregate_verification` | PASS | PASS（3 actions／4 rounds） | PASS（3 actions、5 檢查程序） | 不適用 | 全安全拒絕誤記 executed=true、Windows shell timeout 殘留子程序、已完成 scoped checker FAIL 卻強制 REPLAN；已修分層與實際 process_started。未知派送維持 UNKNOWN。 |
| T13 `inspect_project_scope` | PASS | PASS（1 action／2 rounds） | PASS | 不適用 | 靜默漏檔改 typed inventory error、不給可用 snapshot；否定語意不再誤判 listing；scope 拒絕正確標記。Runtime 快照強制 fresh bytes，補同 size/mtime 快取回歸。 |
| T14 `inspect_project_working_set` | PASS（filesystem／隔離 Git） | PASS（1 action／2 rounds） | PASS | 不適用 | 指定檔案快取 SHA 過期、已知 invalid 被標 UNKNOWN、缺少 request exact scope、空白路徑預檢/執行不一致；已修為 fresh selected SHA／精確 scope／一致拒絕與缺檔 PARTIAL。 |
| T15 `project_sync` | PASS（修正後） | PASS（修正後） | PASS（INDEX_ONLY／FULL_BUNDLE／DELTA） | PASS（兩種 bundle 各 1 有效 ACK） | 舊 SHA、root 擴大、Markdown receiver 誤判及拒絕被標 UNKNOWN 已修；異 receiver/session/send 仍拒絕。 |
| T16 `query_project` | PASS（修正後） | PASS（正常／mixed 各1 action／2 rounds） | PASS（七操作／mixed） | attachments_uploaded=0 | INCOMPLETE 舊分類與 PARTIAL 一律 pending 均已修；只允許可信已完成 mixed read 如實 PARTIAL 結案，不准提升 SUCCESS；root擴大拒絕。 |
| T17 `compare_project_snapshot` | PASS（修正後） | PASS（1 action／2 rounds） | PASS（1 action／2 rounds） | 未上傳 | 修同size/mtime漏改動、非法baseline/root拒絕、snapshot ID路徑越界、schema只允許dict卻Runtime支援ID的錯配。 |
| T18 `build_project_bundle` | PASS | PASS | PASS | NOT_ATTEMPTED | 真正讀回產物、重建內容及SHA；scope與輸出端點保護。 |
| T19 `build_project_delta` | PASS | PASS | PASS | NOT_ATTEMPTED | 真增改刪、空delta、刪除-only與深層junction拒絕。 |
| T20 `extract_project_dependencies` | PASS | PASS | PASS | NOT_ATTEMPTED | 限定來源patterns；不猜Gradle圖；缺讀取分PARTIAL／FAILED。 |
| T21 `inspect_project_ledger` | PASS | PASS（2 actions／2 rounds） | PASS（2 actions／2 rounds） | 不上傳 | READY／EMPTY與實際事件一致；讀取失敗FAILED、索引未確認PARTIAL，來源與歷史bytes/mtime不變。 |
| T22 `query_project_history` | PASS | PASS（6 actions／2 rounds） | PASS（6查詢） | 不上傳 | 逐項對照event ID、順序與數量；空結果／專案隔離及since/limit，500筆上限另由UT驗證。 |
| T23 `inspect_semantic_map` | PASS | PASS（3 actions／2 rounds） | PASS（3 actions／2 rounds） | 不上傳 | STALE/FRESH/MISSING為合法觀察，不強迫修圖；source/map均未改。 |
| T24 `update_semantic_map` | PASS | PASS（1 action／2 rounds） | PASS（1 action／2 rounds） | 不上傳 | 只改metadata、保存真讀回驗證；不把metadata驗證當作source修改完成。 |
| T25 `update_semantic_map_file` | PASS（3正向／10負向snapshot案例＋既有file矩陣） | PASS（根目錄，1 action／2 rounds） | PASS（根目錄，1 action／2 rounds） | 不上傳 | 真patch SHA、JSON/Markdown、ledger與來源不變讀回通過；Git空白/中文檔名另由UT驗證，截斷status/未知版本不啟用例外。 |
| T27 `propose_task_plan_file` | PASS | PASS（1 action／2 rounds） | PASS（1 action／2 rounds） | 不上傳 | Runtime-owned scope、封存hash、原始輸入SHA及來源未套用實讀一致。 |
| T32 `upload_file` | PASS | PASS（1 action／2 rounds，模擬browser＋真transaction） | PASS（使用者核准單次重測） | DELIVERED_TO_CONVERSATION | 真Web只呼叫1次upload_file，CONSUMED／SHA／隨機標記回讀一致；先前BLOCKED保留。 |
| T33 `upload_files` | PASS（多檔完整收據、去重、部分失敗、錯誤來源） | PASS（1 action／2 rounds） | PASS（兩份小txt；需求故意引用錯誤詞） | DELIVERED_TO_CONVERSATION | 1個upload_files、2筆CONSUMED；每份SHA及未放prompt的marker一致；歷史FAIL保留。 |
| T36 | PENDING | PENDING | PENDING | PENDING | 尚未接續驗證。 |
| T37 `web_search` | PASS（正常／空／格式錯／網路錯／空query） | PASS（真HTTP，1 action／2 rounds） | PASS（1 action，4筆實際結果） | 不上傳 | query/URL編碼與來源保持一致，模型最終URL清單獨立比對PASS。 |
| T38 `save_session_summary` | PASS（真保存／IO故障注入／workspace隔離） | PASS（1 action／2 rounds） | PASS（1次保存） | 不上傳 | 真讀回6欄、SHA／size／request一致；綁定workspace測試後恢復原本無摘要狀態，驗證副本保留。 |
| T26 `propose_task_plan` | PASS | PASS | PASS | 不上傳 | 1 action／2 rounds；來源不變，封存hash／scope實讀一致。 |
| T28 `repair_task_plan` | PASS | PASS | PASS | 不上傳 | 2 actions／3 rounds；原拒絕保留且正確SUPERSEDED，修復封存SUCCESS。 |
| T29 `execute_frozen_plan` | PASS（14 probes） | PASS | PASS（Edge唯讀接續結案） | 不上傳 | native 2 actions／3 rounds；Web原2 actions執行PASS、結案STALLED；後續1新VERIFY／2 rounds SUCCESS，原修改未重播。 |
| T30 `validate_edit_plan` | PASS | PASS | PASS | 不上傳 | VALID與mutation=false正確，來源／父同層sentinel未變。 |
| T31 `apply_edit_plan` | PASS（排除crash） | PASS | PASS | 不上傳 | 2026-10-15使用者明確延期crash恢復；僅一般流程列PASS。 |
| T34 `download_artifact` | PASS（priority／contract／provenance） | Web真Runtime下載PASS | PASS（最新SW優先直接結案） | PNG真保存，網路交付未測 | 最新1536×1024／1,604,820 bytes，1個Runtime download／2 rounds／零補救；舊唯讀接續證據保留。 |
| T35 `return_artifact` | PASS（含enqueue失敗重試） | PASS（1 action／2 rounds） | PASS（queue，1 action／2 rounds） | PASS：真sendDocument＋getFile SHA回讀 | 訊息3353、file_id非空、14 bytes SHA相同；手機UI人工待確認；未改原task的PARTIAL／QUEUED歷史。 |

2026-10-15 使用者調整優先級：先T30再T31；T21–T29、T32–T38仍未驗證，不能因跳號認為它們PASS。

### 2026-10-15 後續：crash延期，改做T26 → T28 → T29

本次使用adaptive-dev-router／precise-trace，逐項UT→Runtime IT→實際ChatGPT Web IT。測試入口為LOCAL_TEST送入正式RemoteAgent流程；不驗證Telegram網路傳輸。現有composer debug bypass維持原值，未宣稱驗證該保護。

- T31：使用者明確排除crash後，依已通過的一般UT／Runtime IT／Web IT列PASS；備份仍在記憶體，強制終止後不保證自動恢復。
- T26：UT涵蓋錯型別、非source檔、錯snapshot／SHA／BEFORE、父scope、偽造scope、既有plan篡改、可預測temp hardlink不被跟隨；T30與semantic-map scoped回歸PASS。Runtime native 1 action／2 rounds。Web `RR-LOCAL_TEST-02033F5D8322CDF1`／`TASK-1B2E15CA947F36865FCF`：單次propose，PLAN_FROZEN與持久化SHA／ID／Runtime-owned scope核對，probe/sibling/parent bytes未變，終態SUCCESS。
- T28：舊schema recovery UT失敗來自fixture宣告repair卻提交propose，先被正確的next_action契約拒絕；只調整fixture以測目標latch。首輪Web `RR-LOCAL_TEST-DB5CA74C70C1841D`，repair已封存但引用原拒絕＋repair時終態被判FAIL，模型如實PARTIAL。補強只在成功PLAN_FROZEN、executed=true、精確匹配原Runtime next_actions的情況標記原REJECTED/executed=false為SUPERSEDED；不改寫其歷史FAILED，不遮蔽無關失敗。UT與native同時引用兩筆確認PASS，加入無關失敗仍FAIL。Web重測 `RR-LOCAL_TEST-D72F96528B9FDFC1`／`TASK-F144E7B6171FAD2BD870`：2 actions／3 rounds，SUCCESS；原持久化receipt的superseded_by_action_id核對通過。首測全資料保留web_history。
- T29：僅執行隔離暫存fixture的故障UT。確認同scope的實際修改與檢查可PASS，普通verification=FAIL可回滾、異request/task/epoch/intent被拒；但跨回合被六欄exact equality誤拒、verifier exception未回滾、前導空白路徑被apply正規化但backup/rollback未共用，真probe留下AFTER卻rolled_back=true。沒有修改正式專案；尚未開始真Runtime loop／Web IT。交易流程修正須先確認，不納入crash journal／重啟恢復。

目前驗收通過37/38（T01–T35、T37、T38；T29經唯讀接續結案，T31依使用者要求不含crash，T34最新SW優先單次流程直接結案，T35已補實際Telegram API傳送及SHA回讀）。T37/T38已補UT、native與真Web；僅T36待驗證。手機UI仍需使用者人工確認；T35並非真Telegram自然語言入站→自動dispatcher全程單次驗證，而是接續既有真Web queue產物經正式傳送adapter補驗缺少的網路段。證據集中於`localdata/runtime_it/matrix38`，本次新增`t38_summary_it.json`及`t37_search_it.json`。這次沒有應用包Git checkpoint、push、update或sync-back。

### T38 → T37 修復與驗證完成（2026-10-16）

依使用者順序先T38再T37，執行包限定，不中斷其他工作、未更換URL／browser profile。使用Precise Trace限定producer→共用結果→action loop→正式LOCAL_TEST ingress；不驗證Telegram網路入站。

- T38：`validate_session_summary_matrix`真寫入與獨立讀回6欄／request／size／SHA；空摘要、錯list型別、超64KiB拒絕不改原檔；寫入PermissionError為FAILED且原檔不變，寫後讀回拒絕為PARTIAL/file_saved=true/verification FAIL；不同workspace及sentinel不互改。權限錯誤是本機故障注入，不冒稱真ACL-on IT。未需改production producer。native `WA-F468879B9DD1` 1 action／2 rounds。Web `RR-LOCAL_TEST-65198EB5F7CF79DD`／`TASK-74B78FED5C359D6E882D` 單次save_session_summary，真產物472 bytes、SHA `2bb8739eeb2ed05a12c04f7218bce1986f87c05efd7f3c2c3beaef0d89457616`、SUCCEEDED/PASS、task SUCCESS。因正式入口強制目前workspace binding，Web使用E:\card預設摘要；測試前確認原檔不存在，保存驗證後只移除精確匹配測試SHA的摘要，恢復原本不存在狀態，restore_status=PASS，再獨立確認。驗證產物副本保留於本項fixture，未刪其他檔案；若原檔存在則先持久備份，並只在未被其他工作改動時還原。證據`t38_summary_it.json`。
- T37 RCA：原producer只回AbstractText或RelatedTopics文字，丟掉AbstractURL/FirstURL與group Topics；無法核對來源。空query/None在網路未呼叫前就失敗，原結果仍executed=true。修成空query REJECTED/false/NOT_ATTEMPTED；保留精確query、provider、request_url、最多4筆來源text/url/title，支援巢狀Topics並限制遍歷100項；合法零結果SUCCEEDED。已收到HTTP但JSON/shape不合法為SEARCH_RESPONSE_INVALID/RESPONDED，連線錯為NETWORK_SEARCH_FAILED/FAILED；兩者都不冒稱搜尋成功。verification PASS限定PROVIDER_RESPONSE_ONLY，沒有以此證明來源內容真實或任務其他修改完成。既有DuckDuckGo Instant Answer不更換，也不宣稱是完整網頁搜尋引擎。
- T37 UT `validate_web_search_matrix`：精確中英文query及編碼、abstract URL/group Topics、合法零筆、錯shape、timeout、空query未呼叫network；空／錯誤HTTP為mock，正向native/Web是真服務。native `WA-7472E887652A` 1 action／2 rounds。Web `RR-LOCAL_TEST-76C49B4792A64CD8`／`TASK-7BC21A2FDC7BB2B226CB`：唯一一次web_search，query `Python programming language`，真回應4筆，模型回覆中的query/provider/每個來源URL再與持久化result逐項比對PASS；task COMPLETED/SUCCESS。未使用模型內建瀏覽、未另查來源、未換query或重試。證據`t37_search_it.json`。
- T38與T37修後回歸：`validate_p2_result_contract`（4 tools）、T38 matrix、T37 matrix與git diff --check通過。T36不在本次範圍，未宣稱38/38或所有外部服務皆可用。

### T27 → T32 → T33 接續紀錄（2026-10-16）

T33修復及重測完成：`web_runtime._attachment_ui_snapshot`不再掃composer文字的錯誤詞，只掃provider alert/dialog及per-file error。保留檔名唯一對應、unexpected附件、busy/ring、send enabled與穩定READY門檻，未放寬request/epoch/SHA/CONSUMED。新增`error_source`及`attachment_rejection_evidence`，只記錄來源、匹配詞及數量／busy，不輸出整份prompt。新增UT `validate_attachment_error_origin`先重現舊版失敗再修後PASS：中英失敗詞在prompt不誤拒；真alert/dialog/file error仍REJECTED；busy／send disabled／缺檔／多餘附件均不能READY。T32/T33矩陣、T01附件回歸及共用25-tool結果契約PASS。

修後native `WA-2420E671D3F7`：1 action／2 rounds PASS。真Web `RR-LOCAL_TEST-49F6B0E50DA9F701`／`TASK-143AB3232A31A1814A29`：需求刻意含「上傳失敗」「上傳限制」「upload failed」，單次upload_files上傳兩份48-byte合成txt，兩份穩定READY後提交；兩筆同request的CONSUMED receipt、SHA分別`a51b1303d43c7259fa1614808043385a3a1eccaf676eb57b5a66594b018b185e`及`6728e1ba50fa7a2bbff5dc24adc712139bc98359064c930bbadf1b5182d2642b`與實際檔案一致，模型正確讀回未放prompt的兩個隨機marker。最終SUCCEEDED／DELIVERED_TO_CONVERSATION、task COMPLETED/SUCCESS，沒有重傳或其他action。舊Web FAIL保留web_history；部分失敗／惡意身分等是本機UT，未故意在真網頁製造上傳失敗。未接續T36–T38。

T33最新驗證（下方未開始敘述屬歷史）：UT `validate_upload_matrix upload_files` PASS，補驗完整兩份收據才解鎖、同batch不同action共享去重artifact、錯epoch／SHA不借用receipt；有效檔＋不存在檔保留PARTIAL，前者送達後仍PARTIAL_DELIVERY／非terminal。native `WA-1B5F03E0C879` 1 action／2 rounds PASS，browser邊界為模擬、Runtime與transaction為真。Web `RR-LOCAL_TEST-8CC99D6EA927457B`／`TASK-3B0BDC1F527E9821C000`：僅一次upload_files，queued_count=2；第一份選取後即state=REJECTED、upload_rings=3、send_ready=false，原因上傳失敗，submit_attempted=false，未重傳、沒有CONSUMED，整項不列PASS。證據`t33_upload.json`。目前總數仍34/38。

T33 RCA：持久化Progress的completion_contract.failure包含「Runtime明確回報上傳失敗、額度或頁面失敗…」。`web_runtime._attachment_ui_snapshot`會將composer_text與alert/dialog合併掃錯誤詞；ChatGPT adapter的text來自composer.innerText，包含待送Progress文字。本機同一組READY fixture（按鈕可用、chip已完成、無alert/dialog）只改text為該失敗條件，結果REJECTED/error=上傳失敗；換普通文字則READY。此為確定的SW誤判缺陷，本次事件高度符合；未保存失敗瞬間的原始DOM，故不宣稱完全排除同時的真頁面錯誤。CUA inventory沒有暴露該外部Edge視窗，唯讀檢查無法補回原畫面。此輪依使用者「驗證T33」只補測試與紀錄，未改production classifier。建議後續區分editable prompt與真附件錯誤節點，保留真alert／檔案error及request身分、安全條件，再重測T32/T33；未擅自重傳。

最新補驗：使用者手動上傳成功後，明確要求SmartAgent再執行一次單檔上傳以觀察。`RR-LOCAL_TEST-FC56510EF11C6345`／`TASK-F2CF31211294C1774F20`，檔案`E:\card\runtime_it\matrix38\T32-885fa984d0\web_6ed41b88\probe_0.txt`。只有1次upload_file，READY visible=1／upload_rings=0，正式送出及CONSUMED receipt，SHA `94ba17e9c71d9157ad14910ec5c126765b711ae7910994a6e8a6d90b5dd30960`與本機一致；未放prompt的隨機marker由模型完整讀回，終態COMPLETED/SUCCESS，T32改列PASS，總計34/38。未重傳或修改source，先前失敗保留web_history。此次範圍只有單檔，未接續T33。上傳限制的舊判斷會掃composer文字，具備誤判可能，但尚未拿到舊畫面對應節點證明，不能把舊錯誤確診為帳號額度用盡或DOM誤判。下方BLOCKED紀錄為歷史。

- T27 RCA：檔案版原先取tool payload的task_epoch才傳request_scope，通常造成未綁定；workspace也未啟用request-scoped限制。改成與inline一致，使用Runtime-owned scope、request-scoped workspace，並要求source為授權的絕對／非glob路徑。UT先重現缺request_scope，修後PASS；另驗父scope、relative/glob/ADS、缺檔、錯SHA、壞JSON與schema、未改source。T26回歸PASS。原生loop `WA-BEAFF4967074`、真Web `RR-LOCAL_TEST-982B0F0F6263A99B`／`TASK-0949E4CD474D893FAD8E` 均1 action／2 rounds。封存hash/ID/scope、輸入SHA、probe/sibling/parent bytes實讀一致；獨立review無material finding。成功fixture的輸入放在`.agents`；未宣稱根目錄plan JSON的self-snapshot情境也已驗證。
- T32 Runtime RCA：原先只有read_file能以真CONSUMED receipt更新送達狀態，explicit upload_file(s)仍停QUEUED。沿用相同request/epoch、開始時間、staged path、SHA、唯一conversation、完整artifact集合的receipt驗證，擴展到upload_file(s)；保存原QUEUED歷史及request/task/action identity。PARTIAL不能升格整體成功。UT及native loop PASS；native只模擬browser邊界，transaction/persistence與Runtime loop是真實，不冒充網路IT。T01回歸PASS。兩次早期native失敗來自測試器把transaction寫在package root而非production source_root，修正測試器後2 rounds通過，沒有網路上傳。
- T32真Web `RR-LOCAL_TEST-E802CC610D7C2639`／`TASK-383B4CBDA44F7096F26C`：1個upload_file action，只有小合成txt；第一行隨機標記未放prompt。附件階段得到`attachment_rejected: 上傳限制`，submit_attempted=false；沒有CONSUMED、沒有標記回讀，結果BLOCKED而不是PASS。task歷史FAILED、action歷史QUEUED均不竄改。未自動重送；source_root仍有現存未完成附件transaction，不擅自清除。
- T33：依一項通過才進下一項，尚未執行UT／native／Web；已備妥共用測試器但不列驗證通過。須待使用者解除／確認provider上傳限制後，先補T32成功Web，再送T33兩份小附件。未變更URL或browser profile。
- T32獨立review補充：同批兩個action傳相同內容時，第二個dedup-only結果原本沒有artifact，無法用第一份的真receipt收斂。將pending SHA ledger改為digest→StagedAttachment，每個action結果都保留所引用的原staged identity，但queued_count仍是實際新增數；batch取走／新run時照常清空，不跨batch猜測送達。新增兩個upload_file action、單份實體附件、同一CONSUMED receipt分別解鎖的UT通過，錯epoch仍不解鎖。修後T32 native `WA-FC285868D6E2`（1 action／2 rounds）、T01 regression、共用25-tool結果契約回歸通過。PARTIAL、多檔與其餘T33場景仍待該項驗證，不因共用程式已擴展就列PASS。

### T25 根目錄補丁修復完成（2026-10-16，取代前次PARTIAL）

本次使用者重新要求修復與驗證T25，範圍僅執行包，不接續T27。開始時保留前次T23/T24/T25未提交修改。沒有其他active worker，使用既有Edge/ChatGPT，不換URL、登入profile或重啟其他Agent。

- 修法：僅在update_semantic_map_file入口，對實際讀取的單一.json補丁建立`patch_snapshot_evidence`。目前snapshot必須為可完整重算的v5、Git status未截斷；匯入檔在inventory的SHA必須等於讀取bytes的SHA。扣掉唯一補丁record及它精確相符的Git `??`狀態後，重算的snapshot ID必須等於原始base_snapshot_id。符合才把副本patch綁定目前完整snapshot，交回既有update_semantic_map再做fresh檢查及保存讀回。沒有全域排除JSON、沒有跳過其他來源SHA或改動source。
- Git空白及中文檔名的porcelain quoting採已知檔名的精確encoding比對；不以模糊substring刪除其他狀態。不能證明只差該補丁時仍回原本SNAPSHOT_MISMATCH；未知schema及200行以上可能截斷的status不啟用例外。
- 複審指出最後前置檢查後仍可能有外部變動：補上保存後完整snapshot ID與接受snapshot比較。外部b.py或Git狀態改變時，即使a.py scope仍FRESH，也回PARTIAL/SOURCE_CHANGED_DURING_UPDATE、verification_status=FAIL、mutation_applied=true，不能宣告UPDATED/PASS或假裝未寫入／已回滾；既有失敗latch阻擋原plan恢復。這是檢查時點的證據，不代表能永久阻止外部程序後續修改。
- 新UT `validate_semantic_patch_snapshot`：3正向（無Git、Git空白名、Git中文名）；10負向（來源同size/mtime改動、另加JSON、錯base、錯SHA、proof後／初始檢查後兩種race、已有patch改寫、Git HEAD變更、status截斷及未知版本）。早期拒絕不改map；late-race明確保留實際部分寫入，不冒稱回滾。這些Git commits只發生在全新測試fixture，不是應用包repo。
- 真Runtime loop：`WA-DF715E9D240B`，根目錄semantic_patch.json，1 action／2 rounds PASS。第一輪Web `RR-LOCAL_TEST-595707F5468B827D`也PASS，歷史保留。
- 最後Web：`RR-LOCAL_TEST-6A908A8A02FEE61B` / `TASK-042B9EBBB878F53C9DDA`，1 action／2 rounds SUCCESS，無protocol_rejected。實際根目錄JSON輸入SHA=`803f8c24802f902fd3218d9b6fa41c282cb3997145f0bbccee2b4796085a64e8`，原base=`a741ba9b78bbd92366015e90bf6c30b10054281fecaa8e53c554f057e27d3a1a`，正常保存snapshot=`7a7f800421e136ebe15b66a622889b46c0c96e98729c7f79b5dd2b8514332295`。
- 獨立audit不重播action：對照保存JSON／Markdown、真ledger event ID和revision／snapshot、patch原始base及SHA；a.py/b.py、補丁及非目標檔案bytes/mtime均不變，global語意及b.py紀錄保留。`t23_t25_semantic_it.json`最新web的independent_disk_audit=PASS。既有T25檔案矩陣、T23/T24、plan-scope、ledger、P1與evidence-to-action回歸PASS。真Web是LOCAL_TEST正式ingress，不宣稱Telegram網路入站IT。

### T23 / T24 / T25 semantic map 前次驗證（2026-10-16；T25當時PARTIAL，已由上節取代）

執行包基底HEAD為`d1643d1`，開始時工作樹乾淨。按T23→T24→T25，逐項UT→真Runtime loop→Edge/ChatGPT正式LOCAL_TEST ingress。只操作新fixture，不上傳附件、不更換URL／browser profile、不操作來源Git，也沒有sync-back。

- T23：UT實際改a.py為同size且還原mtime，舊邏輯仍FRESH；現在每次inspect/update核對fresh bytes。不存在的required path不再被部分忽略，損壞／無權讀取map不再當MISSING；透過真工具邊界回FAILED/executed=true/possible_partial_execution=false，scope不合法則REJECTED。指定子目錄不可擴大到父目錄。
- T23 native首測3個唯讀action都成功，卻在結案被semantic_map_requires_bounded_source_evidence擋住；RCA是純觀察MISSING/STALE也強迫進補救。現在只有存在原plan resume_action才強制補救；既有plan補救回歸PASS。Web `RR-LOCAL_TEST-082413674310BB16` / `TASK-A3C333EE20F9B3121EFC`，3 actions／2 rounds SUCCESS，a=STALE、b=FRESH、未建立map專案=MISSING，全部和磁碟核對，來源及map bytes/mtime未變。
- T24：局部a.py patch原本刪掉b.py紀錄，已修為保留範圍外rows，且保留既有global project_summary/flows；結果明列preserved_project_fields，global更新需使用完整PROJECT scope，不用局部修復覆蓋。重複path、錯snapshot/SHA、非法path在寫入前拒絕，原map不變。保存後真讀回JSON及Markdown相等才提供verification_status=PASS與verification_evidence；缺少證據不得terminal_eligible。
- T24首個native遇VERIFY_REQUIRED，原因是工具未提供保存驗證證據，不是要省略驗證。加入真讀回證據後native 1 action／2 rounds，Web `RR-LOCAL_TEST-2490AE94018EA967` / `TASK-BE30E3414F3E5C8C3D26`，1 action／2 rounds SUCCESS。測試器獨立讀JSON核對兩筆rows、a的更新及b保留，來源未變。複審追加非空global語意保留、讀回FAIL不能被後來FRESH漂白：新persistence failure latch保留客觀阻塞，不能恢復原plan；既有僅coverage不足的PARTIAL仍維持原補救流程。沒有加入crash或多檔journal交易恢復。
- T24最終Web回歸 `RR-LOCAL_TEST-FA5E0B7B7F942E3B` / `TASK-92DFE88195918178F922`：1 action／2 rounds SUCCESS，fixture原有非空project_summary/flows；雖然scoped patch帶不同summary與空flows，磁碟讀回確認原global語意與b.py均保留，只更新a.py責任描述。來源與patch輸入摘要未變。
- T25：相對JSON路徑原本以程序cwd解析，誤回PATH_OUTSIDE_WORKSPACE；改用canonical workspace-relative、ADS/越界拒絕及max_bytes+1有界讀取。UT覆蓋缺檔、錯hash、hash後來源改動、invalid JSON、容量上限及ADS。支援路徑內native 1 action／2 rounds，Web `RR-LOCAL_TEST-38E6319DC47729C4` / `TASK-F68E29544F3BB42246E7`，1 action／2 rounds SUCCESS；patch位於.agents，SHA、實際保存revision、b紀錄及來源不變核對通過。
- **T25整體仍PARTIAL**：追加真UT把相同patch放workspace根目錄`semantic_patch.json`，新JSON本身改變專案inventory，仍回SNAPSHOT_MISMATCH。這不是檔案沒讀到、不是模型漏欄位，也不能靠忽略任意source差異修掉。目前未修改artifact/snapshot邊界，待使用者確認擴大範圍；不得把.agents案例通過宣稱成任意workspace位置都已通過。
- UT `validate_semantic_map_matrix`三組、`validate_semantic_map_plan_scope`、`validate_project_ledger`、`validate_p1_result_contract`、`validate_result_evidence_to_action_recovery` PASS。機器證據`localdata/runtime_it/matrix38/t23_t25_semantic_it.json`保留native失敗歷史及Web結果、action原始回覆、檔案前後摘要；後續native含來源程式SHA。正向網頁通過不代表未執行的崩潰／磁碟故障或所有concurrency場景都已驗證。

### T21 / T22 Ledger 驗證與修正（2026-10-16）

範圍：執行包內修改。依序UT→真Runtime loop→Edge/ChatGPT正式LOCAL_TEST ingress；沒有以模擬模型的通過代替網頁IT，也沒有驗證Telegram網路入站。測試fixture由真INDEX_ONLY capsule及query_project建立來源上下文；Web只執行指定Ledger讀取，不上傳、不修改來源或歷史。

- T21首測 `RR-LOCAL_TEST-3874FB9089752E73` / `TASK-C45F53330C4764998126`：兩筆合法inspect在執行前遭ADS拒絕。RCA是需求中JSON的workspace字串與下一欄limit冒號被合併，並非模型action下錯。只針對完整JSON字串值加入詞法邊界與解碼；未放寬canonical ADS規則、未從saved-plan描述擴充授權。既有偽裝ADS及JSON內真ADS／unicode冒號仍拒絕。不是宣稱任意JSON escape形式的自然語言路徑抽取都已涵蓋。
- T21修後網頁 `RR-LOCAL_TEST-0FD28CC063AF25B0`及`RR-LOCAL_TEST-FD48DA3C25B4D9B5`通過；最終一致性修正版回歸 `RR-LOCAL_TEST-835F0D421512DA15` / `TASK-BB12A989AE8EEBC36E5E`：2 actions／2 planner rounds、無協議補救，READY/1與EMPTY/0、event ID及snapshot和磁碟一致。所有來源／歷史檔bytes與mtime不變。
- Ledger讀取原本吞掉OSError/UnicodeError，可將無法讀取冒充空歷史。現在只把FileNotFound視為沒有ledger；缺少專案拒絕；真正讀取器失敗明確FAILED/executed=true，不虛構可能寫入，也不把參數前置檢查誤記成已讀。strict索引讀取檢查schema與必要欄位，避免KeyError/TypeError逸出。
- T21一致性補強：同一次history讀取供count/latest/recent使用，避免第二次讀取混入後來新增事件。寫入方先發布ledger再發布index，有限重讀仍未一致時回PARTIAL、index_consistency=MISMATCH_OR_UPDATE_IN_PROGRESS、metadata_source=LEDGER_EVENTS；摘要依實讀事件建立，不認定損壞、不改磁碟、不允許terminal_eligible。索引遺失但有事件也不冒充EMPTY。UT涵蓋第二讀收斂及持續超過重讀窗口、零count殘留latest、損壞JSON／欄位與權限失敗。
- T22網頁 `RR-LOCAL_TEST-7A02E780B5D4954B` / `TASK-4D1CE919893CDB8BD49B`：6筆query_project_history真執行，逐一核對event_types、snapshot、大小寫路徑、since＋limit、空結果及另一專案無歷史。UT另核對since邊界、反斜線、組合filter、limit=0下限及505事件實際截為500、事件順序與無跨專案污染。
- T22最後回歸 `RR-LOCAL_TEST-252B791F589C4125` / `TASK-7D59D571DA64191EA52F`：6 actions／2 rounds、無協議拒絕，SUCCESS；結果數量為[2,2,2,1,0,0]，所有event ID及順序逐項和真ledger核對。T21/T22最終Web的before/after hash＋mtime、request/task身分、action原始結果及來源程式SHA均已寫入機器證據。
- 原生UT：`validate_project_ledger_matrix`、`validate_project_ledger`、`validate_p1_result_contract`、`validate_authorized_path_extraction`、`validate_chunk_scope_result_contract`、`validate_failed_matrix_regressions` PASS。Runtime IT T21 2 actions／2 rounds、T22 6 actions／2 rounds。唯讀獨立複審發現的發布時差與schema-valid索引欄位損壞已補測修正。首測失敗與後續每次Web重測保留在`localdata/runtime_it/matrix38/t21_t22_ledger_it.json`的web_history；不改寫失敗歷史。

### T29 核准後實作及驗證（2026-10-15）

使用者核准身分綁定、統一路徑及一般例外回滾，明確不加入crash恢復。

1. `task_plan.py`：以Runtime-owned request/task/epoch/intent綁定；只允許非倒退的continuation_seq，仍拒絕外來request或epoch，不開放任意跨request。plan_id格式、hash與lexical reparse檢查保留。validator輸出canonical files，backup/apply/rollback共用相同target。
2. 執行前逐target授權；執行後verification FAIL或可捕捉Exception皆進回滾。逆序原子還原、bytes實讀、清理本次新建空目錄。只還原已知交易AFTER，不覆寫未知外部變更；無法確認或還原失敗回ROLLBACK_INCOMPLETE，外層UNKNOWN/executed=true，不能冒稱未執行或已還原。完成receipt寫入失敗也有回滾。
3. `tools.py` 的execute_frozen_plan改用request-scoped workspace、Runtime自己持有的scope及SecurityContext逐target callback；`tool_result_policy.py`分清未執行拒絕、已執行失敗及不確定回滾。
4. 無post-change semantic rows時不空寫semantic map。source任務用fresh hash更新；保留舊writer前先拒絕任何lexical temp entry（含exists=False的dangling link）。獨立複審發現此temp問題後已修並補UT。

驗證：`WebAgent.tests.validate_frozen_plan_matrix` 14 probes全PASS，含跨輪與禁止replay、異身分／倒退回合拒絕、驗證FAIL及Exception回滾、前導空白路徑、readonly前置拒絕、restore denial如實UNKNOWN、外部bytes不覆蓋、新檔／空dir還原、完成receipt拒絕、source semantic更新與lexical temp拒絕。T26／T28／T30／T31／semantic scope／dependency與P1結果契約（25 tools）回歸PASS。native formal loop 2 actions／3 rounds PASS。

Web `RR-LOCAL_TEST-27D1F138C6CAAC32`／`TASK-5E19A033ED7CDAEDE14F`：第一輪propose PLAN_FROZEN；第二輪execute_frozen_plan真的APPLIED probe.txt，readback 6 bytes／SHA256 `f72c713b7b432dd18949ca10f1be6dbcf493479945653609d1c715b2d8d74356`，內建驗證process_started=true、exit_code=0、PASS；plan `PLAN-420BC9C798AA09CEC5F6`已持久化COMPLETED/PASS。測試器獨立實讀相同bytes，sibling及parent sentinel不變。

第三輪最終回覆發生WEB_ASSISTANT_START_STALLED，整個task成INTERRUPTED。因此目前T29總PARTIAL，不因前兩輪工具成功而冒稱網頁整路PASS，也沒有重做已完成修改。需使用者提供可用對話URL後補驗結案；不能僅憑timeout斷言頁面已滿或模型沒有回覆。仍使用既有composer debug bypass，不驗Telegram網路交付；沒有持久交易／強制終止後恢復保證。

#### T29 Edge補驗結案（2026-10-15，取代上段當時的PARTIAL結論）

- 沿用原task `TASK-5E19A033ED7CDAEDE14F` 的成功執行紀錄；前次取消的接續任務已依實際terminal state經`TaskContinuationStore.finish`釋放殘留lease，未手動改寫原執行結果。
- 初次補驗 `RR-LOCAL_TEST-479BF42848648BB7` 被接續guard誤拒；局部修後 `RR-LOCAL_TEST-FED81937E654AE96` 又被協議入口以`empty_command`拒絕。RCA：executor已接受`verify.type=file_exists/file_contains`，兩個前置層卻仍只看`action`。這兩次均停止自有測試並保留FAIL/CANCELLED歷史，未重播原修改。
- 最終修法：`task_continuation.py`與`smartagent_protocol.py`以相同action/type語意判定；衝突、未知及非字串kind拒絕，命令驗證仍走原安全檢查，FOLLOWUP_VERIFY仍限制可證明唯讀命令。新增alias admission及實際predicate正負向測試、衝突與mutation拒絕測試。`validate_task_continuation`、`validate_matrix38_verification_aliases` PASS，獨立只讀複審無具體問題。
- 修後正式LOCAL_TEST入口→Edge ChatGPT→Runtime：`RR-LOCAL_TEST-193121A8CCB743AA`／`TASK-1624CF0A6A60FABB3E25`，1個新的`run_command VERIFY`／2個planner rounds；使用type別名、關聯原`t29-execute-04`，exit_code=0、兩項predicate PASS，最後COMPLETED／SUCCESS。無需協議補救回合。
- 測試器獨立核對probe.txt、同層sibling、父層outside與原plan receipt的bytes及mtime全不變，證明沒有重做原修改。原task保持原INTERRUPTED歷史，不把後續成功倒寫成原單次直通。
- T29按「實際執行＋唯讀接續結案」範圍列PASS；不涵蓋crash恢復、Telegram網路端交付，也不涵蓋既有unsafe composer debug bypass的安全性。完整機器證據仍在`t29_execute_frozen.json`及`web_completion_history`。

### T34／T35 執行包驗證與修正（2026-10-15）

#### T34 後續：SW優先下載（2026-10-16完成複核）

- 根因已定位：UI confirmed idle後，只要模型帶合法`turn_commit`，舊分支便先返回模型文字；既有圖片橋接只在沒有matching commit時才執行，導致圖片＋Progress-only等待額外補問。
- 現在預設優先：ChatGPT本輪fresh圖片已確認、使用者已有明確保存計畫及輸出位置，且回覆為僅`report_progress(CONTINUE)+turn_commit`時，Runtime使用自己持有的完整Progress與一個download_artifact進入原協議准入、路徑安全、action ledger與executor。模型非關鍵顯示欄位缺漏不阻擋這個既定保存操作；這不是接受不完整模型Progress，也不代表成功已成立。只有圖片的舊橋接保留。
- 模型同輪若有其他operational actions、final_response、COMPLETE或INTERRUPT，不覆寫、不刪除，仍交原流程。新優先入口不套用Gemini。下載前仍須通過UI inactive、idle grace及commit穩定；返回前再次確認取消，不清除已觀察到的取消。
- 自動download ID由request與既定output決定，不隨round改變；本request已有任何download執行紀錄後，自動交付關閉，保留失敗證據供模型明確規劃，不能因原圖仍掛在頁面就跨輪盲目重試。不加入crash恢復或無限重試。
- 複審重現已有Progress計畫分支缺少decision/outcome/decision_reason/evidence_refs，造成`progress_patch_missing`；已補齊當輪欄位及保留原completion_contract，新增「先接受計畫、後續回合出圖」ACK准入測試，RED→GREEN。既有起始測試fixture缺adapter初始化而失敗，僅stub掉與圖片起始無關的transport-error檢查，未放寬產品檢查。
- UT：`validate_image_delivery_priority`通過實際completion loop＋ACK准入；含active→idle→stable、Progress缺欄、混合actions原樣保留、Gemini不改寫、取消、無fresh／無保存意圖／無output、failed-download停止自動重播、跨輪穩定ID、越界在provider呼叫前REJECTED。圖片起始、下載完整性、原P0契約、17項provenance回歸PASS。修後獨立複審無其他具體問題。
- 真Web IT：`RR-LOCAL_TEST-22F4810AAFC3233A`／`TASK-AC8D76D0D581AA9CB4D9`，新優先入口已在對應時間窗的日誌觀察到；1個`RUNTIME-IMAGE-DOWNLOAD-1B0CB7273923196A3DD2`、2 rounds、0次protocol_rejected，直接COMPLETED/SUCCESS，沒有唯讀接續或重抓。PNG1536×1024／1,604,820 bytes，實讀SHA256=`cf9c5ab327be93b42414d6946fcd984d20d0cfb31b7aeddcceea3f6ce6bc472c`與Runtime證據相同，同層sentinel未變。中斷恢復後只稽核既存task／日誌／bytes，沒有重送。證據`t34_priority_it.json`包含`audit`。
- 未加入模型自行改寫正在執行的下載腳本。若未來要模型修補，應另走隔離候選修補、測試及明確授權，不讓下載失敗直接變成修改Runtime的權限。當時T35送達仍未驗證，後續結果見下段「T35 真Telegram送達補驗」。

#### T34 下載與本機保存

- 正式LOCAL_TEST→Edge→Runtime：`RR-LOCAL_TEST-E845A4B736889118`／`TASK-016F71E60B6A36240C00`。一張全新網頁圖片已被request-scoped ledger證明，原`download_artifact`（`t34-download-02`）只執行一次，`staged_png_copy`真正保存1536×1024 PNG原檔、1,348,432 bytes。獨立實讀PNG magic／IHDR尺寸及SHA256=`8b550765a0151b32bdcded2c9f1006e722896a9d554009742e78ea8557c1e3c3`，與下載證據一致。artifact_id=`ART-0127BCAFAB2FBD3F890DB46F`。
- 本次新增UT先重現：manager已被呼叫卻因例外落入舊文字結果，外層誤標executed=false。另有保存後hash／size讀取例外逃出相同保護範圍。修為整個下載及保存核對受同一例外邊界保護；保留原錯誤及真attempted，目標bytes改動回PARTIAL，不確定／目錄輸出回UNKNOWN；不把未知檔案當完整保存，也不queue。
- 已成功保存的檔案原本沒有共用verification_status，終態要求額外VERIFY。現在只在實際hash及size都與producer證據相符時回`verification_status=PASS`、`verification_scope=LOCAL_FILE_INTEGRITY`；缺hash／size不升PASS，錯hash／size拒絕，不代表Telegram送達。
- 原Web第1輪只有Progress，第2輪補action缺base，第3輪才download；其後補驗證到第6輪PASS，第7輪測試器停止已知補救迴圈，保留CANCELLED，不把原單次流程冒稱直通。Progress／自動圖片交付提示仍有額外補救成本，本次未重構該流程。
- 唯讀接續`RR-LOCAL_TEST-7F98B5F505F834B7`／`TASK-277D124061714FA0831F`，1新VERIFY／2 rounds、COMPLETED/SUCCESS。獨立比較PNG及sibling的hash與mtime均不變，沒有重新生成或下載。完成通知仍會因提及Telegram而顯示未產生outbound的提示（本次本來就不要求網路送達）；不將此提示當PNG保存失敗。
- `validate_download_artifact_matrix`、原P0 artifact contract及17項page provenance回歸PASS。包括producer錯誤、錯hash／size、缺檔、保存中途寫入、目錄不確定、保存後檔案消失；獨立複審提出的post-save stat例外已修且重測PASS。證據`t34_download_it.json`保留原Web與唯讀followup，不覆寫原失敗歷史。

#### T35 本機回傳佇列（不測Telegram網路端）

- 新UT重現3個失敗：queue record遺失request/task/artifact identity；completion revalidation丟棄身分；來源hash後消失、stat失敗時已寫入dedup set，重試假報QUEUED但queue實際為空。
- `WebAgent/tool_context.py`：檔案檢查及record準備全部完成後才append並publish去重；使用cached size，publish後不再做可能失敗的檔案stat。記錄request_id／task_id／artifact_id（內容SHA）。`RemoteAgent/telegram_artifacts.py`在路徑／hash重新驗證後保留這些optional欄位；檔案變更仍拒絕，不把queue當送達。
- UT缺檔／越界拒絕、同request重複只一筆queue、queue後hash變更拒絕、hash後消失再重試可真正enqueue、completion身分保留PASS。原P0 artifact與plan-only Telegram completion回歸PASS。
- IT-R真Runtime loop：1 action／2 rounds PASS。首兩次測試器漏更新current_step導致結案拒絕，已修測試器，沒有修改產品Progress規則來遷就測試。
- Web `RR-LOCAL_TEST-8DB7057D364CFE16`／`TASK-D199967CC8A46ACF2D09`：1 return_artifact／2 rounds，外層QUEUED／executed=true，delivery_status=NOT_DELIVERED，非terminal_eligible。source bytes及mtime不變，真正的queue entry、completion及持久化outbound_artifacts三處request/task/artifact/SHA一致。
- 如預期COMPLETED/PARTIAL、橘燈「部分完成／結果未確認」，沒有假報DELIVERED或虛構message/file ID。此處PARTIAL是網路交付未確認，不是本機queue驗證失敗。Telegram實際網路送達依使用者原先範圍排除，仍NOT_TESTED。證據`t35_return_it.json`。獨立複審修後無具體問題。

#### T35 真Telegram送達補驗（2026-10-16）

- 使用者明確核准實際傳送後，唯一已配對對話收到正式`TelegramDeliveryAdapter`→真`TelegramBotClient.send_document`送出的測試文件；不修改Bot token、配對、listener或getUpdates offset。不再執行模型或原return_artifact，不改寫原LOCAL_TEST任務的PARTIAL／QUEUED紀錄。
- 來源為既有真Web task `TASK-D199967CC8A46ACF2D09`持久化outbound中的`queue_probe.txt`，固定無敏感資料內容`T35_QUEUE_ONLY`、14 bytes。正式completion路徑再次核對workspace與SHA，adapter只呼叫一次sendDocument，另有一則狀態文字通知；非sendPhoto，不經圖片壓縮。
- Bot API成功回應：marker=`T35_TELEGRAM_IT_31FB66BA93`、document message_id=`3353`，file_id／file_unique_id非空，回應chat與唯一配對目的地一致，file_name及file_size正確。測試器只保存必要回應，不列印token或chat/user ID。
- 由該真file_id呼叫getFile並下載回隔離fixture，實際14 bytes、SHA256=`cdb9168616c96a788e88c27c404528707bbbe7c4eb68cbeeac3684359be707d3`與原檔完全相同；原檔hash及mtime未變。adapter回delivered=true、artifact_count=1，證明不是只有QUEUED。
- 證據`localdata/runtime_it/matrix38/t35_telegram_delivery_it.json`：status=PASS、delivery_status=DELIVERED、source_unchanged=true。手機對話窗由使用者人工查看尚未觀察（NOT_OBSERVED），不冒稱已看過手機畫面；沒有測自動dispatcher/重新啟動復原或真Telegram自然語言入站的單次全程。原先queue-only測試限制仍保留作歷史。
- 驗證腳本先記錄network_started，重跑預設拒絕重傳；未知傳送結果不盲目resend，只有已有document receipt時才允許`--audit`讀回。此次沒有逾時、重試或重複文件。傳送與回讀結果已成功，不需修改產品程式碼。

本次只改執行包，沒有sync-back、update、Git checkpoint或push。仍使用既有composer debug bypass；未將它列為通過安全驗證。
T30 Web `RR-LOCAL_TEST-2A39F022F3ADD8DD`／`TASK-5C57E249F61E2821500F` PASS：1 action／2 rounds，VALID、executed=true、mutation_applied=false；目標／同層／父層sentinel獨立bytes比對不變。模型如實區分計畫可用與已修改。累計完整通過21項（T01–T20+T30），非T01–T30全部通過。
T31 Web首測 `RR-LOCAL_TEST-487E4FA053EA40E6`：apply成功，但合法linked VERIFY帶depends_on，被要求原action先PASS而阻擋；最終模型修復後完成，測試器另因讀错continuation actions/results map發生KeyError（不冒稱產品失敗）。原試驗history保留，不重做原apply。dependency修法僅讓自己的唯讀VERIFY（主command與verify steps均為已證實readonly）依賴已SUCCEEDED/executedtrue的原action，不需預先PASS；failed/UNKNOWN/foreign/stale及其他消費者不放寬。resource ledger的相對路徑改用plan workspace或命令cwd，並與executor共用canonical規則，避免把實際子專案檔記成E:\card同名檔。UT覆蓋前導空白路徑、普通dependency仍拒絕、主／verify命令mutation拒絕；native2actions／3rounds與resource_paths全比對PASS。
T31 Web修後 `RR-LOCAL_TEST-4BD425918DA7DD60`／`TASK-DE9F28C278E8A39D9262`：2 actions／3 planner rounds，apply只一次、linked VERIFY帶depends_on正常執行PASS，來源6bytes／SHA實讀相符、父同層sentinel不變，continuation results原action effective PASS且兩筆resource_paths皆為真fixture/probe。整體一般流程PASS；crash未實作，因此T31總PARTIAL。仍使用現有composer debug bypass，並未驗證Telegram網路端或正式任務遭強制關閉。
T31 RCA／局部修法：write_text換行轉换使LF內容與postSHA不符；hardlink寫入動到外部檔；回滾未移除新建目錄，rollback例外逸出錯標未執行。改成encode精確bytes、同dir獨占temp+原子替換，create使用獨占publish不覆蓋並行檔，成功publish才記touched；寫前bytes重檢不回滾他人未被本action寫過的更新；所有target在第一寫前做SecurityContext require_write。真正readback及postSHA比對、逆序恢復+確認bytes、本次新空dir清理；失敗回ROLLBACK_INCOMPLETE／UNKNOWN／executed=true。UT含LF/CRLF、真hardlink、multi-file/newdir rollback、restoredenial及rollback時PathSecurityError、create碰撞／寫前改動。native2actions／3rounds，apply只一次，linked VERIFY PASS並更新原有效狀態；Get-FileHash不可用的首測FAIL已留errors，改Get-Content與file predicates（測試器仍獨立SHA）。Web `RR-LOCAL_TEST-487E4FA053EA40E6`／`TASK-E2B88F1A018B3BC3D090`驗證中，證據`localdata/runtime_it/matrix38/t31_apply_edit.json`。備份仍在RAM，沒有crash恢復PASS宣稱；使用者要求先提出持久交易實作範圍，見`doc/edit_plan_crash_recovery_plan.md`。
T30 UT／IT-R PASS，實際Web驗證中。RCA：錯BEFORE仍VALID，duplicates與ADS／根路徑沒有一致拒絕；SHA／snapshot／path拒絕只存在business層、共用結果卻UNKNOWN/executed=false。修正fresh SHA snapshot、相對plan path authorize與lexical reparse拒絕、duplicate target與唯一BEFORE、非法encoding回INVALID_PLAN／decode或讀取失敗回READ_FAILED。共用結果區分「驗證已執行」與valid=false，mutation_applied始終false，不當成修改成功。UT每次比對來源bytes；native唯一validation 1 action／2 rounds，目標、同層sentinel、父層sentinel不變。P1、semantic局部plan、evidence-to-action與blocker回歸PASS。Web request `RR-LOCAL_TEST-2A39F022F3ADD8DD`／`TASK-5C57E249F61E2821500F`；證據`localdata/runtime_it/matrix38/t30_validate_edit.json`，UT `validate_edit_plan_matrix`。

T01 自動證據：`localdata/runtime_it/matrix38/t01_read_file.json`。
測試入口：`source/WebAgent/tests/run_matrix38_t01_read_file_it.py`。
小檔 IT-W：request `RR-LOCAL_TEST-930D548CBDCF3FE6`，task `TASK-1B02050CBD864B138E6F`，
確認兩個真實 read_file、42 bytes 原文／0 bytes 空檔；沒有附件上傳。
大檔 IT-W 使用 1,139,070 bytes 合成文字，必須同時驗證 ready／實際送出與模型讀到首尾唯一標記；僅 staging/hash 相等不足以通過。
首次大檔 IT-W：`RR-LOCAL_TEST-F76C42CE7661F732`，附件已讀到但交付狀態未更新，測試 FAIL／業務 PARTIAL。
修後全新標記重測：`RR-LOCAL_TEST-F18A8EFC2A6196E8`，`TASK-95557F6D823CA107CFBF`，
1 次 read_file、3 輪 planner，交付收據 CONSUMED、SHA／bytes 相等、首尾標記相等、終態 SUCCESS。
T01 相關回歸：P0 read/command、P0 artifact、flexible Progress admission、Progress terminal lifecycle 均 PASS。
T02 證據：`localdata/runtime_it/matrix38/t02_list_directory.json`，
入口 `source/WebAgent/tests/run_matrix38_t02_list_directory_it.py`。
IT-R 真實目錄讀取與 Windows exclusive directory handle 權限拒絕已通過，未修改 ACL。
T02 複審修正：子檔使用一次 explicit stat，避免 is_file/is_dir 吞掉存取錯誤或狀態互相矛盾；新增子檔消失／拒絕的 PARTIAL UT。
T02 回歸曾發現 common envelope 的 action_id 讓相同目錄被當新證據，防空轉失效。
`progress_recovery.read_fact_digest` 改為比較目錄結果事實，不比較 request/action 身分；新增同內容不同 ID／真實檔案大小改變 UT。
修後 flexible Progress admission 回歸確認第 3 輪停止同內容重讀，scope hardening PASS。
T02 網頁 IT 兩次初始化失敗：`RR-LOCAL_TEST-F03A57E257A99973`、`RR-LOCAL_TEST-D5B49D162FDC9076`。
首次 readiness 回讀僅 `[AGENT_PROTOCOL_READY]`（22 chars），沒有 matching JSON；第二次仍 readiness 不符。
兩次 actual_action_count=0；分類為 BOOTSTRAP_BEFORE_ACTION，不是目錄工具執行失敗。
不略過 session/hash/session_id 驗證；需先確認完整頁面回覆與初始化抽取，再重做 T02 IT-W。
2026-10-14 使用者更換 URL 後，T02 重測 PASS：`RR-LOCAL_TEST-414631C80C7EF4AB`、`TASK-75CA5AAFDBAB870D0A2E`。
實際 URL 尾碼 `6acf1f4a-c078-83e8-a42b-0f148e95e8c5`；初始化成功，2 個 list_directory action、2 輪 planner、worker 約 88.8 秒。
列舉目錄 4 項（alpha.txt 9 bytes、中文.txt 13 bytes、empty／sub 為目錄）；第二目錄 0 項。獨立 stat 比對名称、種類、bytes、requested/resolved path 均相等。
未遞迴、未讀取檔案內容、無附件上傳／寫入／刪除；任務 COMPLETED/SUCCESS。
此為唯讀觀察，Runtime 原始 verification_status=UNKNOWN，不篡改為 postcondition PASS；矩陣 V=PASS 指測試檢查器已對照真實 filesystem 結果。
最新進度：T01–T14 完成（14／38）；依使用者要求暫停，不啟動 T15。後續若需大幅架構／協議／安全規則修改，先提出影響與取得同意。

T12–T14 UT／Runtime IT／Web IT 全部 PASS；下一個尚未開始的項目為 T15 project_sync。

T13 首輪真網頁 `RR-LOCAL_TEST-647198D5B30C241E`／`TASK-1B1ACDB61BA17AC21F11` 失敗：需求「不是列出目錄」被 listing heuristic 誤判，合法快照被拒絕，且拒絕誤標 DELETE_PREFLIGHT_REJECTED。修否定 mentions（保留獨立正向 listing）與通用 scope reason 後，以完全相同需求重驗通過：`RR-LOCAL_TEST-B8B979E8119E5800`／`TASK-261702A94468BC4EA494`，單次 inspect action，4 檔／64 bytes／逐檔 SHA 相符，無附件。fixture 另有 build、.venv、Runtime state/log 排除檔案。

T13 RCA：實際 Windows exclusive directory handle 使 src 無法枚舉，修前卻少算 main.py（file_count=3，正確應為4）仍回報成功快照。`_records` 新增 onerror，metadata／hash 讀取失敗也拋 `ProjectInventoryError`；工具回報 FAILED／PROJECT_SCAN_INCOMPLETE、scan_completed=false、snapshot_id 空。其他快照消費者維持拋錯 fail-closed，不發布部分快照。UT 涵蓋成功／空專案／缺檔與非目錄／父路徑擴大拒絕／禁止 fallback／不支援欄位／真目錄與檔案分享鎖／metadata fault injection／否定語意與正向 listing 保留。後續由 T14 揭露 metadata-cache 缺口，Runtime inspect 快照新增 verify_cached_content=true；同 size/mtime 改內容的 T13 UT／Runtime 回歸也 PASS。背景 project_sync 的既有 cache policy 未因此全面更換，仍列入 T15 後續驗證範圍。P1 policy 25 工具、snapshot compare 2 tests、T02–T04 scoped guard 回歸 PASS；獨立唯讀複審未發現剩餘具體缺陷。

T14 證據：`localdata/runtime_it/matrix38/t14_inspect_project_working_set.json`。UT 真 filesystem 修改同 size/mtime，修前返回舊 SHA；改為只對指定 working set 做 fresh SHA，以實際 bytes 作 content_identity，不依 Git index 或 metadata 快取猜測。隔離 Git repo（不操作應用包 Git、不 push）也測過 assume-unchanged 無法隱藏指定檔案變動。缺檔為 INCOMPLETE／PARTIAL；empty、相對越界、絕對越界、超出 explicit request files 均拒絕且 executed=false。完整專案 inventory 未呼叫，無關檔修改不影響 working set ID。

T14 複審找到 leading whitespace 預檢/執行不一致；兩端一致拒絕周邊空白，絕對路徑合法內部空白保留。UT 以真 leading-space 目錄證明不會讀到不同的 src/a.py。刪除已無用途的 working-set 全案 baseline 讀取，未改 background sync cache policy。最後複審未找到剩餘具體問題。

T14 Web `RR-LOCAL_TEST-CB62426D57B7A020`／`TASK-8E9951A6CB40DA9984BF`：單次 inspect_project_working_set，精確 src/a.py（6 bytes）與 docs/b.md（5 bytes），fresh hashed_file_count=2、reused_hash_count=0、missing_paths=[]、attachments_uploaded=0；測試器獨立逐檔 SHA/bytes 相符，task SUCCESS。未納入 unrelated.txt、未修改／上傳／掃全案。T14 完成後依使用者指示暫停，沒有啟動 T15。

2026-10-15 更新（取代上述 T14 暫停時的進度）：目前 T01–T20 全部 PASS（20／38）；下一項T21，T21–T38尚未驗證。

T18 RCA／修法：fresh SHA避免同size/mtime錯誤；request workspace與output_dir在掃描前驗證，連同`.agents`快取寫入端點限制。每個source part採獨占暫存檔及原子替換，拒絕reparse leaf，防止既存hardlink截斷外部檔案；實際讀回bytes相等並解包驗證後才發布native verification PASS，不把建包視為交付。UT包含輸出scope拒絕、內容偽造、真hardlink外部sentinel保持不變；hardlink測試output放在排除的`.agents`，避免將測試產物混入來源清單。原生loop 1 action／2 rounds PASS；P1 25工具及10MiB source回歸PASS。網頁request `RR-LOCAL_TEST-4B01407B9E5897AF`／`TASK-84447F326A205AD7D66A` 尚待終態及產物獨立驗收。證據`localdata/runtime_it/matrix38/t18_build_bundle.json`；UT `validate_build_bundle_matrix`。

T15 RCA 與修正：

- INDEX_ONLY 原本只沿用 metadata cache，同 size/mtime 改內容會發布舊 SHA；INDEX_ONLY／FULL_BUNDLE／DELTA 改 fresh-content snapshot。保留 snapshot／Git 資料格式，不放寬來源身分。
- 省略 root 會 fallback 到較大 Workspace：唯一 request root 可沿用；多 root 要求明確指定；任何 target 不得超出 request scope。正式工具結果為 REJECTED／executed=false，不標 UNKNOWN。
- 真 Web FULL_BUNDLE 上傳一個小型合成 bundle 後，ACK 的 conversation_id 被表示成 `[URL](URL)`，原字串比較產生 receiver_binding_mismatch。只解開 label 與 target 完全相同的 HTTP(S) URL；不同 receiver／session／interface／send ID 仍拒絕。保留原 ACK 證據，不修改 receipt、hash 或 action identity。
- 初次 Web INDEX_ONLY `RR-LOCAL_TEST-1EC66B521A6EFC70` PASS：一次 project_sync，2 files／18 bytes，attachments_uploaded=0。
- 初次 FULL_BUNDLE `RR-LOCAL_TEST-364ED3FF7925476E` 發現 ACK 表示差異，不列 PASS；已保留交易原 ACK。確認原因後只取消該自建測試任務，再以新 fixture 重測，未重送原交易。
- 修正後 FULL_BUNDLE `RR-LOCAL_TEST-9BF19B3F428D5837`／`TASK-30C018C7A32A4B8926B0` SUCCESS：一個 action、一個 batch/bundle，有效 ACK、CONFIRMED；摘要解析 checker 重檢保存結果，沒有重傳。
- DELTA `RR-LOCAL_TEST-37E91C6BCC2FAC94`／`TASK-1744172D25C8E0A5BD0A` SUCCESS：一個 action，僅 a.py 改動，一個增量 bundle 有效 ACK；完整 snapshot SHA 逐檔與本機相符。
- UT：fresh scope／三策略 SHA、正式拒絕結果、Markdown ACK 身分正負向、root resolution、10MiB source、ACK reupload staging、receiver identity、indexed query、P1 25 工具結果契約全部 PASS。Web FULL_BUNDLE／DELTA 必須收到有效 ACK 才更新總 PASS。

T15 證據：`localdata/runtime_it/matrix38/t15_project_sync_verify_only.json`（保留修前失敗 history），測試入口 `localdata/runtime_it/matrix38/t15_verify_only.py`。測试不驗 Telegram 網路端；仍使用既有 composer debug bypass，不代表 composer 原始驗證保護已驗證。

T16 RCA：成功 read_range 與缺檔查詢同批，索引重建一次後 business=INCOMPLETE；結果 policy 只辨識 READY，因此錯標 UNKNOWN/executed=false。已分類為 PARTIAL（保留成功內容及個別錯誤），全失敗為 FAILED。另重現 model 傳入父層 query root 時擴大 request scope，已在建立索引前拒絕，REJECTED/executed=false。沒有放寬 write/auth/ADS 邊界。

T16 UT `validate_query_project_matrix` 覆蓋七種 operation、520行連續分頁逐行比對、EOF/cursor、UTF-8內容/SHA、缺檔 mixed、同 size/mtime 過期索引重建、相對越界與父root擴大拒絕。既有 indexed query 與 P1 25 工具契約回歸 PASS；本機 loop 1 action/2 rounds PASS。

T16 Web 七操作 `RR-LOCAL_TEST-E8190AF48D696DD5`／`TASK-CFAB408E3156CCCB55A5` SUCCESS：同一 query_project 7筆結果均 OK，成功7／失敗0，來源內容、SHA 與本機相符，attachments_uploaded=0。混合缺檔 Web 尚待通過，總 T16 不列 PASS。

T16 mixed Web 首次 `RR-LOCAL_TEST-9700DB8A5750DB3D`／`TASK-4A902EF5E6532377DA95`：工具正確 PARTIAL/executed=true，但 action state 一律把 PARTIAL 當 pending，拒絕模型的 COMPLETE/PARTIAL（partial_not_supported_or_recoverable_work），多輪後停機。修正只辨識可信、未截斷、含成功與ERROR列的 query_project 完成結果；資料仍 PARTIAL、terminal eligibility仍false，不能冒充 SUCCESS。寫入／附件／程序／UNKNOWN及截斷partial仍受原 blocker 保護。新增 native mixed loop 1 action/2 rounds 可如實結案；成功終態判斷仍 UNVERIFIED。blocker recovery／P1回歸 PASS，真 Web 重測中。

T16 mixed 重測 `RR-LOCAL_TEST-11CB05BA02368387`／`TASK-8C20B697F358AE205531` PASS：1 action，執行 PARTIAL／evidence AVAILABLE，1 OK+1 ERROR，附件0；任務正常 COMPLETED/outcome PARTIAL、橙燈與接續 ID，沒有重做查詢或再補欄位。原生 SHA 與內容、action/query ledger 皆相符。T16 已完成，不把預期 PARTIAL 說成兩筆來源都讀取成功。

T17 RCA：metadata cache 漏列同size/mtime的改動檔；未知ID fallback成未知基準，非法結構可 KeyError/TypeError；scope可擴大；底層 snapshot loader把任意ID當檔案路徑；協議只接受dict而Runtime接受ID。已修 fresh SHA、先驗baseline/結構/root再掃描、明確REJECTED、loader只接受64hex且存檔ID/root一致和路徑留在workspace、schema合法型別dict|str。使用隔離旁側sentinel重現loader越界，修後不读取。UT與T15/T16/P1/ledger回歸PASS，native1action/2rounds准确列出a.py、added.md、gone.md；Web待通過。

T17 Web `RR-LOCAL_TEST-FEE51331464A0B19`／`TASK-3D3537CFE7CE56F06E8E` PASS：1 action/2 rounds，SUCCEEDED/executed=true，known ID與保存baseline一致，freshness=CHANGED，changed_file_count=3、changed_paths准确，無其他工具／上傳／修改。整體T17 PASS。

T18 Web `RR-LOCAL_TEST-4B01407B9E5897AF`／`TASK-84447F326A205AD7D66A` PASS：1 action／2 rounds，SUCCEEDED/executed=true，兩個實體bundle均獨立核對bytes、SHA及解包內容，source_file_count=2、complete=true、verification=PASS，delivery=NOT_ATTEMPTED；沒有其他action、來源修改或附件上傳。T18總PASS。

T19 Web `RR-LOCAL_TEST-EEF6627050295B84`／`TASK-BB3C1FC092BA380F63FF` PASS：1 action／2 rounds，added=added.md、modified=a.py、deleted=gone.md，tombstone準確；實體bundle的bytes、SHA與解包內容獨立核對，replay無mismatch，SUCCEEDED/executed=true／verification PASS／delivery NOT_ATTEMPTED。T19總PASS；未上傳或改來源。

T20 Web `RR-LOCAL_TEST-A3F71B8AE6D11040`／`TASK-55F09F5AF2532FD6F88E` PASS：1 action／2 rounds，實際擷取CMake兩筆、Python import、C++ include共四筆；三個來源bytes／SHA獨立核對，Gradle明列unsupported，warnings空，SUCCEEDED/executed=true／PASS／NOT_ATTEMPTED。模型如實說明有限pattern範圍，沒有冒稱完整graph或補猜未支援關係。T20總PASS。網頁IT此案例是正常唯讀擷取；PermissionError部分／全失敗的分類由UT驗證，不代表真Web負向終態均已覆盖。

T20 RCA／修法：extract_project_dependencies可擴大至父workspace，parser讀取失敗只記warning卻被共用結果標SUCCEEDED。已套用request_scoped與fresh source SHA；parser實際讀bytes、核SHA後嚴格UTF-8解析，新增scanned_files／unsupported_files／source_checks及SUPPORTED_SOURCE_PATTERNS_ONLY範圍。有warning則部分成功PARTIAL／全讀失敗FAILED，不把未支援Gradle或有限pattern結果冒充完整graph。UT包含四筆CMake/include/import已知證據、Gradle未支援、單檔／全部PermissionError、父scope拒絕。原生1action／2rounds與來源bytes/SHA核對PASS，P1回歸PASS；Web待驗收。證據`localdata/runtime_it/matrix38/t20_dependencies.json`，UT `validate_dependencies_matrix`。

T19 RCA／修法：build_project_delta未限制明確request root，父workspace誤走DELTA_NOT_AVAILABLE而非scope拒絕；已在讀取base前套用request_scoped。共用結果缺少交付及native verification；新增實際delta manifest replay，加上既有part讀回驗證，明確NOT_ATTEMPTED，空delta／刪除-only亦要replay PASS。複審發現深層`.agents/project_context/deltas` outward junction可繞過只檢查`.agents`；已在resolve前檢查lexical delta及snapshot／tmp端點。真Windows junction UT確認REJECTED且外部owned目錄保持空。UT包含增改刪、同size/mtime變更、空delta、刪除-only、未知／非法base、replay故障不假成功、精確scope。UT／原生loop（1 action／2 rounds）及T18／T15fresh scope／P1回歸PASS；真Web尚待驗收。證據`localdata/runtime_it/matrix38/t19_build_delta.json`，測試`validate_build_delta_matrix`。
T03 入口：`source/WebAgent/tests/run_matrix38_t03_inspect_directory_it.py`；證據 `localdata/runtime_it/matrix38/t03_inspect_directory.json`。
T03 網頁 IT：`RR-LOCAL_TEST-261E0D0E0CED42CC`／`TASK-185519795F00B7CBD3D2`；2 個 action、2 輪 planner、worker 約 95.2 秒、SUCCESS，無附件與 mutation。
T03 的原生 Windows boolean probe 使用快速路徑；測試先修正注入點，明確以「boolean false + stat PermissionError」重現誤分類，再於實際 stat 控制點修復；不把未命中的 mock 當成 Runtime bug。
P1 result contract／flexible Progress admission 回歸 PASS，不代表矩陣其他工具已做 IT。
T04 證據 `localdata/runtime_it/matrix38/t04_find_file.json`；網頁 `RR-LOCAL_TEST-0E8227C327131226`／`TASK-F80A369B88FC576B7904`，2 actions，2 輪 planner，約 113.2 秒，SUCCESS，完整 3 matches／真 no-match、無上傳與 mutation。
T04 核心差異：搜尋的 complete 僅針對已聲明的範圍／excluded directory/link policy，不代表被排除的 build 等目錄已搜尋；無命中與不可搜尋分開。
T05 證據 `localdata/runtime_it/matrix38/t05_write_file.json`；UT 包含 Unicode/LF、空檔、4096／4097 字元、覆寫、新 parent、PermissionError、non-file、readback mismatch。實際 file lock 不覆寫原檔；只需修驗證時不重做 mutation。
T05 網頁 IT：`RR-LOCAL_TEST-7DB32CC553660449`／`TASK-7B938AE40B169605187E`；1 個 write_file、2 輪 planner、約 109.8 秒。47 bytes，SHA256 `44c26e23a015300d0b5fa79998fd9ed6814f1ba33275988720c03325544d716c`，readback PASS，SUCCESS，無其他工具／附件。
T06 證據 `localdata/runtime_it/matrix38/t06_delete_path.json`。
正式網頁 request `RR-LOCAL_TEST-211984722E91E1F4`，task `TASK-C7202D0F80C13440BEC2`。
待單次批准 `APR-3494F5E1446E0FD5F3AB`，target `E:\card\runtime_it\matrix38\T06-fc4ab301a8ca\web_disposable_c2546175`，0 entries／0 bytes。
不得替正式流程合成批准、修改永久授權或略過確認。若批准過期，記錄拒絕且目標保留；取得新明確批准後，重跑尚未執行的刪除，不能重播已成功副作用。
T06 原測試批准過期，`USER_APPROVAL_EXPIRED/executed=false`，原目標仍在；原失敗留在 attempt_history。
使用者「核准 單次」後重測同一目標：`RR-LOCAL_TEST-FF40AA5CD97D0650`／`TASK-55EB1D4EA13A52D0FA30`，新批准 `APR-49E095C041F469A2801D`。
批准前核對精確 target、request/task/action、期限、0 entries/0 bytes 與 fresh manifest SHA；新批准為 CONSUMED，未呼叫永久授權 API。
真刪除 action `delete-t06-retry-02`：SUCCEEDED/DELETED；linked VERIFY `verify-t06-absence-04`：exit=0/stdout=False/PASS，verifies_action_id 指向該刪除。目標不存在，終态 SUCCESS，worker 約 239.6 秒（含待批准）。
T07 證據 `localdata/runtime_it/matrix38/t07_begin_file_write.json`；網頁 `RR-LOCAL_TEST-0B2BE4AF4061E4A7`／`TASK-987B64A0C3C7BAB35826`，3 actions：begin OPEN/0 bytes → abort ABORTED → linked readonly VERIFY PASS，目的檔不存在、staging 移除、SUCCESS。原測試失敗原因是未安排後置檢查與 parent scope，非 Runtime 未建立交易；修測試、不略過 guard。
T08 證據 `localdata/runtime_it/matrix38/t08_write_file_chunk.json`；UT／IT-R 實際磁碟 bytes 和 manifest 一致，chunk 只記接受不記正式檔案交付。IT-R 5 actions、6 planner rounds，最後取消與真唯讀 VERIFY PASS。
T08 首輪網頁 `RR-LOCAL_TEST-BE5213BB474AF497`／`TASK-E19D942C259AF58ED066`：五項 action 及業務 SUCCESS，但取消後僅核對 metadata，缺獨立 staging readback，coverage=UNKNOWN。
T08 觀察器初版重測 `RR-LOCAL_TEST-9EE4963723986632`／`TASK-D0BDC2B2BCDC215C607B`：輪詢 manifest 時觸發 Windows 原子替換 WinError 5；chunk 可能部分執行，Runtime 原樣標 UNKNOWN，未篡改 PASS。此為測試干擾，非已證實 ACL 權限問題；只修 checker，不弱化 Runtime guard。
觀察器改為 FILE_SHARE_READ/WRITE/DELETE 讀取此測試唯一 staging，完全不碰原子替換中的 manifest，取得完整兩塊即停止；Temp stress：200 append + manifest replaces 全通過。VERIFY checker 同時要求主命令／verify命令正向唯讀 grammar、精確 target 與 expect=false，接受 canonical action/type 別名。T07 既有真網頁證據重新套用此 checker PASS，不需重刪任何目標。
受干擾 T08 LOCAL_TEST 超過 600 秒後，只協作取消該 exact request/task；task 最終 CANCELLED，交易已 ABORTED，未停止 Agent／關網頁或重送有疑義副作用。
T08 修後新網頁 `RR-LOCAL_TEST-7A29F646AB9CDE22`／`TASK-BBBF589F61B922F5E30C`：5 actions，獨立 staging 實讀 14/27 bytes 與 UTF-8 原文一致；取消 staging 移除、目的檔不存在、linked VERIFY PASS／SUCCESS。fixture-only manager restart續寫與 crash後截回已確認 byte boundary 的 UT 通過；沒有重啟真 Agent。
T09 證據 `localdata/runtime_it/matrix38/t09_commit_file_write.json`；IT-R 4 actions／5 rounds，實際 final bytes/SHA 與驗證相符，不上傳。
T09 首輪 Web `RR-LOCAL_TEST-8738983F185A5B41`／`TASK-1C8182BA8C878B6D2533`：commit 確實寫出正確 bytes/SHA，但 Runtime `_evaluate_verification_step` 忽略 `type`，將兩項檔案 predicate 誤當 run_command；`contains` 也未比較。雖任務回報 SUCCESS，矩陣保持 FAIL。另有 checker 不接受等價 Get-Content -Raw -LiteralPath 排序，已修為有限正向語法重排，未放寬 Runtime 執行權限。
Native 修前重現：存在檔案 expect=false、ACTUAL 檔案 contains=WRONG 兩項皆假 PASS。修後接受等價 action/type、text/contains；欄位衝突、未知 action、空 path/needle、非 bool expect 為 SPEC_INVALID；explicit stat 的 PermissionError 不冒充檔案不存在。Legacy command-only 與 canonical action/text 保持支援。修後負向／正向 UT、P0 read/command、P0 artifact、P1 contract、Progress terminal 回歸 PASS，T07/T08 IT-R 重跑 PASS。沒有重新執行真 delete。
T09 修後 Web `RR-LOCAL_TEST-52B202B96C94F3FA`／`TASK-B3C6CB3F2BE1522DA143`：4 actions；真 file_exists exists=true/expected=true、file_contains text=T09_COMMIT_OK/found=true，不再只有 run_command exit=0；29 bytes／SHA256 `caad70343d51e58522b2a0cb55434f8479bf18df1edaab4e8c2508347669b6bd`。測試器獨立 raw UTF-8 bytes 完全相符；PowerShell Get-Content 未指定 -Encoding 時中文 stdout 曾有編碼差異，不把 stdout 誤稱完整 UTF-8 一致性證明。
T10 證據 `localdata/runtime_it/matrix38/t10_abort_file_write.json`；UT 和 IT-R 通過，4 actions／5 rounds；abort 後目的檔仍是原始 BASE bytes，無附件。
T10 網頁 `RR-LOCAL_TEST-5F5BB80A1DFA00FC`／`TASK-0F5765B0527A838936C7`：4 actions，30 bytes staging 被取消移除；原檔 raw bytes 仍完整等於 `T10_KEEP_ORIGINAL\n`。Runtime 真 file_exists/file_contains 兩項 PASS、verifies_action_id 關聯此次 abort，SUCCESS。沒有 commit 或附件。
T11 證據 `localdata/runtime_it/matrix38/t11_run_command.json`。所有 native 命令都真的執行，CMake/Git 只操作全新合成 fixture；GIT_MUTATE 不提交應用包或使用者 repo、不推送。PROCESS 用委派真 Popen 的 spy 觀察真 PID running，確認完成前尚未有 final result，再等實際 exit 0。
T11 Runtime 修前：合法 expected_failure 被 _run_command_verification_detail 以 missing=verify 拒絕。修後只接受 canonical schema 已验证的 BUILD/TEST 負向契約，普通修改／無效 exit=0／空期待／未知格式仍不豁免。另一次 expected_failure_not_bound_to_success_contract 是測試器漏前置 goal binding，修測試不放寬 Runtime。
T11 GENERAL 修前：依 Runtime next_actions 完成 linked VERIFY/PASS，仍未寫入任務 condition ledger，再次 terminal_success_verification_missing。修復 remedy 明列 condition_id 與 required_fields，不假造 PASS、不自動推論綁定，真正執行新 VERIFY 後 task 結案。正式 Native IT-R 普通矩陣 12 actions／13 rounds，GENERAL 2 actions／4 rounds（含一次拒絕與補救）。
T11 command field repair／terminal lifecycle 回歸 PASS，獨立 read-only 複審未發現剩餘問題；實際 Web 分四小組：A INSPECT/MUTATE/VERIFY 歷史替換，B BUILD/TEST 正負向，C 合成 Git，D PROCESS/TRANSFER/GENERAL goal verification。

T11 Web A：`RR-LOCAL_TEST-344C5372D7EA567A`／`TASK-BEB73B3A22F94EF0BD2D`，4 個實際 actions，原 MUTATE 只執行一次；最後兩筆 history 的 observed_verification_status 為 FAIL→PASS，effective_verification_status=PASS，最終 SUCCESS。首輪測試器誤查 actions，造成假陰性；Runtime 正確資料位於 results，已修 checker 並離線重驗原始證據，不重播任何 action。

T11 Web B：`RR-LOCAL_TEST-5EE45F3C8A9A94FB`／`TASK-DDD3CCC08711F8991C5C`，4 actions。真 CMake 產生指定內容、TEST 正向 PASS；不存在目錄 BUILD exit=1、TEST exit=7 均保留 FAILED/FAIL，預先宣告的 expectation_status=PASS，task SUCCESS。未建立 missing_build 或改寫原始失敗。

T11 Web C：`RR-LOCAL_TEST-8304691FF3D0735F`／`TASK-B895EF2BF1E104740467`，2 actions；新建合成 repo HEAD=`053386814d37cd84d2afc7d8233d5fb98bfcf1b4`，message=`T11_FIXTURE_CHECKPOINT`，獨立 Git 查核相符。未操作應用包 Git 或 push。

T11 Web D：`RR-LOCAL_TEST-8CC0B969AE6D39EB`／`TASK-DEAB5947A0E1E5BC7701`，5 actions；PROCESS/TRANSFER/GENERAL/VERIFY，另一次唯讀 VERIFY 綁 Runtime condition_id，客觀 predicate PASS 後 SUCCESS。GENERAL 原 terminal_eligible=false 保留，未重播前三項。測試器固定四项產生假陰性，修為只允許第五项同一 GENERAL 的唯讀条件 VERIFY，並重验保存證據；不放寬任何 Runtime guard。額外補問與驗證成本仍如實保留。

T12 證據 `localdata/runtime_it/matrix38/t12_aggregate_verification.json`。原生 UT：cwd 與正向內容、混合 exit 0/1/0、找不到命令、逾時、全拒絕、部分拒絕、空規格，以及真 native Popen 啟動失敗。未執行的安全拒絕回報 REJECTED/false；有部分實際檢查則 PARTIAL；process_started 來自真 Popen。restricted dispatch UNKNOWN 另以純政策故障輸入驗證，未冒稱實際 ACL-on executor IT。

T12 RCA：原先 aggregate 安全拒絕也 executed=true；新增真啟動旗標、逐項結果與共用政策分層。Windows shell 超時只 kill 父程序留下子程序與 capture 鎖；改為只對仍存活的自有 Popen PID 樹終止，不全域停 Agent。已完成 scoped checker 的 FAIL 曾錯走 REPLAN、擋住新 aggregate；保留外層 FAILED 與 verification FAIL，但 checks_completed 證明檢查已執行，允許新的關聯 VERIFY。Runtime IT 真正執行 MUTATE→aggregate FAIL→新 aggregate PASS，history 保留、最新 PASS，3 actions／4 rounds 收斂。

T12 測試器修正：初始 next_action 殘留 list_directory，及 findstr 遇到沒有末尾換行的輸入，不屬於 Runtime 放權問題；改為工具一致與明確 Windows 換行 fixture。P1 25-tool policy 回歸與 terminal lifecycle 回歸 PASS。實際 Web request `RR-LOCAL_TEST-0EA60793FDF4E9B5`／task `TASK-36BDB74CA3D965276DA9` PASS：一次 MUTATE、兩次 aggregate，共 3 actions、5 個真 checker 程序。負向 exit 0/1/0、正向 0/0、同一原 MUTATE 的 history FAIL→PASS 與最新 effective PASS 相符，task SUCCESS；獨立讀取 bytes 精確為 T12_ACTUAL 加 CRLF，保護檔不變。沒有重做 MUTATE 或上傳。獨立複審的兩項具體缺口均處理後，未發現剩餘具體問題；修後 T11 原生 UT／Runtime IT 再次 PASS。
