# GRA v1 執行總計畫（SmartAgent 元件名稱：GAR / Goal-Anchored Recovery）

- Plan ID：`GRA-V1-20261030`；Owner：使用者；Executor：Codex（自主分析、實作、修復、驗證、提交）。
- 狀態：`PLAN_APPROVED_FOR_CODEX_TRIGGER`；**本提交僅建立計畫，尚未執行任何 Stage 1/2 工作或 Live**。
- 固定起點：`b43120000/smartagent` commit `51072718ebfe11a8e460d51134ecbc01e6bfa900`（已含 `REQUEST_ACCEPTED` C1 分類補丁）。計畫分支：`plan/gra-v1-two-stage-20261030`。
- 驗證 Source checkout：`E:\card\smartagent-dev-20261029-source`。在**該路徑**作 Git、Source、Regression 與 Live；不要另外分 worktree、不要使用舊 C 套件／舊部署。
- 第二個真實 ChatGPT 對話 WebGPT2（L4 唯一指定 target）：`https://chatgpt.com/g/g-p-6a818bab5be0819188aa5bfab333f9aa-localagentspace/c/6ae33414-a71c-83ec-9b60-c8e67417a379`。原始對話 A 必須是**另一個**既有對話、在同一 Chrome3 BrowserContext；不得把 B 當原始任務或同時交給其他 Controller。
- 使用者在另一個對話給 Codex 的啟動指令為「執行連結的 GRA v1 計畫」。此句即授權 Codex 依本文件自主執行兩階段（受下列 Gate 限制），不需反覆請求細項派工。
- 程式識別名稱維持 `GAR`，不為了使用者稱呼 `GRA` 而重命名既有 Runtime API。

## 0. 開始前：基底、Checkout 與安全約束（先做）

1. 讀本計畫全文，核對 Source `git rev-parse HEAD`、`git status --short`、上游分支與 `config/protocol_manifest.json`；不得將不同候選 SHA 的測試混為一談。以計畫所在 Git commit 為共同祖先開發；若本地有其他已存在修改，先確認來源並**不覆寫／不遺失**。
2. 若 `git status --short` 發現 `config/protocol_manifest.json` 有未提交 diff，**先依使用者指定方式**保留：
   `git stash push -m "backup manifest before GAR A0" -- config/protocol_manifest.json`
   只在該檔確實有 diff 時執行；記錄 stash ref；不得 `stash drop`／`stash pop` 而意外覆寫；其他 dirty 檔應另行安全保存或停下清楚記錄，絕不能強制 checkout/reset 清除。
3. 固定基線 `51072718ebfe11a8e460d51134ecbc01e6bfa900` 與本計畫 commit；新開開發分支，**禁止 merge/push main、force-push、修改 Update.bat／update.bat、publisher 支線**。不得影響 Edge1；Live 使用既有 Chrome3 與 user 提供的工作環境，不啟新 profile、不清除舊狀態。
4. 先檢查現有 GAR 設計文件 `doc/smartagent_goal_anchored_recovery_design.md`、`doc/gar_shared_runtime_implementation_20261029.md`、`doc/gar_f828_validation_rca_20261029.md`，與實際 Source 的落差，以**最新 Source／真實事件**為準；舊報告不是現版 PASS。
5. 嚴格區分 `LOCAL_TEST Telegram-shaped sender`、真實 Telegram 網路、WebDirect WebCopilot、Offline fake Planner、真實 Browser Live；Log 必須寫出精確通道。不得偽造 Telegram 實際收送／模型回覆／Runtime Loaded SHA。若封閉環境權限不齊，保留 BLOCKED 和證據，不能宣稱 PASS。

## 1. Phase 1：D1 可重現 Fault Injection → 真實 GAR L4 Live（第一版 Git 交付）

**目的：只模擬原 Planner 的協議錯誤；Bounded Repair、GAR L1/L2/L3/L4 決策、BrowserContext、Fresh Adapter Bootstrap、舊 Planner Fencing、Terminal Admission、RemoteAgent 回覆通道都必須使用真實既有程式。**

| 工作 ID | 具體工作 | 完成／驗收條件 |
|---|---|---|
| D1-01 | 由 Codex 實讀 `source/RemoteAgent/telegram_webagent_worker.py`、`source/RemoteAgent/local_telegram_sender.py`、`source/WebAgent/protocol_loop.py`、`source/WebAgent/fresh_context.py`；選最小的原始 Planner callback 測試接入點 | 注入確實覆蓋 **RemoteAgent Telegram 模擬發送路徑**；只改 WebAgent/controller.py 不算完成 |
| D1-02 | 新增明確 ON/OFF、預設 OFF 的**單筆／單次安全測試 Fault Injector**：由可信本地測試資訊（建議 LOCAL_TEST 任務 metadata/ticket + 固定原始請求或受限 request identity）授權，原始 Planner 返回 `GAR_D1_INVALID_V9`。勿信任使用者 prompt substring 當啟用權限 | 只攔截指定測試 request 的 original Planner，沒有 tool execution、沒有重播、沒有模型終態偽造；普通任務、真實 TELEGRAM transport 與 WEB_COPILOT 均不受影響 |
| D1-03 | 依現行 `BoundedRepairBudget` 與 GAR Engine 自然升級 L1→L2→L3→L4；不手動設定 level、不直接呼叫 `_gar_l4_activate`、不降低閾值、不重設預算 | 實際 `protocol_rejected`、`bounded_repair_observed`、`gar_local_repair_handoff`、`gar_recovery_decision` 依序可觀測；要求真實 freshness，不將 stale DOM 當 Planner error |
| D1-04 | L4 交接到 WebGPT2，切換時自動停止注入；原頁保持原 Task/Request/Epoch/outbox，檢驗原 Planner fencing、單一 Fresh Session、唯一 target ownership 與 matching SessionProtocol | 第二個預開的 ChatGPT 對話真的收到 bootstrap + fresh Planner 回合；`gar_fresh_planner_activated` 與實際 Browser 資料相符，`fresh_sessions=1`，無第二次切換 |
| D1-05 | Fresh Planner 正常完成`Explain RAII in C++.`等**無副作用文字任務**，結果經 V9/Terminal Gate，回到 RemoteAgent LOCAL_TEST 原始回覆通道；沒有 mock terminal receipt | 合法 `terminal_acceptance` 與原始 Transport outbox 完整關聯，未遺失 Task/Request/Epoch，內容符合原 Goal，沒有不受支援的 Verification PASS |
| D1-06 | 增設 Offline UT／V9 IT、負向測試（flag OFF、wrong identity、stale/UNKNOWN、無 B URL、相同 URL、頁面已占用、誤注入普通任務、注入跨 request 泄漏）並更新 Strict Manifest | Exact SHA 的新增測試 + 原有 `run_gar_regression.bat` 11 組回歸、Manifest 全 PASS；`Update.bat` 不改 |
| D1-07 | 在封閉環境**由 RemoteAgent Telegram 模擬發送端**送出普通測試 Prompt，完成一次真實 Chrome3 L4 Live；記錄本地測試結果與 Source/Runtime identity | 見下方 Live 表；不能只用 fake Planner IT 冒稱 L4 Live |
| D1-08 | Codex 自行修補失敗原因並重跑；固定通過版 SHA，**先 commit + push 第一階段**，附完整驗證摘要 | Git 分支 `feat/gra-v1-d1-live-20261030`，明確 Commit SHA、測試退出碼、Live request_id、事件證據、PASS/BLOCKED 狀態 |

### Phase 1 真實 Live 運作方法（Codex 自主查證現有命令／設定）

- RemoteAgent 的 `source/RemoteAgent/local_telegram_sender.py` 是**Telegram-shaped LOCAL_TEST** 發送端：它送入真實 RemoteAgent Queue／Shared V9 Worker，但不向 Telegram 網路發送訊息。其 worker 的 Source tag 為 `REMOTEAGENT_TELEGRAM_SIMULATION`。**不要只啟動 WebDirect Controller 或直接手貼 ChatGPT A Prompt 代替此通道**。
- 從 `E:\card\smartagent-dev-20261029-source` 啟動既有 RemoteAgent supervisor/worker，確認使用**此 checkout**的最新 Source，並由相同啟動程序繼承 `SMARTAGENT_GAR_V1=1`、`TRI_ONE_TASK_RESULT_V1=1`、`TRI_ONE_TEXT_ONLY_V1=1`（原有可用旗標依現況核對），以及精確的 `SMARTAGENT_GAR_FRESH_CONTEXT_URL`（WebGPT2 完整 URL）。D1 注入旗標必須只授權該筆 LOCAL_TEST 測項，不能全域影響任何一般任務。
- 先確認原始 ChatGPT A 的實際 routing URL **不是** WebGPT2；兩頁同一 Chrome3 profile/context、都預先存在且閒置、無既有草稿／未確認 Submit、B 沒被其他 Worker 佔用。不能強制關閉活動中的 Worker 或繞過 Lease／Owner。
- 對已存在且設定好的 LOCAL_TEST Sender，範例（Codex 要依本機實際 RemoteAgent workspace/config 驗證；`--workspace` 只在需要時設為既有受信任 workspace，不要把 Repo checkout 任意誤認成任務 workspace）：
  `python -B source/RemoteAgent/local_telegram_sender.py --request "Explain RAII in C++." --timeout 900`
  從 Repo root 執行並確保 `PYTHONPATH=source`；必要時先核對 Sender／Supervisor 的可用狀態。送件只做**新的一筆**無副作用測項，不重播先前 INTERRUPTED/PAUSED 任務。
- 首次導通後再做 flag OFF 正常任務／明確的失敗安全分支測試。不要在真實 Telegram 聊天室發測試訊息；不觸發外部文件、程式修改或通訊副作用。

### Phase 1 Live PASS 必要證據（全部逐項記錄）

| Gate | PASS 條件 |
|---|---|
| Source / A0 | 固定 SHA、實際執行 Worker 來源、Manifest loaded-code 一致；WebDirect A0 若可用另行記錄，RemoteAgent 至少要有 `source_load_attestation`。若 `source_commit_status=NOT_ATTESTED` 不能假稱 Runtime 證明 Git SHA |
| Origin | Sender 明確為 `LOCAL_TEST Telegram 模擬`，真實 RemoteAgent Queue／Worker 收到同筆 Task/Request/Epoch |
| Fault | 注入僅發生在 original Planner，可信測試旗標／單筆身份，注入次數與 V9 拒絕數可核對，原始模型未被冒稱真正失敗 |
| Escalation | 自動觀測 Bounded Repair handoff、GAR L2／L3／L4 PREPARE，預算同步且無工具副作用 |
| Browser L4 | 真實 WebGPT2 收到 SessionProtocol bootstrap、頁面唯一、Proof 通過、`gar_fresh_planner_activated`；一次 fresh session，舊 Planner Fenced |
| Completion | 真實 fresh Planner 接受 V9 Terminal，原任務正常完成，結果經 RemoteAgent `LOCAL_TEST` outbox 返回，無假 SUCCESS |
| Safety | 原 Task/Scope/Action Ledger/Checkpoint 不丟失，不重送 unknown operations、無第二次 fresh bootstrap、無安全閘繞過 |

任一 Gate 未達＝`D1_NOT_PASSED`（可細分 `NOT_TRIGGERED`／`BLOCKED`／`FAIL`）；只有全 PASS 才將 Stage 1 標為 `D1_LIVE_PASS`。保存原始本機 JSONL/log 和遮罩後的索引；Git 只放 SHA、時間、事件名稱、數量、狀態與不敏感證據，**不得 push 原始 log、cookies、URL 中的機密參數、聊天全文或 Token**。

## 2. Phase 2：D1 之後的完整 GRA v1 正式版（第二版 Git 交付）

**依賴：Phase 1 已有固定可重現的 D1 正版 SHA／測試證據。若 Phase 1 未通過，只允許修正、留下 handoff，不得跳過 D1 對 GRA v1 宣告正式 PASS。**

先新增完整技術文件 `doc/gra_v1_design_and_acceptance.md`，使以下範圍具備**明確資料結構／邊界／實作／驗收**，再由 Codex 逐項實作與測試；不要求單一大改寫，採 Source Ground Truth 的最小變更、Regression 優先。

| 工作 ID | 設計／實作目標 | Offline / Live Gate |
|---|---|---|
| V1-01 Evidence Semantics Contract | 明確分開 Reference Kind（狀態標記、規格拒絕、Action Result、Condition Evidence 等）、Provenance／request scope、Execution Finality、Verification Status、Terminal Eligibility、Pending／Blocker；避免再對 `REQUEST_ACCEPTED` 增加臨時字串特例；遷移方式與 backward compatibility 明列 | 附 cross-layer truth table + 正／負 UT，Terminal SUCCESS 只能由合法 Evidence 或獨立 Answer-only Gate 支持；`UNKNOWN` 永不變造 `PASS` |
| V1-02 GAR L0–L5 / shared budgets | 鞏固 transport freshness、bounded repair handoff、L2 Rebase、L3 Replan、L4 singleton、L5 PAUSED；保留 original Goal／Progress、所有者、回覆路由、狀態雜湊，Snapshot redaction；跨重試／切換不重置修復預算 | Offline deterministic failure matrix、無副作用重播、不能偽造完成與 Action ID 重新命名繞過 |
| V1-03 D2 — Fail-Closed / L4 Negative | 遺失或相同 Fresh URL、未預開／多頁相符、已占用頁面、生成未閒置、bootstrap／proof 身份不符、stale/uncertain submit、Action OPEN/UNKNOWN、snapshot/budget desync、soft deadline、L4 fresh 回覆又無效 | 每種均有具體 reason、Checkpoint 保留、無第二個 Bootstrap、無外部 Action 重播；安全且有權限的負向 Live 子集至少一次，不能要求真實破壞或外部副作用 |
| V1-04 Cross-origin / Delivery | 驗證 RemoteAgent `LOCAL_TEST Telegram 模擬` 與原生 WebDirect/WebCopilot 都使用共用 GAR 策略，入口各自維持身分／租約／Task Result／ACK／outbox；不把兩條不同路由的結果當成同一個 Live | 至少一筆每個受支援入口的真實 Browser Live（含正常路徑、無注入）；原路回覆與 Source identity 可追蹤。真實 Telegram 外部網路測試不得自動執行 |
| V1-05 Release / Rollback / Observability | 預設 OFF 的安全發布策略、測試旗標不滲入正式路徑、必要 Log schema、部署前／後 Source hash、可逆回退與已 PAUSED 任務的保全、已知限制和失敗處置；禁止直接自動上線或 main merge | Exact SHA 全量 Offline + Strict Manifest + Live Matrix；D1/D2/各入口完成，所有 FAIL/BLOCKED 顯式記錄；文件與實測對齊 |

Phase 2 最低文件交付：
- `doc/gra_v1_design_and_acceptance.md`：架構、Classification/Terminal/Recovery 權責、Stage 1/2 acceptance matrix、D2 安全案例、Live 步驟、API/Schema/Flags、部署回退與已知限制。
- `doc/gra_v1_release_verification.md`：固定 SHA、變動列表、實際執行的 UT/IT/Manifest 命令與退出碼、兩路 Live request/task/epoch 追溯、L4 bootstrap、Terminal/PAUSED、負向 Gate、正式版判定 `PASS / NOT_PASSED / BLOCKED`。
- `doc/gra_v1_execution_handoff.md`：Codex 進度、當前 SHA、尚未完成項與下一命令（見額度管理規則）；若完成可記 `COMPLETE`。
- 如需更動現有 `doc/smartagent_goal_anchored_recovery_design.md`，保留歷史候選描述並明確區分新正式 v1，不得以新文件消除舊失敗紀錄。

第二階段完成後**再** commit + push `feat/gra-v1-completion-20261030`（以 Phase 1 固定通過 SHA 為祖先），連結 commit 與全部驗證報告。`GRA v1 正式候選可交付`≠`已 merge/main/production rollout`。若 Gate 失敗仍可 push 有明確 NOT_PASSED 標記的 WIP/修復記錄，不能標成正式 PASS。

## 3. 兩個 Git 里程碑／驗收表

| Milestone | Git 分支 | 完成標準 | Push 規則 |
|---|---|---|---|
| M0 Plan | `plan/gra-v1-two-stage-20261030` | 本文件、基準與範圍固定 | 本次僅 Plan commit；**不代表測試通過** |
| M1 D1 L4 Live | `feat/gra-v1-d1-live-20261030` | D1-01～08，真實 B bootstrap + fresh planner terminal + 原路 LOCAL_TEST 回覆 + 全量 Offline | 固定第一版 Commit SHA／Stage 1 Evidence，上傳 Git 後才開始 M2 |
| M2 GRA v1 | `feat/gra-v1-completion-20261030` | V1-01～05、正式設計／驗證文件、Offline + 正反向 Live 矩陣、release/rollback | 固定第二版 Commit SHA／Formal Acceptance，**不 merge main、不預設開啟正式功能** |

Codex 有權自行在範圍內分析、改 Source、調測試、重跑安全測試及提交多個小 commit；若設計與現有 Source 不吻合，先記錄 RCA，再採最小可驗證修正。**禁止用降低 Gate、人工捏造事件、直接設置 recovery level、跳過真實 L4 或同一舊 Request 重播來換 PASS**。

## 4. 5 小時額度／交接規則（重要）

- 每一輪執行紀錄包括時間、分支、SHA、測試狀態、下一件未完成工作、已確認風險。
- **當 5 小時額度可見剩餘 ≤5%**（或工具顯示接近限制），立即停止新增大型 Source／啟動新 Live，安全完成當前原子動作，將 10～20 行簡短交接寫入 `doc/gra_v1_execution_handoff.md`，**至少包含**：
  - `Last known clean commit / branch / worktree dirty files / stash ref`；
  - `Current milestone, first unmet Gate, exact blocker`；
  - `Offline & Live actual PASS/FAIL/NOT_RUN`，最後的 request/task/epoch 和不敏感 log 路徑；
  - `No replay / pending unknown side effects / active Browser owner`；
  - `Next 1–3 exact commands, resumed schedule or next testing window`。
- 若額度不可直接讀取，不得虛報百分比；遇到限額／tool 中斷徵兆或離開工作會話前也先更新交接。能安全 commit/push 文件就提交至目前里程碑開發分支，**不可把未通過版標記 PASS**。
- 不要承諾在沒有排程工具時會自行背景續跑。Codex 下一回合應從交接檔與 Git 現況恢復；已 INTERRUPTED/PAUSED/UNKNOWN 任務不可無授權重播。

## 5. 最終回報格式（兩階段各一份）

`MILESTONE | BRANCH | EXACT_COMMIT | BASE_SHA | CHANGED_FILES | OFFLINE_PASS_FAIL_NOTRUN | STRICT_MANIFEST | LIVE_ORIGIN | LIVE_REQUEST_ID | L2_L3_L4_OBSERVED | FRESH_BOOTSTRAP | OLD_PLANNER_FENCED | TERMINAL_OR_PAUSED | SIDE_EFFECT_REPLAY=NO | BLOCKERS | NEXT_STEP`。

將完整報告連結及固定 SHA 提供使用者。只有真正執行且可回溯的測試才能填 `PASS`；否則填 `NOT_RUN`、`NOT_TRIGGERED` 或 `BLOCKED`。
