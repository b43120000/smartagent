# SmartAgent Goal-Anchored Recovery (GAR) — Hybrid Recovery 設計提案

> **狀態：DESIGN / NOT IMPLEMENTED**  
> **決策日期：2026-10-29**  
> **決策：採用 Hybrid Recovery（Same-Conversation Recovery → Fresh Planner Context）。**  
> **定位：長任務可靠性設計；與 PR10-2A → PR10-5A → PR10-5B → Goal1 → PR11-A/B/C/D 既定驗收序列分離。**  
> **範圍：本文件只有方案規劃，不授權 Source 修改、Local/Live 測試、變更 ACL/Updater/Publisher、部署或 main merge。**

## 0. Executive decision

GAR 的**唯一最終成功判準**是：SmartAgent 在原始使用者授權範圍內，完成原始任務中所有**必備成功條件**，且每項條件都有符合其驗收規格的 Runtime 證據。

GAR 的中間目標是恢復有效規劃；「模型不再重複文字」、「成功重新連接網頁」或「產生 final_response」**都不是**原始任務成功的充分條件。

核心原則：

1. **Goal 不回退**：原始需求、不可變的限制、授權、必備成功條件由 Runtime 保存；Planner 不能以 Replan、摘要或新對話擅自刪改。使用者明確修改需求時，另走經審核的目標版本變更，而非 GAR 自行修改。
2. **Evidence 不回退**：已驗證的 Tool 結果、執行身分、Receipt、OPEN/RUNNING/QUEUED/UNKNOWN 及歷史 FAIL 必須保留。不得藉由模型換 Session、Action ID 或文字改寫，重新聲稱操作未執行。
3. **Plan 可以回退**：可回到最後一個有證據支持的規劃節點，只重規劃**未完成的工作**；這不是回滾真實世界或重新執行舊工具。
4. **Runtime 是唯一權威**：Planner 只能提出候選方案；Action admission、scope、execution identity、verification、terminal acceptance、所有恢復預算由 Runtime 裁決。
5. **失敗可以誠實停止**：若無授權、無客觀證據或安全可行的下一步，維持 BLOCKED／UNKNOWN／PAUSED／FAILED／PARTIAL 等真實狀態，不得無限重試或假稱 SUCCESS。

## 1. 問題與預期改善

已知現象：模型回覆不完整；Progress 或 tool envelope 必填欄位缺失；模型反覆給相同內容；已收到 Runtime rejection 卻持續沿用相同策略；目前常透過重新初始化模型對話嘗試脫困，效果不穩定。

**需先排除非模型原因**：

- Transport/DOM 重複讀取上一輪輸出、舊 round/ACK、回覆尚未生成完成：這不是模型重新思考失敗；應由 Transport 完成 fresh-response identity 與完整性檢查。
- 解析／Schema 錯誤：若可精確修復，先採最小欄位補正，避免丟掉原上下文。
- 真正的推理／規劃停滯：經多次獨立新回覆，模型仍未產生符合 Runtime 限制的新行動、解除 blocker 或增添有效證據，才進入 Rebase/Replan/Fresh Context。
- 真正的外部阻礙（權限、缺少資源、工具故障、未決定的寫入或網路交付）：切換對話不能代替客觀補救或使用者核准。

GAR 目標是**提高原始任務驗收完成率**，並同時確保重複副作用與虛假 SUCCESS 不增加。此為設計假設；**尚無 GAR 的實測成功率**，不預先宣稱 Fresh Context 優於 Same-Conversation 的百分比。

## 2. Task、Planner Session、Runtime 的責任切割

- **Original Task / Goal Contract（Runtime-owned）**：保存原始使用者需求、要求的可驗證成功條件、限制、owner/identity、授權範圍及版本。
- **Task Ledger / Evidence / Checkpoint（Runtime-owned）**：Progress、Action Result、正負證據、terminal receipt、pending transaction、blocking dependency、不可重播的副作用、最後安全檢查點。
- **Planner Session（可替換）**：用 Runtime 提供的可信狀態，選擇下一個合法的規劃步驟。其回覆不是執行證據。
- **Transport（可重新連線）**：傳送／讀取模型回覆，證明 fresh response 身分及完整性；不能把舊 DOM 回覆當成新 Session 的推理結果。
- **Runtime Admission / Executor**：決定能否執行、如何處理 UNKNOWN、何時解除 blocker，並核發被允許的 terminal acceptance。

**規劃上的邏輯關係：** 一個 Original Task 可以由多個 Planner Session 接續，但每次 session transition 必須具備完整 owner/request/task/epoch lineage 與防重播保護。若現有 Task Continuation 需要新 request/task/epoch，必須明示父子關係、沿用原 Goal Contract 並執行現有授權檢查；不得因為本文件的抽象「同一 Task」而繞過既有身份模型。

## 3. Hybrid Recovery 分層狀態機

| 層級 | 觸發／用途 | Runtime 行為 | 預期結果 |
|---|---|---|---|
| L0 **Transport Integrity** | 舊 DOM、ACK/round 不匹配、未完整傳回 | 對照 request/round/ACK/turn_commit、transport completion；必要時等待或安全中斷，不呼叫 Planner 重答 | 確認是否真的有新模型輸出 |
| L1 **Minimal Repair** | 缺欄位、可無歧義 Schema mismatch | 保留合法、未執行的原 action batch；限定欄位／格式修復；仍經完整 admission | 合法 ACK 或明確 rejection |
| L2 **Progress Rebase** | 模型持續誤解目前狀態或重複無效答案 | 將原始 Goal、最後已接受 Progress、有效證據與 blockers 重新輸入**原 Planner Session**，要求定位下一步 | 新的合法且可觀察進展的候選方案 |
| L3 **Bounded Replan** | 原策略卡住、L2 未產生客觀進展 | 回到最近 Verified Checkpoint，僅替換未完成的後續 Plan；保留歷史 action/rejection 的精簡負面策略資訊 | 不重播已完成操作的新策略 |
| L4 **Fresh Planner Context** | L3 仍停滯，且 snapshot 完整、目前副作用安全可接續 | 使用新推理上下文，只注入 Runtime 驗證的 Recovery Snapshot；禁止將舊錯誤回答全文當成新權威 | 新 Session 在相同原始 Goal 下續作 |
| L5 **Safe Pause / Human Escalation** | 無合法新策略、預算耗盡、證據或身份缺失、外部核准必要 | 持久化 PAUSED/UNKNOWN/blocker 與恢復所需資訊；不宣告原任務 SUCCESS | 安全且可審查的未完成狀態 |

**轉換策略：** L0 先於一切模型修復；L1/L2/L3 適用範圍不同，不必每次全部通過；只有可客觀定義的持續無進展才升級 L4。沒有真正的新證據，不能用「重新生成 Action ID／換用詞」清空計數器。跨 L1–L4 的總時間、token、成本與 retry budget 必須共享，不能透過新 Session 歸零。

若任何階段已符合原始 Goal Contract 且通過 Runtime 驗收，可以正常結束，不需再進更深層 Recovery。

## 4. Recovery Snapshot V1（提議格式）

這是**擬議的資料契約**，目前尚非已存在或部署中的 schema。

| 區塊 | 必要資料 | 權威與規則 |
|---|---|---|
| \`goal\` | original_request_ref、goal_version/hash、必備 success criteria 與實際驗證要求 | Runtime-owned；不能由 Planner 自行縮減 |
| \`identity\` | owner、original_task_id、current_task/request/epoch、planner_session_id、lineage、workspace scope ref | 需符合現有 request ownership/scope，不允許越權 |
| \`checkpoint\` | checkpoint_id、ledger_generation、最後一致的 Progress version、已驗證的完成節點 | 必須由持久化紀錄重建，禁止根據模型自述虛構 |
| \`verified_evidence\` | action/result refs、verification、receipt、來源及有效性 ACTIVE/STALE/SUPERSEDED | 正負證據都保留；必要時可唯讀查證引用 |
| \`pending_operations\` | OPEN/RUNNING/QUEUED/UNKNOWN/PARTIAL 及待核對副作用 | 不假設「沒回應＝沒執行」；不可盲重送 |
| \`remaining_work\` | 尚未完成的目標條件、依賴項、當前 permitted next actions | 從 Goal 與 Ledger 算出，不以舊回答當證據 |
| \`blockers\` | blocker_id、reason、owner、客觀 release_condition、允許補救 | 不允許 Planner 自改 satisfied=true |
| \`failed_strategy_summary\` | 被拒絕策略的 normalized signature、原因、嘗試次數、不可再用的方式 | 僅精簡失敗理由；不要重放整段錯誤推理 |
| \`recovery_budget\` | 累計拒絕、無進展次數、Session 切換數、期限、剩餘資源 | 在所有 Session 間持續有效 |
| \`integrity\` | schema_version、snapshot_id、content_digest、created_at、journal reference | Runtime 驗證版本與內容一致性 |

可供 Planner 讀取的 Snapshot 須經**資料最小化與敏感資訊遮罩**；內部 ledger 保留必要的真實身份/路由資訊，但不將 token、chat_id、auth secret、未授權檔案或任意 log 原文塞入模型上下文。原始資料不可驗證時要標示 UNKNOWN，不能合成可信值。

## 5. 最小恢復提示的語意契約（不是現成 Prompt）

- 這是**同一原始任務的接續**，不可自行換目標。
- 先列出已由 Runtime 驗證的完成條件，以及尚未完成的條件；不得把意圖或文字當證據。
- 指明前策略卡住的客觀理由；**至少提出一個可被 Runtime 檢查的不同策略**，或誠實表示沒有可用策略。
- 只選擇 \`allowed_next_actions\` 與原授權範圍內的動作；任何動作仍要通過標準 v9/Task/Tool admission。
- 不允許重播已執行 mutation，亦不可盲重送 UNKNOWN／OPEN／QUEUED 的動作。
- 最終回覆仍必須經原始 Goal Contract 驗收；缺少證據時不得 SUCCESS。
- 不要求模型揭露私有推理鏈；只需要可檢查的策略摘要、候選下一步、預期依據與阻礙。

## 6. Verified Checkpoint 與 Replan 規則

1. **Checkpoint 選取**：以最後完整持久化且 identity/ledger/evidence 一致的節點為準；不是「往前退 N 個聊天訊息」或按模型臆測選擇。
2. **計畫回退，執行不回退**：已完成/已驗證的 action 不得被新計畫重跑；即使重新規劃，也需引用舊 evidence。
3. **待決副作用的優先序**：先客觀查明進行中/未知的寫入、command、交付或其他 mutation，未解除前不得聲稱完成。對無法觀察的外部副作用必須停在 UNKNOWN。
4. **Semantic no-progress**：比較 normalized blocker、未完成條件、允許操作、有效證據 generation 與後置狀態；單純新 Action ID、新 round、文字差異或 \`executed=true\`（但 blocker 未解）**都不等於目標進展**。
5. **所有候選重新經過 Admission**：不得把 GAR 當成既有 Scope/Progress/Terminal/Action Loop guard 的快捷通道。
6. **同任務併發控制**：新舊 Planner/Worker 必須有明確的 lease/fencing token 或等價單一執行權；舊 Session 延遲回覆要拒絕，而不是納入新證據。
7. **Snapshot 舊化**：Ledger 改變、owner/epoch 不符、外部執行未決或 hash 不一致時，舊 Snapshot 不可直接推進 mutation；需重建或安全暫停。

## 7. 何時切 Fresh Context？

**候選規則（待實驗，不是正式閾值）：** 已確認至少兩次不同 round 的**新生成回覆**，經一次最小修復及一次 Progress Rebase/Bounded Replan 仍無可驗證進展、並重複同一類 normalized blocker 時，才考慮 L4。實際閾值要與既有 bounded repair 預算協調。

L4 前置 Gate（全部必備）：

- Transport 已證明回覆是 fresh，而非舊 DOM/ACK 被重讀。
- 原始 Goal Contract、授權、身份 lineage、完整可檢查 Ledger 仍存在。
- 無未受保護的 in-flight mutation；對 OPEN/UNKNOWN/QUEUED 有明確查證/保護機制，或採僅唯讀恢復。
- 可對已完成 action 做去重與副作用保護；舊 Session 已撤銷提交新 action 的權利。
- Snapshot 可驗證、符合目前 Runtime 版本；沒有新 Session 身份混淆。
- **存在受支援且已驗證的 Fresh Context 建立方式**。若網頁或 API 不提供可靠的新會話/關聯/接管機制，不能假裝清空模型記憶；只可用 L2/L3 或安全 PAUSED。

Fresh Context 不一定意味著「新建 ChatGPT 網頁聊天」。其定義是：新 Planner 推理上下文被證明不含不必要的舊錯誤回覆，同時保有原 Task 的可信 Runtime 狀態。如何透過實際 browser/transport 實現，需獨立能力探查及 IT 驗證，不能先作可用假設。

## 8. 與現有 SmartAgent 模組的整合草案

| 既有模組／文件 | 可重用基礎 | GAR 新增設計責任 |
|---|---|---|
| \`agent_core/task_progress.py\` | Runtime 持久化的 Goal、Progress、修復歷程 | Goal Contract 版本與未完成條件投影；不覆寫原始合同 |
| \`agent_core/action_loop_state.py\` | blockers、allowed actions、pending lifecycle | 為 Recovery Snapshot 提供 objective blocker/next-action projection |
| \`agent_core/bounded_repair.py\` | 拒絕回合 budget、同類 blocker stagnation | 跨 Planner Session 共用的總恢復預算及語意進展判定 |
| \`WebAgent/protocol_loop.py\` | admission、Progress repair、evidence/terminal guards、PAUSED | GAR 狀態調度；不得建立可繞過正常 admission 的特例 |
| \`agent_core/task_continuation.py\` | 任務接續、執行前 journal、歷史/identity、防重播 | 定義同 Task 多 Session 的 lineage 與遺失結果恢復契約 |
| Transport／Browser adapter | 既有 round、ACK、DOM/輸出接收 | fresh-response provenance、舊回覆識別、session fencing 與可用性驗證 |

關聯文件：
- \`doc/runtime_loop_resilience.md\`
- \`doc/runtime_it16_prerequisite_bounded_repair_design.md\`
- \`doc/shared_task_continuation.md\`

上述為**預計整合點**，不是宣稱這些模組目前已具備 GAR 所有能力。實作前必須對正在開發的產品分支重新比對 API、schema 與 Task Continuation 行為。

## 9. 驗收與成功率定義

**唯一最終主指標：Original Goal Completion Rate**

\[
\mathrm{OGCR} = \frac{\text{所有原始必備成功條件均經 Runtime 驗收的任務數}}{\text{納入評測的原始任務總數}}
\]

每筆任務的必備條件與可選條件必須預先釘選，不能在 Recovery 後縮減分母或更改原始成功定義。合法的客觀負向驗證（例如「目標檔案不存在」且已證實）也可構成成功條件，不能一律要求 Tool 回傳 SUCCEEDED。

**輔助指標，不可替代 OGCR：**
- Recovery Yield：停滯後真正到達新客觀里程碑的比例；
- Duplicate Side Effect Count：重複執行有外部副作用的 action 次數，安全目標 0；
- False Progress / False Terminal Rate：沒有客觀證據卻宣告進展／SUCCESS 的比例，安全目標 0；
- Verified Checkpoint Integrity：跨 Session 保留原 identity、actions、evidence、blockers 的一致性；
- Cost、latency、tokens、new-session 次數與停滯後客觀淨進展；
- Honest Halt Accuracy：無法安全續作時，是否正確進 PAUSED/UNKNOWN 並指出客觀阻礙。

比較組別：A **Same-Conversation Only**、B **Fresh-Context Only**、C **Hybrid GAR**；對相同的 pinned 任務、初始 checkpoint、工具及授權限制，以相同總計算/時間/重試預算進行對照；不要用不同案件或不同權限比成功率。初期可用固定 Planner、失敗注入及獨立離線 Runtime 測試，再決定是否經授權進行安全 Live。

**目前無數據足以斷言 C 的成功率必然最高**；Hybrid 是可檢驗的架構選擇，不是已證成的統計結論。

## 10. 強制負面案例與安全 Gate

1. **舊 DOM 重播**：回應文字相同且 ACK/round 沒變；只應 Transport reconciliation，不得消耗新 action 或把它算成新的模型錯誤。
2. **真正新回覆仍重複**：ACK/round 新、semantic blocker 不變；需辨識 no-progress，有限次數後升級 L2/L3/L4 或 PAUSED。
3. **缺欄位且 batch 含 mutation**：保存未執行合法 batch；不能因局部補答而重新 dispatch/改 payload。
4. **已完成 mutation**：任何 Session 都不得以新 Action ID 重做同一已完成的副作用；唯讀驗證可依既有規則進行。
5. **OPEN writer**：切 Session 不解除 \`OPEN_WRITE_TRANSACTION\`；不得以 COMPLETE/SUCCESS 越過 commit/abort/verification。
6. **RUNNING／QUEUED**：新 Session 不能當作失敗或未執行；不得不經客觀確認重送 command/transport delivery。
7. **UNKNOWN side effect**：不得推論「肯定未執行」；先核對，否則安全停止。
8. **Foreign task/request/epoch、stale snapshot、scope escalation**：拒絕；不得重置身份或改寫原授權。
9. **原始 Goal 條件遭刪改**：拒絕不具使用者核准的隱式 Goal 變更；False SUCCESS 必須被 terminal gate 擋下。
10. **budget exhausted / new session unavailable**：保留恢復狀態並 PAUSED，不得再次初始化形成無限迴圈。
11. **Crash between checkpoint and new session**：須重建唯一恢復狀態，避免兩個 Worker 同時領取並執行。
12. **secret in snapshot/log/progress**：輸出應遮罩敏感資訊；禁止把 credentials 或任意 raw log 注入模型。
13. **negative expected predicate**：結果 FAIL 可能是符合使用者成功條件的觀測；不可誤判為無進展。
14. **外部副作用不可 exactly-once 保證時**：忠實呈現限制，不能以輸出或 Snapshot 承諾 exactly-once。

所有負面測項都要明確檢查 no unapproved action、no false acceptance、journal preserved，而不只看模型是否產生不同文字。

## 11. 分階段開發提案（不代表已授權執行）

| 階段 | 產物 | 過關條件 |
|---|---|---|
| GAR-0 — Baseline/Contract | 固定原始任務測試集；Goal Contract/Snapshot schema、目前 Runtime seam mapping | 定義可客觀測量的 OGCR、identity/inflight safety gate；不修改主線 |
| GAR-1 — Classification | Transport freshness / Schema repair / semantic no-progress 事件分類 | 能辨識舊 DOM vs 真實新回覆；不得誤觸 tool |
| GAR-2 — L2/L3 | Runtime Snapshot projection、Progress Rebase、Bounded Replan | 保留 Goal/evidence、阻止重播，於既有預算內產生有效下一步或 PAUSED |
| GAR-3 — L4 | Fresh Planner Context adapter、session fencing、load/verify snapshot | 能可靠隔離舊 Session；無可用 adapter 時 fail closed |
| GAR-4 — Comparison | A/B/C 對照驗證，含離線、注入錯誤、可授權的安全 E2E | 以原始任務完成率與安全指標決定是否採用、調參或回退 |
| GAR-5 — Rollout | 預設 OFF 的灰度配置、觀測與回退機制 | 不影響原 PR10/PR11；審核通過後才開啟 |

**依賴關係：** GAR 作為獨立規劃項目，不能把未完成的 PR10-2A/5A/5B/Goal1/PR11 驗收直接改稱 GAR PASS；也不能在開發 GAR 時改寫那些既定 gate。正式改 Source/啟動 Live/merge 需另行授權並具備獨立驗證計畫。

## 12. 開放決策／實作前必須確認

- **Fresh Context 能力**：目前 Telegram/WebCopilot/Browser 所用 provider 是否能在可驗證的方式下建立新 Planner Session？如需建立新 chat，是否能正確綁定 owner、Task、reply destination 並隔離舊輸出？
- **Goal Contract 版本**：既有 Progress completion_contract、Task Continuation 與 original request 如何形成不可私改且可增補客觀驗證的版本化 Goal？
- **Checkpoint Granularity**：以 action/evidence/plan node 哪種粒度恢復最符合現有 Runtime 交易模型？
- **Budget 與升級門檻**：L2/L3/L4 幾次、何種語意變化算客觀進展？需與既有 bounded repair 計數及 soft deadline 整合。
- **Evaluation Datasets**：如何選取真實的長任務、repair hang、DOM replay、chunk OPEN/UNKNOWN、外部交付等案例，同時不碰使用者真資料？
- **跨 Session 工具狀態**：in-flight mutation、舊 Planner 遲到的 ACK、terminal receipt、effectively-stale evidence 由誰進行 fencing 與仲裁？

上述未決事項不得由模型根據直覺標成「已經解決」。

---

**決策摘要：** 採用 **SmartAgent Goal-Anchored Recovery（GAR）／Hybrid Recovery** 作為後續設計方向；以 L0→L4 的有限恢復階梯、Runtime-owned Goal/Evidence/Checkpoint 及 Original Goal Completion Rate 為核心。**文件記錄設計共識，不表示功能已實作、驗證或發布。**
