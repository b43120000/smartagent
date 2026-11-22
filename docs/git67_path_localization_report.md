# Git67 Path Corruption Localization Evidence Report

調查日期為 2026-11-19，時區 Asia/Taipei。主要驗收 Run ID 為
`git67_path_localization_20261119_04`。

基準分支為 `git67/case-c-action-contract-repair`，基準 Commit 為
`9e370d46052dcc3e9d6e6902a0fe6bc37fcf43f0`。
開始時本機位於乾淨的 Git68 `797e182c565fe9894b729ac8c784e3a8d6902674`。
切換至現有 Git67 後，以 fast-forward 取得指定 Sidecar Commit。

本次只新增隔離 Harness、UT、測試 Evidence、報告及 Sidecar CI 項目。
正式通訊、Parser、Continuation、Executor、安全政策與正式 Fixture 未修改。
既有 `sidecar_git67_reproduction.restore_once`、Git67 真實函式及 IT10 recorder
均有實際呼叫。所有任務建立只發生在 TemporaryDirectory 內。

## Evidence 索引與判讀

- [完整檔案 SHA256 索引](evidence/git67_path_localization_20261119_04/index.json)
- [機器可讀 Evidence Matrix](evidence/git67_path_localization_20261119_04/matrix.json)
- [控制文字、Raw 模擬 HTTP Body 與觀測樣本](evidence/git67_path_localization_20261119_04/captures.json)
- [執行摘要、IT10 樣本與真實歷史重播](evidence/git67_path_localization_20261119_04/summary.json)

Matrix 的 PASS 表示該列的輸入與輸出片段保真，不表示路徑正確、獲得寫入授權、
工具執行成功或 Live 任務成功。故意提供的錯誤路徑也應原樣保留。
每列都有 Run／Case ID、適用的 Message／Update／Task／Request ID、
Source Function 與行號、Input／Output SHA256、字元及位元組數、code points、
反斜線位置、Hex、Evidence Source、檔案引用與結果。

主驗收共 165 列觀測。

| 類別 | PASS | INCONCLUSIVE | FAIL |
| --- | ---: | ---: | ---: |
| SIMULATED | 129 | 7 | 0 |
| HISTORICAL | 14 | 0 | 0 |
| LIVE | 0 | 15 | 0 |

Live 列皆為未執行的觀測缺口，實際 Live 測試數為 **0**。
本地實跑六種正確輸入形式及一組錯誤輸入，共七組；
另有 18 組固定合成回覆擷取，為六種形式各重複三次。
歷史實跑為兩筆 Task，使用其真實 Ledger 各恢復一次。
這些數量只計主要驗收 Run，先前 Harness 建置嘗試不合併統計。

## S0 — Ground Truth

| 問題 | 實測回答 |
| --- | --- |
| Q0.1 | 正確路徑 26 字元，U+005C 位於零起算位置 2、10。錯值 25 字元，只在位置 2 有 U+005C。 |
| Q0.2 | 本次 Harness 以 chr(92) 組合，進入模擬 Transport 前即驗證 Hex。歷史兩筆的發送前原文仍缺失。 |
| Q0.3 | 有。Golden Input 的 .txt 與 .utf8.bin 檔各自保存實際 UTF-8 bytes，來源獨立於對話 UI。 |

正確 Golden SHA256 為
`15824c0b99e164dd97180ef8b6605463378fa06c2ec17722baf1dbe345687577`。

錯誤對照 SHA256 為
`8cf9e689c7fa49adf886b5685081799f132bf27bfae879530c78050ccd92bfe9`。

```text
GOOD E:\AIspace\.case_d_backups
45 3a 5c 41 49 73 70 61 63 65 5c 2e 63 61 73 65 5f 64 5f 62 61 63 6b 75 70 73

BAD  E:\AIspace.case_d_backups
45 3a 5c 41 49 73 70 61 63 65 2e 63 61 73 65 5f 64 5f 62 61 63 6b 75 70 73
```

同時實測 `json.loads(json.dumps(GOOD)) == GOOD`。
Raw JSON 中的雙反斜線與解碼後的一個 U+005C 分別記錄，沒有用視覺顯示判定字元數。
S0 為 PASS。

## S1 — Telegram

| 問題 | 實測回答 |
| --- | --- |
| Q1.1 | 產生並保存模擬 HTTP Response Body，經真實 TelegramBotClient.get_updates/_call，保留 Golden Input。Live T1 未取得。 |
| Q1.2 | 真實 HTTP UTF-8／JSON 解碼後 T2 保真，七組控制皆通過。 |
| Q1.3 | 歷史缺少發送前文字、原始 HTTP Body 與輸入方式證據，無法區分 App、複製貼上、上游渲染或輸入原文。 |

IT10 實際以受控合成資料啟用一次並成功寫出 path code points、hash 與
Message／Update identity。只在這個 Sidecar process 暫時設定診斷旗標，
結束後還原，沒有修改 ACL 或正式 Runtime 設定。
診斷 ACK 是合成 fixture 設定，並非對 Live 資料目錄作 OS 權限驗證。
受控 Trace 樣本已保存在 summary.json。

IT10 的 `02_telegram_message_text` 呼叫位於 TelegramUpdateAdapter.normalize，
此時 TelegramBotClient._call 已完成 `json.loads(response.read().decode("utf-8"))`。
因此 IT10 的這個階段不能冒充 Raw HTTP Body。
目前缺少專用的 Live raw-body collector，也沒有可對帳的 Live sender bytes。

最小追加觀測需要一個隔離 receiver 的 opener／response tee，或 _call 中解碼前的
受控 callback。只在專用測試 inbox／時間窗收集已知 Case ID 的 Body，
對同批非測試訊息拒絕保存，避免將私人對話納入 Evidence。
使用原始 Body 時需保留其 bytes 與選定 Update 的映射。
本次沒有實作正式通訊 Hook。

S1 為 SIMULATED PASS／LIVE INCONCLUSIVE。

## S2 — Ingress 與持久化

| 問題 | 實測回答 |
| --- | --- |
| Q2.1 | 真實 TelegramUpdateAdapter.normalize 保留所有測試字串，只處理首尾空白。 |
| Q2.2 | 真實 AgentIngressGateway.accept 保留 original_text；以受控 sys.settrace 取得 return frame 的 local 值。 |
| Q2.3 | 真實 TaskStateStore 暫存寫入、重新讀取後，request、metadata.source_text、metadata.source_user_command 完全一致。 |
| Q2.4 | 七組未觀察到刪除反斜線。入口的 strip、一般字典組裝、JSON dumps／loads 均保真。 |

隔離點僅為 Workspace 選擇，返回本次暫存目錄，避免碰觸正式 Task Store。
Gateway、Session Router、Queue 與 JSON State IO 均使用真實程式。
這項隔離不授予 Runtime Action 權限，也沒有啟動 Worker。
每筆使用獨立 Message／Update／Task／Request identity。

T2～T6 的實測為 PASS。T7 沒有偽裝成已執行 Worker，
七組均列 INCONCLUSIVE。歷史 A／B 另有真實 T7 紀錄可供對帳。

相關 Source Function 與行號逐列保存在 Matrix。
主要函式為 TelegramUpdateAdapter.normalize、AgentIngressGateway.accept、
RemoteTaskQueue.enqueue_remote_request 與 json_state_io.write_json_atomic。

## S3 — Prompt 與 Composer

| 問題 | 實測回答 |
| --- | --- |
| Q3.1 | 隔離 Task Reload 保真。歷史 Worker Request 為錯值，且與保存 Request 的 SHA256 一致。新 Live Worker 未啟動。 |
| Q3.2 | 真實 _prompt 及真實 tool_catalog 產生的 Original user request 區段保真。 |
| Q3.3 | 本地隔離 Chrome、真實 Git67 預設 _write_continuous_prompt 完成寫入，read_composer_snapshot 讀回完整 Prompt 一致。 |
| Q3.4 | 六組 T8～T10 未觀察到移除 U+005C，已保存完整受控 Prompt、value、innerText 與 textContent 的不同表示。 |

Browser editor 是受支援的原生 textarea，沒有冒充 ChatGPT ProseMirror。
textarea 的 value 才是已輸入內容，innerText／textContent 的空值另行保存，
不視為字元遺失。瀏覽器所有網路請求均攔截中止，沒有 Send。

初期 Harness 曾以舊 staged writer 搭配泛用 contenteditable，發生 chunk readback
無法確認。當時尚未發布完整 Evidence，也沒有定位到 U+005C 差異。
正式驗收改用 Git67 helper 實際選擇的 continuous writer 與原生 textarea。
這項 Harness 邊界明確保留在 summary.json，不宣稱已驗證 provider editor。

S3 為 ISOLATED DOM PASS／LIVE INCONCLUSIVE。

## S4 — Model Response

| 問題 | 實測回答 |
| --- | --- |
| Q4.1 | 未實際呼叫模型，無法判定模型是否在收到正確 Prompt 後生成錯值。 |
| Q4.2 | 合成 JSON 經真實 read_answer_text 擷取保真，共 18 組。歷史瀏覽器回覆本身已是錯值。 |
| Q4.3 | 測到合成 HTML pre/code DOM 擷取；未測 ChatGPT 服務端原文、Markdown Renderer 或 Telegram App。 |

三次 Live 模型嘗試位置各有 T10/T11/T12/T13 缺口列，
全部為 NOT_EXECUTED／INCONCLUSIVE，沒有以固定回覆冒充模型參與。
S4 保持 INCONCLUSIVE。

## S5 — Parser、Canonical Action 與 Preflight Input

| 問題 | 實測回答 |
| --- | --- |
| Q5.1 | Sidecar Response Text 的 arguments.path 使用受控 Golden Input／錯值對照。 |
| Q5.2 | 真實 parse_minimal_decision_with_diagnostics 保真，normalization=NONE。 |
| Q5.3 | 真實 admit_minimal_action 建立的 Canonical Action 保真。 |
| Q5.4 | 在 preview_tool_scope 邊界攔截實際 Action，捕獲 path 後固定拒絕 SIDECAR_CAPTURE_ONLY。 |

T13～T16 共七組、28 列 PASS，意義僅為 path fidelity。
沒有執行實際 Scope Authorization，也沒有派發 create_directory。
歷史實際 scope_rejected:PATH_OUTSIDE_AUTHORIZED_ROOTS 紀錄另外保存，
其拒絕結果沒有被改寫。

## S6 — Differential Controls

| 問題 | 實測回答 |
| --- | --- |
| Q6.1 | 純文字、Inline Code、Code Fence、JSON、Join-Path 描述、chr(92) Golden 六種形式均在可觀測離線階段保真。 |
| Q6.2 | 未操作 Telegram App 或 ChatGPT 複製貼上，因此兩者仍未排除。 |
| Q6.3 | 測試僅對指定路徑及缺字元對照建立結論，未推廣到所有 Windows 路徑。 |
| Q6.4 | 新 Task 入口與 Continuation 修訂文字解析均保真；真實恢復另有獨立 KeyError。 |

Join-Path 只作為原始文字比較，沒有執行該 PowerShell 命令。
JSON 形式同時保存 Raw 模擬 HTTP 與欄位 Decode 後的 path，避免混淆表示法。
Inline Code／Fence 測試不宣稱包含外部 Markdown 引擎。

## S7 — Historical Reconciliation

| 問題 | 實測回答 |
| --- | --- |
| Q7.1 | 離線保真結果不能解釋歷史上送達 T5 之前何處丟字，只能確認未重現轉換。 |
| Q7.2 | 是，最早可核實的錯值仍在 metadata.source_user_command／持久化 Request。 |
| Q7.3 | 否，缺少原始 sender 與 Live HTTP Evidence，根因維持 UNKNOWN。 |
| Q7.4 | 是，使用兩筆真正歷史 Continuation 記憶體副本，皆在同一正式程式行號重現。 |

兩筆真正 Request 的 SHA256 均符合
`01ba6338d521f40c25e8f56c274a73562d6f1147273d32fdbd0c721914db741a`。
source_user_command、source_text、request 與 Worker request_started.request
逐字相同。回覆中的 create_directory JSON、Parser 結果與 Admission arguments
也相同，normalization=NONE。

summary.json 保存實際來源檔案 hash、Request hash、原始回覆的受限 JSON、
回覆行號、Admission 回合、拒絕理由與原始檔案索引。
不保存完整私人需求、Token、Cookie 或其他對話。

Continuation Restore 使用既有 restore_once 呼叫真正的
WebAgentProtocolLoop._restore_continuation。
Task A 的 12 筆及 Task B 的 2 筆真實 results 缺少 action_digest，
對應 actions 均有 signature。兩者均重現
`source/WebAgent/protocol_loop.py:1110` 的
`KeyError: 'action_digest'`，保留 traceback，executed_tools=0。
兩個正式 JSON Store 在重播後再次比較，bytes 完全相同。

後續修訂的 HISTORICAL followup_revisions 也保存在摘要。
第一筆接續的 effective_goal 仍含錯值；後一筆已含正確值且 goal_changed=true，
但同樣在歷史 Ledger 恢復階段中斷。這支持先前 RCA 的兩個問題相互獨立。

## 結論與下一個測試

```text
PATH_CORRUPTION_FIRST_CONFIRMED_STAGE=T5_source_user_command_ALREADY_BAD
PATH_CORRUPTION_FIRST_MISMATCH_INTERVAL=UNOBSERVED_SENDER_INPUT_TO_T5
PATH_CORRUPTION_ROOT_CAUSE_CLASS=UNKNOWN_INSUFFICIENT_EVIDENCE
CONTINUATION_KEYERROR_REPRODUCTION=REAL_HISTORICAL_LEDGER_REPRODUCED
RCA_CONCLUSION_CONSISTENCY=CONSISTENT_WITH_PRIOR_UNKNOWN_AND_INDEPENDENT_RESTORE_FAILURE
```

沒有任何已確認的相鄰歷史觀測能顯示「正確字串 → 錯誤字串」。
FIRST_CONFIRMED_STAGE 只表示最早存到錯值，並非已定位致錯函式。
Matrix 的 First Mismatch Indicator 因此全部為 false。

仍未排除發送前輸入／上游渲染／複製貼上、Telegram 傳輸及未被保存的入口區間。
目前證據不支持將模型、Parser 或 Markdown 引擎指定為已確認的致錯原因。

下一個最有資訊價值的測試為專用測試 sender 直接以程式產生 Golden Input，
保存發送前 bytes 與發送收據，同時在隔離 receiver 收集對應 Raw HTTP Body、
T2 message.text 與 T3 Normalized Text。以同一 Case／Message／Update ID 對帳。
需要明確的受控 sender 及只含測試訊息的收集窗口；
目前 IT10 可補充 T2 後的觀測，但無法補回既有的歷史 Raw Body。

本次沒有執行 Live Telegram／模型，也沒有提出任何 Live PASS。
先提交測試及 Evidence，正式問題維持原狀，等待後續決定。

## 重跑方式

在 Repository 根目錄，選擇不存在的新輸出目錄，以免覆蓋證據。

```powershell
$env:PYTHONPATH = 'C:\smartagent\source'
C:\smartagent\.venv\Scripts\python.exe -B -m WebAgent.tests.sidecar_git67_path_localization --history-root C:\smartagent --output C:\smartagent\docs\evidence\git67_path_localization_new_run
```

完整 browser 隔離測試需要 Playwright 與已安裝的 Chrome／Edge，
或透過 SMARTAGENT_OFFLINE_BROWSER 指定開發用 browser。
--no-browser 會保留 browser 未執行狀態，不能據此宣稱 S3 PASS。
