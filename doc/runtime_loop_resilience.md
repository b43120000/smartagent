# Action Loop 相容性與防空轉保護

修改包：C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1。
目的：可修復的模型／Runtime 格式差異不直接丟失任務；授權、實際執行與客觀終態仍必須驗證。
本次沒有修改 E 槽開發 source、sync-back、Git checkpoint 或 Telegram 網路驗證。

## 八項實作

| 情況 | Runtime 實作 | 邊界 |
|---|---|---|
| 數字型別不符 | 依工具 schema 正規化無歧義 int/float 字串；query 範圍數值也正規化；錯誤一次回 field_issues | 不猜 bool、ID、path、command 或任意 JSON/prose |
| 補答漏 action | 保存原順序；部分原 action 回送時合併完整 held batch；阻止換掉原決策或提前結案 | 不在補答階段執行；完整准入後才執行 |
| 改措辭未解除阻塞 | Action Loop 輸出 blockers[]、穩定 ID、blocking_action_id、remedies、release_conditions | 機器條件由 Runtime 結果解除，模型不能自行設 satisfied=true |
| 宣稱完成但證據不足 | 依 open write／pending／stale／postcondition 給精確補救；SUCCESS 不能跳過 live blockers | 不把 queued/running 或未知執行改成成功 |
| transition 不匹配 | 支援同 project root、涵蓋所需範圍的 read_range；終態提案進客觀終態閘門；WAIT_RUNTIME 只允許觀察／驗證 | 不是接受任意同名工具；blocked action 不得盲目重送 |
| 路徑黏上後續欄位 | 已辨識的鄰接 snapshot_id/project_handle/queries 等欄位不併入路徑；引號內字面內容保留 | 真實及偽裝 ADS 仍拒絕；其他模糊 prose 不保證可解析，應提供引號／完整路徑 |
| chunk／程序／交付未完成 | 分開 QUEUED/RUNNING/PARTIAL/UNKNOWN lifecycle；彙整較早 ACTIVE OPEN/pending，不被後來讀取遮蔽 | commit/abort 解除相同 write_id；網路交付仍由 transport owner 確認，未新增假的 ACK |
| 舊證據／結果覆蓋 | 保存 request/task epoch/round/snapshot、ACTIVE/STALE/SUPERSEDED；局部 mutation stale 受影響資源；cache replay 不改生產回合／最新驗證 | pathless mutating command 保守影響 workspace；嘗試執行後 FAILED 也可能有部分改動；歷史 execution 不會被 verifier 改寫成已執行 |

PARTIAL 必須引用當前 request 的有效客觀結果；不能用舊失敗跳過 OPEN／VERIFY／PREREQUISITE。
已觀察到的失敗／拒絕可依完成契約回 FAILED/PARTIAL；transport queued handoff 只能誠實報 PARTIAL，不能聲稱交付成功。
真正沒有新證據、沒有合法下一步、授權拒絕或模型持續忽略同一補救時，原本防空轉暫停仍保留。
這不是「所有錯誤都強制繼續」。

## 程式入口

- source/agent_core/smartagent_protocol.py：normalize_tool_numeric_fields、批次 field_issues。
- source/agent_core/tools.py：直接 dispatcher 呼叫同樣正規化。
- source/agent_core/action_loop_state.py：WAIT_RUNTIME、pending lifecycle、全 action blockers、可用工具篩選。
- source/WebAgent/protocol_loop.py：held batch、精確 terminal recovery、等價範圍、原始／解析路徑、證據歷程與准入。
- source/WebAgent/protocol.py：Planner 提示和 Runtime-owned 欄位規則。

## 驗證

新增 validate_runtime_loop_resilience（八類、包含實際 Ack 准入、abort/supersession、UNKNOWN verifier、cache replay）。
直接相關回歸：authorized_path_extraction、action_loop_runtime_state、progress_terminal_lifecycle、
flexible_progress_admission、result_evidence_to_action_recovery、blocker_recovery、
typed_command_and_scoped_semantics、delete_result_recovery、P0/P1/P2 result_contract、indexed query。
測試存在者維持原安全判定；未放寬 ADS、根目錄授權或修復 mutation 的前置條件。

### 本機模擬 Telegram → 真實網頁 → 真實 Runtime

| request | rounds | 真正 tool action | 協議拒絕 | 結果 |
|---|---:|---:|---:|---|
| RR-LOCAL_TEST-4239221D134C1D16 | 2 | 2，同輪 inspect_directory + query_project | 0 | SUCCESS；sample_limit=3、read_range 1 行、truncated=false、cursor=null |
| RR-LOCAL_TEST-F772FF6F668795BF | 2 | 1，read_file | 0 | 正確以 COMPLETE/FAILED 結案；FILE_NOT_FOUND、executed=false，未重試／建立檔案 |

正向 event：EVT-695473C57FB6E3B97112；負向 event：EVT-28F1593D435792DC9D9F。
Task state=COMPLETED 不代表所有 operation SUCCESS；負向測試 outcome=FAILED 是預期結果，不能改寫為 SUCCESS。
目前 IT 不涵蓋真實網路交付、長時程 process polling、所有 provider DOM 或所有自然語言歧義。

獨立複審發現的 pending replay、舊 OPEN 隱藏、PARTIAL 跳過驗證、abort 未解除、
pathless mutation 舊證據、最新 scoped PASS 仍被拒絕等缺口均已依具體證據修正。
UNKNOWN/PARTIAL 經目前 request 的獨立 linked VERIFY 得到 PASS，可採用該 postcondition 作終態證據；
原始 execution_status 仍保留 UNKNOWN/PARTIAL，不捏造過去操作一定執行成功。
