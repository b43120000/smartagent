# Goal 1 離線候選分支與封閉環境 Live 診斷（2026-10-29）

> **STATUS: IN_PROGRESS / NOT FORMAL PASS / NOT DEPLOYED**  
> Branch: feat/goal1-offline-20261029  
> Base: 13cfce8b74be2b47ad37298356c8750d44b82ce7  
> **不要**在 PR10-2A 未獲正式驗收前，將本分支的 5A/5B 候選當作已接受功能；不得以本分支覆蓋 Codex 目前的 shared branch、封閉環境中的 active Worker，或受保護的歷史任務。

## 1. Goal 1 的完整定義

參照獨立 planning branch 的以下原始文件，禁止用此頁縮減正式驗收：
- doc/tri_one_phase1_shared_completion_text_only_fast_path_plan.md
- doc/tri_one_phase1_verification_matrix.md
- doc/tri_one_phase1_execution_handoff.md

階段順序：PR10-2A（TaskResult compatibility + Live）→ PR10-5A（no-tool answer-only）→ PR10-5B（Telegram + WebCopilot ingress and delivery）→ Phase 1 integrated regression and formal Goal 1 acceptance。最終 main merge 仍由使用者批准。

## 2. 此分支目前已實作的離線候選（尚未核准部署）

- **5A opt-in policy**：新增 \`source/agent_core/tri_one_answer_only.py\`；由 Runtime 原始 request、結構化附件／路徑、已有 action、pending result、OPEN/UNKNOWN blocker 等做保守 eligibility 判斷；不足以判斷的自然語言任務拒絕 fast path。此 lexical allowlist 是安全候選，不是證成所有自然語言請求皆可自動分類。
- **5A Runtime gate**：在 \`source/WebAgent/protocol_loop.py\` 的正常 v9、Progress、Action State 檢查之後，僅對獨立允許的真正 answer-only terminal，不再強制建立不存在的 ActionResult PASS；Terminal Receipt 額外標記 \`ANSWER_ONLY_RESPONSE_ADMITTED\`，仍保留 evidence verdict=\`UNVERIFIED\`（無外部觀察證據）。全部預設 OFF。
- **2A/5A TaskResult 連動**：\`source/agent_core/tri_one_task_result.py\` 僅在 Runtime 真正發出 answer-only accepted receipt、沒有工具 action／artifact／evidence 且兩個 feature flags 開啟時，才能寫入 \`completion_kind=ANSWER_ONLY\`，保留 \`LEGACY_UNVERIFIED\` 與 \`EVIDENCE_BACKED\` 的區分。
- **離線測試程式**：`source/WebAgent/tests/validate_tri_one_5a_answer_only.py` 與 `validate_tri_one_5b_shared_worker.py` 涵蓋 text-only 正負例、跨入口旗標選擇、WebCopilot 內部控制文字過濾、共享 Outbox 結果／去重／交付回執。以上是**已提交的測試碼，不代表已執行 PASS**。
- **封閉環境摘要工具**：\`source/WebAgent/tests/goal1_live_triage.py\` 可選擇讀取本機 log，僅提取有限的診斷標記／筆數／hash，而不輸出原始 log、敏感 ID、路徑或原文；也可不讀 log，僅使用自然語言症狀。

**5B 共用執行路徑（本分支新增，預設 OFF）：**
`source/web_copilot.py` 的 WebCopilot 原始入口仍會使用 `AgentIngressGateway`。新的 `agent_core/tri_one_shared_worker.py` 及 `RemoteAgent/remote_worker_launcher.py` 只有在三個受控旗標明確開啟後，才把 **WEBGPT_COPILOT** 導向原本 Telegram 使用的 `RemoteAgent.telegram_webagent_worker`／`WebAgentProtocolLoop`；否則完全保留舊 `smart_agent.py` Worker，不新增 CDP Browser。共用 V9 Worker 使用原 queue/task/epoch、Progress/Action Ledger、Terminal Receipt、RemoteEventStore 與原 Telegram/WebGPT Outbox。已對 WebCopilot 源頭訊息加上 protocol control filtering，避免 Agent 自己的回覆再成為新任務。

**受控旗標／回退**：
- `TRI_ONE_TASK_RESULT_V1=1`：開啟 2A 結果契約；
- `TRI_ONE_TEXT_ONLY_V1=1`：開啟 5A Answer-only admission；
- `TRI_ONE_WEBCOPILOT_V9_V1=1`：只針對 WebCopilot 啟用共用 V9 Worker（5B）。
上述三項全開時才切換 WebCopilot Worker；關閉任何一個則維持其原本 `smart_agent.py` 路徑。Telegram／LOCAL_TEST 原本已使用 V9 Worker；其 Fast Path 另受 5A 旗標控制。啟閉時應先確保沒有正在執行的任務，不能以改環境變數強行接管舊 Worker。

**尚待證實的限制**：WebCopilot 的真正模型回覆仍透過既有 `WebGPTDeliveryAdapter`，必須 Live 確認原始對話送達、無重複接收內部 turn；artifact metadata 保留並不代表 WebGPT 已收到附件。此次改動不包含 LocalAgent 的全路徑共用協議（需 2B/2C）。

**目前驗收：** 2A 原 shared branch Live L03 曾回報 WRITER_TIMEOUT/QUARANTINED；此分支的 5A/5B 無可引用的候選 SHA 原始 Live 證據，UT/IT/Live 一律不得因 Source 存在而標 PASS。

## 3. 本機先跑純離線驗證（不要啟動 Worker/Browser）

依實際 Source branch checkout 與本機虛擬環境執行；Windows PowerShell 範例：

\`\`\`powershell
$env:PYTHONPATH = "source"
python -B source/WebAgent/tests/validate_tri_one_5a_answer_only.py
python -B source/WebAgent/tests/validate_tri_one_5b_shared_worker.py
python -B source/WebAgent/tests/validate_tri_one_2a_task_result.py
python -B source/WebAgent/tests/validate_tri_one_2a_matrix.py
python -B source/WebAgent/tests/validate_tri_one_2a_open_terminal.py
python -c "from agent_core.protocol_manifest import require_protocol_manifest; require_protocol_manifest('.'); print('GOAL1_MANIFEST_PASS')"
\`\`\`

GitHub Actions 配置檔 `.github/workflows/tri-one-goal1-offline.yml` 已加入，以 Windows runner 進行 2A/5A/5B 純離線測試與嚴格 manifest 檢查；除非取得實際 workflow run + logs，不能將已提交 CI 配置誤認為通過測試。上列為**建議測試命令**，不是本文件聲稱已執行。測試前先閱讀各腳本，以保證 fixture 不碰實際工作路徑。其餘正式 UT/IT/回歸依原 matrix 個別排定，結果必須綁 exact SHA、attempt、exit code、raw stdout/stderr hash；保留失敗歷史，不能將舊 SHA 的 PASS 合併到新 SHA。

## 4. 封閉環境的最小人工回報

若任何 Live 出現問題，**不用將整份原始 log、chat_id、帳密、私有路徑上傳**。只回報以下一段自然語言即可：

> Goal1 case：2A-L03；入口：WEB_COPILOT；Loaded Worker SHA：NOT_ATTESTED（若未知）。  
> **中斷位置**：初始化完成後，Composer 要送文字時停住。  
> **實際觀察**：顯示 WRITER_TIMEOUT 與 QUARANTINED；沒有取得有效 WEBACK 或最終回覆。  
> **最後 Runtime 狀態**：NOT_OBSERVED。Terminal Receipt：NOT_OBSERVED。Delivery：NOT_OBSERVED。  
> **有沒有重送同一請求**：沒有。  
> **採取過什麼人工操作**：僅觀察，未清理、重啟或強制 kill。

上述是**示意格式**，不是已發生事實；請只填實際觀察到的內容。若有客觀 LOG，請將原始 LOG 留在 LOCAL_ONLY，再產生安全摘要。

### 可選：一次性安全摘要（不自動連網）

\`\`\`powershell
$env:PYTHONPATH = "source"
python -B source/WebAgent/tests/goal1_live_triage.py --case 2A-L03 --interface WEB_COPILOT --source-sha NOT_ATTESTED --symptom "初始化後 Composer 超時，沒有新的回覆"
\`\`\`

如本機有相關 log，可額外加 \`--log "實際本機檔案"\`；程式僅輸出預先限定的訊號字與計數、log bytes/hash，絕不輸出原始 log 的任意欄位。需留檔時加 \`--output "新的安全摘要檔路徑"\`；若檔案已存在會拒絕覆寫。**報錯時應回傳摘要本身，非原 log。**

### 最低必備事件鏈（有就記，沒有填 NOT_OBSERVED）

- 原始 ingress：USER_SUBMITTED / REQUEST_ACCEPTED、原 interface 類型
- Worker 身分：manifest/loaded code identity（不是 repo HEAD）
- Planner：fresh round、ACK/turn_commit、任何 rejection reason
- Runtime：Progress decision/outcome、實際 operational action count、pending OPEN/UNKNOWN/blockers
- Terminal：receipt status／result completion kind
- Outbox：原 interface delivery receipt status、duplicate retry count
- 失敗點：transport、bootstrap、composer、model reply、protocol parse、tool、terminal、outbox/adapter 的哪一層

嚴禁為了補 log 重送舊 request、修改過去的證據、繞過隔離、開新 browser profile，或聲稱 local test 等同 WebCopilot 真 ingress Live。受阻時請保留 PAUSED/BLOCKED/UNKNOWN，直到可安全驗證。

## 5. 待完成的實作與驗收

1. 2A 的最新 shared branch WebCopilot L03 必須先經實際來源、載入版本和原入口驗證；無法在本分支重用前次舊 SHA L01/L02 為新 Live。
2. 5A 除了此 candidate 的 isolated UT，還需現有 V9 + Progress + Ledger + Terminal 的真 Runtime fake Planner IT，完整 negative matrix 和單輪/零工具 Live；預設 OFF 不等於完成。
3. 5B 的共用 V9 Worker 接線已在此分支提交；**仍須以 exact SHA 驗證旗標 ON/OFF、WebCopilot 真 ingress、Progress/Terminal、TaskResultV1、Outbox 及 source attestation，不得只靠 fake adapter 稱 Live PASS**。
4. Telegram 與 WebCopilot 必須個別驗證 FP01–FP08 可適用項目、delivery receipt、restart/idempotency、0 duplicate side effects。
5. 依原始矩陣在相同 frozen Goal1 SHA 上做相容性、安全回歸、Live 與正式簽核。缺少者標 NOT_RUN/BLOCKED；任何 TEST-only/LOCAL_TEST 不可以替代 Live。
6. PR11 Prerequisite Resolution v3、GAR Hybrid Recovery、Updater/Publisher/通用部署系統均非本次 Goal1 分支實作範圍。

**本文件是一份誠實的候選開發與人工回報交接，不是 Goal1 PASS 聲明。**
