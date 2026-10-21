# 模型呼叫 Runtime Tool 規劃表

日期：2026-10-16

目標環境：`C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`

狀態：執行中。T01～T14 已依本表重新執行 UT、真 Runtime loop 與 Edge／ChatGPT Web IT；T15～T38 尚未依本表完成，不沿用舊 PASS 自動升格。

相關紀錄：

- [工具執行與結果遷移矩陣](runtime_result_migration.md)
- [模型精準調用與 Runtime 相容性改善紀錄](runtime_precision_compatibility_plan.md)

## 1. 目標與範圍

驗證完整鏈路：模型回覆 → 解析 → 正規化 → 准入 → 實際調用 → 結果回傳 → 下一步／終態。

原矩陣偏重「工具是否真正執行、檢查與如實回覆」，本表補充「模型輸出如何可靠地映射成正確工具呼叫」，並覆蓋缺漏、等價表示、補救、依賴、重送與等待。不只針對近期兩筆失敗案例。

核心原則：

- 正確 action 必須調用正確工具，傳入正確參數，執行次數可核對。
- 格式等價可正規化；操作語意、路徑、授權、命令、成功結果不能猜。
- 單一 action 的失敗只阻擋依賴它的操作，不無差別阻擋獨立合法 action。
- 執行、驗證、交付與任務終態分層記錄；QUEUED 不等於 DELIVERED，exit code=0 不等於需求已達成。
- 補救對準實際阻塞；不重播已完成副作用，不以無關讀取填補交付回執或驗證缺口。
- 既有安全、ADS、來源 SHA、snapshot 與授權 scope 檢查不得為降低拒絕率而放寬。

## 2. 全工具共通呼叫契約測試

每個工具均須評估 C01～C12 的適用性。沒有對應能力的格子標 N/A 並說明原因，不當成 PASS。

| 編號 | 驗證面向 | 驗證方法 | 通過條件 |
| --- | --- | --- | --- |
| C01 | 正確呼叫 | 提供完整 canonical action，記錄實際 dispatcher／執行器收到的工具、參數與呼叫次數 | 正確工具收到正確參數；實際執行一次；客觀結果與回傳一致 |
| C02 | 等價格式兼容 | 依明訂 schema 測試數字字串、支援的別名、欄位順序及合法省略 | 正規化不改語意與身分；可確定沿用的不重問；不支援的轉換不得猜值 |
| C03 | 必填欄位缺漏 | 個別及同時移除必要欄位，包含巢狀欄位 | 一次回報所有可安全判定的缺漏與欄位位置；不執行不完整操作 |
| C04 | 非法／衝突輸入 | 非法型別、未知欄位、別名衝突、無效 JSON／Windows 路徑轉義 | 精準指出錯誤；不靜默更改路徑、命令或操作；有歧義時補問 |
| C05 | 補救保留原操作 | 補 Progress／action 欄位，包含修復會改變語意的情境 | 合法參數及未執行操作不遺失；語意改變依 replacement 身分規則處理；已完成操作不重播 |
| C06 | 獨立與依賴准入 | 同輪合法 A、錯誤 B、依賴 B 的 C；另測非關鍵 Progress 錯誤 | A 可執行；B 不執行；C 等待；不因顯示欄位錯誤連坐；安全必要條件不可略過 |
| C07 | 身分與重送 | 同 action ID 同內容重送、同 ID 不同內容、不同 request 引用、正式接續 | 不重播副作用；身分衝突明確拒絕；跨 request 只能循可信接續流程，不任意掛接歷史 |
| C08 | 路徑與授權 | 子目錄、空白／中文、相對路徑、自然語言尾綴、父目錄擴張、ADS／link | 正確路徑不誤拒；越界不執行；requested/resolved、cwd 與有效 scope 可核對 |
| C09 | 結果忠實度 | 注入成功、拒絕、失敗、部分完成、未知、執行前後例外 | executed、execution、verification、delivery 各自反映事實；未執行不能成功；保留原始原因 |
| C10 | 非同步等待 | 程序執行中、附件處理、交付排隊、分塊未提交及查詢超時 | 走正確等待／查詢；不重做、不誤當完成、不改道無關讀取；無法確認回 UNKNOWN／等待 |
| C11 | Evidence 更新 | 舊／新驗證、FAIL→PASS、snapshot 變更、過期引用 | 最新有效結果更新且歷史保留；新證據可解除相應阻塞；無關歷史不永久卡住任務 |
| C12 | 收斂與終態 | 缺證據、可補救、客觀阻塞、完成、相同阻塞重複 | 補救只取得缺口；無進展時明確停等／詢問；終態需對應完成契約與實際 evidence，不靠模型宣稱 |

## 3. T01～T38 專屬呼叫與驗收規劃

每列均需搭配適用的 C01～C12，不以單次正向工具測試取代共通契約測試。

| 編號 | Runtime tool | 專屬調用契約與驗證方法 | 關鍵通過條件 |
| --- | --- | --- | --- |
| T01 | read_file | 小檔 inline、空檔、UTF-8、缺檔、權限拒絕、大檔必要交付分流；核對實際 bytes／內容／ref | 讀取與上傳不混淆；排隊不算讀取完成；原始錯誤保留 |
| T02 | list_directory | 精確第一層 fixture 清單；測 path、depth、include_files 與授權邊界 | 實際清單一致；不擴大至父目錄；獨立讀取不被非關鍵 Progress 擋住 |
| T03 | inspect_directory | 多 paths、recursive、sample_limit、缺項、混合成功／失敗 | path／paths 不混用；採樣與完整計數標示清楚；逐項狀態與 partial 正確 |
| T04 | find_file | root/name/max_results、巢狀同名、無結果、超額及非法 root | matcher 與 root 正確；空結果不是工具失敗；不擴大搜尋 |
| T05 | write_file | 精確內容、覆寫政策、UTF-8、4096 邊界、寫入及讀回失敗 | bytes／hash 與輸入相符；超限一次給出分塊契約；補驗證不重做寫入 |
| T06 | delete_path | 僅新建測試目標；精確絕對路徑、安全批准、批准過期與目標變更 | 未批准／目標變更不刪；刪除後獨立檢查；拒絕不能記成功 |
| T07 | begin_file_write | write ID、encoding、overwrite、身分衝突與重送 | 建立正確暫存交易；不提前產生正式檔案；不覆蓋其他交易 |
| T08 | write_file_chunk | UTF-8 byte offset、自動 offset、chunk identity、重複與缺塊 | 寫入實際 bytes 正確；不靠模型猜 byte offset；收到 chunk 不等於完成 |
| T09 | commit_file_write | expected_size／SHA、缺塊、目的地衝突、重送及讀回 | 只提交完整交易；正式 bytes 正確；重送不重寫；失敗不假完成 |
| T10 | abort_file_write | 指定 write ID、重複取消、commit 後取消、其他交易隔離 | 只取消對應暫存；既有正式檔不變；取消後不可偷 commit |
| T11 | run_command | 下節全部 10 operations；實際 command／cwd／程序、驗證規格與關聯 | 命令語意與 operation 一致；啟動／結束／驗證分開；exit=0 不自動代表目標達成 |
| T12 | aggregate_verification | 多 checker、混合 FAIL、拒絕、程序錯誤、cwd、驗證替換 | 每個檢查真執行；保留逐項結果；FAIL→PASS 不重做原 mutation |
| T13 | inspect_project_scope | 小專案 inventory、排除項、容量上限與精確 root | 不冒充 list_directory；不掃父 workspace；掃描範圍可核對 |
| T14 | inspect_project_working_set | 選定兩檔，分別變更選中／未選中檔；Git／filesystem binding | working set 精確；無關變更不造成局部任務全案阻塞 |
| T15 | project_sync | INDEX_ONLY／DELTA／FULL_BUNDLE、base、容量、scope、ACK | strategy 對應流程真執行；INDEX_ONLY 不上傳；bundle 例外需實際 ACK |
| T16 | query_project | 依現有 schema 展開全部 operation；分頁、EOF、snapshot 過期、批次 partial | 查詢內容及版本正確；合法獨立結果保留；不把截斷當完整來源 |
| T17 | compare_project_snapshot | 基準後增改刪、無變更、非法基準 | 差異與檔案實況一致；不以新 snapshot 冒充原 baseline |
| T18 | build_project_bundle | 小專案建包、manifest、上限、敏感檔排除、解包比對 | 包內 bytes／inventory 正確；建包不等於上傳；失敗不留假成功 |
| T19 | build_project_delta | base 綁定、增改刪、空 delta、manifest replay | 只含應有變更；基準／scope 正確；空 delta 如實表示 |
| T20 | extract_project_dependencies | 已支援 parser、未支援格式、單項／全部讀取失敗 | 來源 SHA／行號可核對；有限 parser 不宣稱完整依賴圖 |
| T21 | inspect_project_ledger | 真持久 ledger、缺歷史、讀取錯誤、損壞、limit | 讀失敗不冒充 EMPTY；project／snapshot／provenance 正確 |
| T22 | query_project_history | event/path/snapshot/time/limit filters、空結果、不同專案 | filter 真套用；順序／數量正確；不同專案不串歷史 |
| T23 | inspect_semantic_map | 未建、已建、局部、source 變更與 stale | 未載入不當已分析；觀察結果不誤觸無關強制補救 |
| T24 | update_semantic_map | schema、snapshot、source SHA、局部更新與持久讀回 | 精確更新、不抹範圍外資料；輸入拒絕不修改；保存後實讀驗證 |
| T25 | update_semantic_map_file | 檔案版與 inline 等效、expected SHA、根目錄 patch 參與 snapshot | 可信讀檔與來源檢查一致；只處理精確 patch 自身影響，不忽略真來源變更 |
| T26 | propose_task_plan | 完整／缺欄 schema、scope、snapshot、freeze 與 plan ID | PLAN_FROZEN 真保存；未 execute 前來源不變；不把提案當完成修改 |
| T27 | propose_task_plan_file | 檔案版／inline 等效、expected SHA、讀取授權 | 兩入口身分與 scope 一致；hash 不符不得 freeze |
| T28 | repair_task_plan | 可控拒絕後合法 repair、原 plan identity、重送 | 修正解除真缺漏；歷史保留；合法修復不再被已取代拒絕卡住 |
| T29 | execute_frozen_plan | plan ID、scope、snapshot、子 action 依賴、失敗與重送 | 每個子 action 可核對；不重播；例外回滾如實回報；不宣稱未實作的 crash 恢復 |
| T30 | validate_edit_plan | required fields、source body、BEFORE、SHA、snapshot | VALID 不修改檔案；MISSING_CONTENT 導向精確 read prerequisite；錯來源拒絕 |
| T31 | apply_edit_plan | BEFORE→AFTER、套用前重檢、備份、例外回滾與 partial | 精確 bytes／scope；來源變更不套用；回滾可核對；crash 恢復仍不在本次範圍 |
| T32 | upload_file | 單檔身分、selected／processing／ready／consumed、配額／拒絕 | 真 provider 呼叫次數與 marker 回讀一致；不盲目重傳；未 consumed 不假稱模型已讀 |
| T33 | upload_files | 多檔 inventory、逐檔失敗、unexpected、dedupe | 每檔身分與狀態保留；混合失敗如實 partial；composer 文字不當 provider error |
| T34 | download_artifact | fresh request／artifact、SW 優先、原檔 bytes、頁面重繪、錯圖／保存失敗 | 正確新 artifact；原檔 bytes／格式／hash 可核對；不錯殺已登記同圖、不重生成 |
| T35 | return_artifact | 來源 hash、enqueue、QUEUED→DELIVERED／FAILED／等待、去重 | 等待回執不轉讀檔；message/file ID 與必要原檔回讀可核對；不重複交付 |
| T36 | google_drive_upload | 授權、真 client 呼叫、file ID／metadata、失效授權與網路錯誤 | 真交付才 PASS；未配置標 BLOCKED_ENV／未測；不記錄 token |
| T37 | web_search | 原 query、正常／空／網路／格式錯誤、來源 URL | query 不替換；來源保留；provider 回應不等於來源內容已查證 |
| T38 | save_session_summary | 欄位、unique marker、讀回 hash、權限錯誤、工作隔離 | 真持久化且不污染其他工作；讀回失敗如實 partial；保存不代表原任務成功 |

### T11 必須展開的 operation

| Operation | 專屬驗證 |
| --- | --- |
| INSPECT | 唯讀 command／cwd／stdout；未提供額外 verify 不憑空宣稱其他修改完成 |
| MUTATE | 真修改及產物；exit=0 無預期產物、後置 FAIL、補驗證不重播 |
| BUILD | 合成專案成功／失敗、產物大小／位置、只修驗證不盲目重編 |
| TEST | 真測試數、assertion pass／fail；預期 FAIL 與測試目標達成分層 |
| VERIFY | 綁 action／condition、真 checker、FAIL→PASS、正式跨 request 接續 |
| GIT_INSPECT | 唯讀 Git 資訊與實際 repo 一致，不改動歷史 |
| GIT_MUTATE | 只用合成 repo；真 commit／HEAD 驗證、重送不重複提交，不 push |
| PROCESS | 真 PID、RUNNING→EXITED、退出碼；啟動不當完成；只處理測試自有程序 |
| TRANSFER | 真來源／目的 bytes 與 hash；拷貝不等於 Telegram／外部交付 |
| GENERAL | 逃生口執行結果如實記錄；不可自動宣告任務成功，須有適當 goal verification |

實作前須以當時正式 schema 重新核對 operation／欄位集合；發現差異先更新規劃，不憑文檔創造新的工具參數。

## 4. 跨工具控制層

report_progress、final_response、turn_commit 不混入 38 工具數，但必須單獨及與工具組合驗證。

| 控制面向 | 驗證方法與通過條件 |
| --- | --- |
| Progress 正規化／增量沿用 | 合法省略沿用可信欄位；數字字串與合法 status 別名可正規化；缺少當輪 decision／evidence 不得捏造 |
| Progress 與 action 准入 | 非關鍵顯示錯誤不阻擋獨立合法 action；授權、依賴、操作必要條件仍要檢查 |
| Condition 與終態 | ID 唯一映射可由 Runtime 推導分類；未知／衝突／過期 ID 精準補問；SUCCESS 必須有實際目標證據 |
| Turn commit | 驗證完整回合、action_count 語意與實際 action 集合；半份／錯輪／舊 assistant 回覆不得執行 |
| 批次補救 | 一次列出可判定錯誤；保留 pending action；不可重播已執行副作用；真正 blocker 修復後可前進 |
| 任務接續 | Telegram／WebCopilot 共用可信 task lineage、原需求與 evidence；復原後查核未知結果，不任意掛接其他任務 |
| Transport | composer、submit、user/assistant scope、readback 分層；送出前拒絕不當成模型回覆拒絕；不以重送掩蓋不確定送達 |

## 5. 驗證層級

| 層級 | 方法 | 可證明的範圍 |
| --- | --- | --- |
| UT／契約測試 | 固定 canonical 及變形回覆，控制執行器邊界，對照呼叫次數／參數；另用真 fixture 確認副作用 | 排除模型隨機性，定位解析、正規化、准入及 lifecycle 規則 |
| IT-R：真 Runtime loop | 可控制的模型回覆序列，正式 loop／dispatcher 搭配真工具與合成 fixture | 完整補救、依賴、重送、等待、evidence 更新與終態鏈路 |
| Web IT | 正式 LOCAL_TEST ingress、既有 Edge／ChatGPT，真模型產生 action；外部交付用正式 adapter | 提示契約、DOM、送出、回讀、Runtime 整合；外部交付另以回執證明 |

自然語言 prompt 不能保證模型產生指定錯誤。缺漏／衝突／重送等負向測項由固定回覆注入確保命中，不能只靠多跑 prompt。UT 的 mock 通過不冒充真執行或外部交付通過。

## 6. 實施順序與測試安全

1. 盤點當時正式 tool schema／alias／operation／producer，對齊既有證據，建立 38×12 適用性矩陣與可重用 fixture。
2. 優先 T35 交付等待、T11／T12 驗證、Progress 與獨立 action 准入。
3. 依工具群依序覆蓋剩餘工具，每項先 UT、再 IT-R、再 Web IT；保留所有失敗嘗試。
4. 跨工具任務：讀取→寫入→驗證→交付；缺來源→精確讀取→編輯；失敗驗證→新驗證；中斷→接續；同輪獨立與依賴混合。
5. 回歸安全拒絕、scope、ADS、SHA／snapshot、去重與未知結果；不可只比較拒絕率下降。

只操作唯一合成測試目標；不以使用者專案作首次破壞性測試。不全域停止 Agent、不清除 profile、不重播已完成副作用。需要額外授權、刪除批准、外部服務設定或更換 URL 時先詢問。不自動 sync-back、update、Git commit 或 push。

## 7. 執行紀錄表模板

每個適用的 Txx×Cxx 獨立一列。工具級狀態由覆蓋明細彙總，不以一筆 PASS 取代整個工具的涵蓋率。

| 工具／案例 | 預期調用與參數 | 實際調用／次數 | 客觀副作用／檢查證據 | 回覆狀態是否一致 | UT | IT-R | Web IT | RCA | 最終修法／重測證據 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 待建立 Txx-Cxx | 待填 | 待填 | 待填 | 待填 | NOT_TESTED | NOT_TESTED | NOT_TESTED | 待測後填 | 待測後填 |

### 2026-10-16 T01～T05 執行紀錄

| 工具／案例 | 預期調用與參數 | 實際調用／次數 | 客觀副作用／檢查證據 | 回覆狀態是否一致 | UT | IT-R | Web IT | RCA | 最終修法／重測證據 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 `read_file` | 小檔與空檔各一次；大檔一次，由 Runtime 自動分流 | 小檔 2 次；大檔 1 次；未混用 upload/query/command | inline bytes/text 相符；大檔 1,139,070 bytes，receipt 為同 request 的 CONSUMED | 一致；QUEUED 未提前算成功 | PASS | PASS | PASS | IT-R 測試器曾用後續 Web 輪替檔 SHA 比對固定 `large.txt`，形成假失敗；另一次 Web 結案遇 transient stale readback，但兩次 read action 未重播 | IT-R 改以實際 staged fixture SHA 比對；fresh Web 重測小檔 `RR-LOCAL_TEST-E51ED894C92392AB`、大檔 `RR-LOCAL_TEST-4740964068DE2F05` PASS |
| T02 `list_directory` | 兩個精確 path，各列第一層一次 | 2 次 `list_directory`；無其他工具 | 第一目錄 4 項、第二目錄空；缺目錄／非目錄／拒絕／父層 scope 均有 typed 結果 | 一致；未擴大至父目錄 | PASS | PASS | PASS | 無 production 缺陷 | Web `RR-LOCAL_TEST-421027C7A3EF47E7`／`TASK-E5DC36FC10242B56A239` PASS |
| T03 `inspect_directory` | 同一 paths，recursive false/true，各 sample_limit=1 | 2 次 `inspect_directory` | false=2 files/2 dirs/10 bytes；true=3 files/2 dirs/21 bytes；拒絕與 partial 分開 | 一致；`paths` 未被改寫成 `path` | PASS | PASS | PASS | 無 production 缺陷 | Web `RR-LOCAL_TEST-2D65CEC3AC6977C3`／`TASK-5A66314CD3B0E4D91598` PASS |
| T04 `find_file` | 同 root 搜尋命中與無命中，各一次 | 2 次 `find_file` | 3 matches 與 `matches=[]` 均符合 fixture；limit、拒絕、junction 不越界由 UT/IT-R 覆蓋 | 一致；無命中未誤報工具失敗 | PASS | PASS | PASS | 沙箱不能建立 junction fixture，屬測試環境限制，不是 Runtime 缺陷 | 正式 Windows 權限下 UT/IT-R PASS；Web `RR-LOCAL_TEST-649560EC6E4D5425`／`TASK-D495A452EFCB7256B132` PASS |
| T05 `write_file` | 一次精確 UTF-8/LF 寫入；另以 UT/IT-R 覆蓋空檔、4096/4097 邊界、覆寫、拒絕與讀回不符 | Web 只呼叫 1 次 `write_file`；無命令、分塊、讀檔、上傳或刪除 | 47 bytes 寫入、47 bytes readback；SHA256=`4f9daaa0a70bbbe9d318f4faf09b6130f7152a6e5dd516246490b49d1c73370e`；verification=PASS | 一致；執行、readback 與 terminal SUCCESS 均由同一 request evidence 支持 | PASS | PASS | PASS | Codex sandbox 對 `E:\card\runtime_it` 寫入回 `WRITE_ACCESS_DENIED`，屬測試程序權限差異；另舊 IT-R 固定 action ID／檔名不利重跑診斷 | IT-R 改用每輪唯一 action ID／檔名並保存失敗 ledger；正式 Windows 權限 IT-R PASS；Web `RR-LOCAL_TEST-F36AE50186B721B2`／`TASK-20759FA1447FD3B33E76` PASS |

### T06 2026-10-16 新驗證

| 工具 | UT | IT-R | Web IT | 實際證據／RCA | 修正 |
| --- | --- | --- | --- | --- | --- |
| T06 delete_path | PASS | PASS | PASS | `RR-LOCAL_TEST-34C638920991350F` / `TASK-FBB183F081F994119AA9`：6 輪；首次批准逾期回 REJECTED/executed=false，唯讀確認同一空目錄後重新申請；使用者批准 `APR-3C59B60CEFA93E322B9C` 後僅一次成功刪除，關聯 VERIFY 的 file_exists expect=false PASS；第5輪缺 terminal steps 狀態，第6輪補齊後 COMPLETED/SUCCESS。測試器600秒超時不代表 Agent 已結束。 | 增加 `--collect-only`，從既有 request/task 收集最終 evidence，避免重新發送副作用；分開核對遭拒刪除與真正執行的刪除，保留過期紀錄並要求成功刪除恰好一次。Production 安全檢查維持原規則。 |

### T07 2026-10-16 新驗證

UT、IT-R、Web IT 均 PASS。正式 Web request `RR-LOCAL_TEST-124B6E35F84A4E3F` / task `TASK-99F0F7EB976732C639E1`；實際 3 個 action：begin_file_write、abort_file_write、關聯 VERIFY。OPEN/next_offset=0 的交易確實建立，取消後 manifest=ABORTED、staging 已清除、目的檔不存在；VERIFY PASS，任務 COMPLETED/SUCCESS。無需修改 production runtime。UT 另覆蓋重送不重建、身分衝突、既有檔不覆寫與不完整 expectations 拒絕。

### T08 2026-10-16 新驗證

UT、IT-R、Web IT 均 PASS。Web `RR-LOCAL_TEST-A7FB1F0CF3F4606E` / `TASK-F593BFA55ED8751C0868`；實際 begin、兩筆 chunk、abort、關聯 VERIFY 共5項。觀察器獨立讀取 staging，確認第一塊14 bytes、兩塊累計27 bytes，內容 hex/hash 與輸入UTF-8相符；取消後 manifest=ABORTED、received_chunks=2、staging清除、目的檔不存在，VERIFY PASS及COMPLETED/SUCCESS。網頁第4輪曾延遲等待 assistant 啟動，原 request 後續完成，沒有重送 chunk。UT另覆蓋錯誤offset/index/size/hash不變更、base64、身分衝突、重送去重及fixture-manager重建。無需production修改。

### T09 修正與重測

首次 Web `RR-LOCAL_TEST-BDBFE93509C374CC` / `TASK-020F836B2ACA95B86477` 已實際 commit 29 bytes及關聯VERIFY PASS，但未通過整體Web驗收：初始field_refs衝突與未知ref經模型補正後使用新action ID完成，舊blocked chunk/commit仍留在deferred清單，terminal被阻擋並要求重做已關閉交易。已取消此自建測試，保留原產物，未中斷其他Agent。

修法：只在單塊COMMITTED交易具備本request成功執行、相同write_id、完全相同payload、唯一對應、完整bytes及SHA吻合時，將未執行舊chunk/commit記為SUPERSEDED並保存proof；不冒充曾執行。多塊、錯hash、跨request、重複歧義、不同交易及失敗receipt維持阻擋。新增 `validate_committed_write_repair_reconciliation` 真Runtime loop與負向案例PASS；action_scoped_admission與progress_terminal_lifecycle回歸PASS。另補測試器collect-only、路徑resolved比較及無插值雙引號唯讀command辨識，不重播原副作用。

修正後UT及IT-R PASS；新Web `RR-LOCAL_TEST-E71CE722674E8B8A` / `TASK-8DE3FC4B84E86F58BF9C` PASS，實際begin/chunk/commit/VERIFY共4項，29 bytes，SHA256=`caad70343d51e58522b2a0cb55434f8479bf18df1edaab4e8c2508347669b6bd`，type/contains別名產生兩項真正postcondition evidence且均PASS，COMPLETED/SUCCESS。舊deferred問題的固定重現由新增真Runtime loop回歸覆蓋；此新Web未刻意注入同一錯誤，不宣稱每次自然語言均能命中特定負向分支。

每次測試另外記錄：schema／程式版本、request ID、task ID、round、action ID、fixture、原始輸入、正規化結果、拒絕診斷、執行／驗證／交付狀態、實際 evidence 與最終回覆。不得保存憑證、token 等敏感資料。

狀態使用 PASS／FAIL／PARTIAL／BLOCKED_ENV／NOT_TESTED／N/A。N/A 必須說明理由；依賴外部環境而未測不能標 N/A；QUEUED／UNKNOWN 不是 PASS。

### 2026-10-17 Composer 已送達／殘留草稿恢復

RCA：手動 T10 `RR-TELEGRAM-91ECCB0E3BE6836B` 在協議附著前遭 `composer_unrelated_draft_present` 拒絕，尚未執行 T10 action。前筆恢復只因找到唯一 submit marker 就回 ALREADY_DELIVERED，再提升為 RECOVERED_PRIOR_DELIVERY，卻未要求 composer 為空，讓下一筆寫入撞上殘留草稿。

修正 `composer_operation.py`：送達證據與 composer 可用分開判斷；送達後仍須兩次穩定 observation、非生成中；非空草稿須比對實際 primary 的完整／已記錄 prefix 雜湊，原子 compare-and-clear，清理後再次確認空值、turn 不變及非生成中。未知草稿、內容改變、歧義送達、scope 變動均不清理；父流程亦拒絕將非空 ALREADY_DELIVERED 提升為可繼續狀態。不重送已送達內容。

回歸：`validate_composer_recovery`、`validate_composer_operation`、`validate_composer_rich_link_matching`、`validate_composer_mismatch_diagnostics`、`validate_unsafe_composer_verification_bypass` PASS。涵蓋 owned／foreign primary、secondary 不可授權 primary、原子清理競態、生成中、turn 改變、重複 marker 及父流程防守。

實頁恢復：Edge/CDP 1272 同一既有對話，殘留 22456 字元 normalized SHA256=`e35dc1d8c7afc9512b5cf2b93336e6199051beadeaac18d1ea3275003b52e2c2`，與歷史 operation `22f63722ffda40ffa1652f74e5eced21` confirmed_prefix 相同；以該歷史 record 執行修正後 recover helper，得到 ALREADY_DELIVERED / sent_count=1 / OWNED_DRAFT_CLEARED / composer_empty=true。再恢復當前 quarantined record 得 RECOVERED_EMPTY。未抹除 journal 或跳過所有權檢查。

正式 LOCAL_TEST 網頁重測 PASS：`RR-LOCAL_TEST-FC549045791A6EFA` / `TASK-D034F7DD464C97571BF1`。先收到 AGENT_SESSION_STALE，再完成正常初始化；5 個規劃回覆，實際 begin/chunk/abort/VERIFY 共4項，未執行 commit、上傳或重播舊交易。chunk 接受30 bytes／1塊，abort=ABORTED、file_committed=false，staging 已移除；關聯 VERIFY 的兩項實際 postconditions 均 PASS。測試器獨立確認原檔精確為18 bytes `T10_KEEP_ORIGINAL\n`、SHA256=`06a59564f93c7ab048877e07bc22769375adbfde73086a16d5bb9db111208325`。終態 COMPLETED/SUCCESS，測試器 `T10_PHASE_PASS IT-W overall=PASS`；證據存於 `localdata/runtime_it/matrix38/t10_abort_file_write.json`。本輪不宣稱所有網頁／未知手動草稿都可自動恢復。

## 8. 驗收指標與界線

### 2026-10-17 04:11 單次接續驗證

T10 重新執行 UT、IT-R、Web IT 均 PASS（非沿用舊總狀態）。新網頁 request `RR-LOCAL_TEST-A7D286D5282158F2`、task `TASK-56B878AF8115FBE1F981`，5 輪規劃／4 個實際 action，begin → 30 bytes chunk → abort → 關聯 VERIFY；原目的檔18 bytes精確不變、staging清除、ABORTED/file_committed=false、兩項檢查PASS、COMPLETED/SUCCESS。未重新執行已完成舊交易。先前手動 request 在 transport 階段失敗與新測試通過分開保留，沒有將舊失敗改寫為成功。現場未重現新的 T10 缺陷，因此本次不另改 production。

T11～T14 現存 JSON PASS 是2026-10-14舊矩陣證據，不作為本次新版規劃直接通過依據；T10確認通過後按順序重新驗證。

本輪 T11 UT、IT-R PASS：真實執行10種 operation，涵蓋執行／驗證分離、負向exit、FAIL→PASS、GENERAL先拒絕自動結案再以關聯condition verifier通過；不是mock命令成功。網頁分組進行，只有A～D均有本輪PASS才標記本次T11通過。

| 本輪網頁組別 | 狀態 | request / task | 客觀結果 |
| --- | --- | --- | --- |
| T11-A | PASS | RR-LOCAL_TEST-D1FF37B03D27DBA7 / TASK-A6B9C7AA8EC7E0D5F676 | INSPECT→一次MUTATE→同一action的負向VERIFY FAIL→新VERIFY PASS，共4項；fixture精確為T11_ACTUAL，action-level history保留FAIL/PASS，最新有效PASS；COMPLETED/SUCCESS |
| T11-B | PASS | RR-LOCAL_TEST-A0B0BEE44E46B073 / TASK-B8ADB8B4CF681C682B90 | 正常BUILD/TEST通過；負向BUILD exit1及TEST exit7保留FAILED/FAIL，expected_failure均PASS；合成產物內容吻合且missing_build未建立；COMPLETED/SUCCESS |
| T11-C | PASS | RR-LOCAL_TEST-E9F4EB784E00DE61 / TASK-26F9007C83D178A2AECD | 2項GIT operation；合成repo一次commit、HEAD=8d071db81c849c883ba1bac97fff9e00e12a3be8，訊息及40位SHA後置驗證吻合；未push；COMPLETED/SUCCESS |
| T11-D | PASS | RR-LOCAL_TEST-7E2121A208A4AE82 / TASK-48350C04414C192C9FD3 | PROCESS/TRANSFER/GENERAL/VERIFY及一次Runtime要求的新condition VERIFY，共5項；GENERAL原始eligibility=false保留，兩筆關聯驗證PASS；拷貝bytes吻合、無副作用重播；COMPLETED/SUCCESS |

本輪 T11 完成：UT、IT-R、四組新 Web IT 全部 PASS，才開始 T12；本輪尚無需修改 production runtime。D組一次額外condition驗證保留為對話成本，不抹除原始GENERAL限制或假稱只走四項。

本輪 T12 UT、IT-R、新Web IT均PASS：`RR-LOCAL_TEST-C24D722221DDEEED` / `TASK-47E50D6C8773BAA25219`。一次MUTATE、兩筆關聯aggregate_verification；負向三項實際exit=0/1/0，正向兩項exit=0/0；每項process_started=true、checks_completed=true，負向FAIL歷史及最新effective PASS正確保留。獨立測試器確認精確`T12_ACTUAL\r\n`及must_not_touch原檔不變；COMPLETED/SUCCESS。模型未額外驗證CRLF bytes，因此此bytes證據來自測試器，而非冒充模型工具結果。本輪不需修改production。

本輪 T13 UT、IT-R、新Web IT均PASS：`RR-LOCAL_TEST-B6AFC985313A2849` / `TASK-A56B5416EA4A2B4ABEF9`。一次inspect_project_scope、scope精確為合成project；4檔/64 bytes、build_files及各檔SHA與實檔吻合。COMPLETED/SUCCESS；無父層掃描、project_sync、query、命令或上傳。本輪不需修改production。

本輪 T14 UT、IT-R、新Web IT均PASS：`RR-LOCAL_TEST-1C5572CFF156696D` / `TASK-8C0142FB93EBBAB3A410`。一次inspect_project_working_set，requested_paths及結果精確為src/a.py、docs/b.md；file_count=2，size/SHA與實檔吻合、missing_paths=[]，unrelated.txt未納入，無其他工具或上傳；COMPLETED/SUCCESS。本輪不需修改production。

- 正確 action：工具／參數／次數／scope 與預期一致。
- 等價表示：不必要補問下降，但歧義與安全衝突仍拒絕。
- 補救：一次列出可判定缺漏；修復後解除對應 blocker，不丟 action、不重播副作用。
- 等待：不導向無關讀取；交付／程序／交易有正確 lifecycle。
- 結果：獨立 oracle 與 Runtime 回覆一致；執行、檢查、交付、目標達成分層。
- 收斂：記錄拒絕率、補救往返、相同 blocker 重複次數及進展證據；不以單純放寬規則降低數字。
- 涵蓋率：分別統計適用案例已執行／通過數及 UT／IT-R／Web IT，未測與排除項公開列出。

本表能驗證的是「已列出的輸入、變形與失敗情境下，Runtime 調用與狀態可信」。不宣稱有限測試可保證任意模型回覆、任意環境皆正確，也不將 38 工具各一次 PASS 解讀為完整介面驗收。
