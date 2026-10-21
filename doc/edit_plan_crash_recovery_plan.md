# T31 程序 crash 恢復：擬議實作範圍

狀態：僅規劃，尚未實作。2026-10-15 使用者要求加入 crash 恢復，但先提出範圍。

後續決定（2026-10-15）：使用者明確要求「先不做crash回復，先做T26、T28、T29」。以下保留為未啟動的設計；T31依不含crash的一般UT／Runtime IT／Web IT範圍列PASS，不宣稱具備強制關閉後自動恢復。

## 目標與邊界

一般例外回滾已實作；目前 batch_apply 的 backups／created／touched 只在記憶體。程序強制結束不會進 except，因此多檔編輯可能停在中間。

此次 crash 範圍是 worker／Python 程序中斷及重啟，不承諾硬碟毀損、斷電或惡意本機管理者修改紀錄。不得全盤掃描、全域重啟、盲目重播 apply、猜測任務成功，亦不得覆蓋 crash 後使用者自行修改的檔案。

## 最小完整切片

1. apply 開始前，所有 target 完成 schema、snapshot、SHA、scope、權限與 linkage 檢查。
2. 新增專用交易儲存模組。預計使用 `<workspace>/.agents/edit_transactions/<transaction_id>/`，先驗 metadata 端點與 reparse，備份不可进入 source bundle、模型提示或附件。
3. 持久備份所有原檔 bytes／SHA、預期 AFTER SHA、create/new-dir 清單及 canonical plan digest；綁定 workspace、instance/owner、request/task/action identity。
4. 先保存 PREPARED，再逐檔保存 write intent，原子發布，實讀確認後保存 applied receipt；每一步狀態 atomic 保存，關鍵 bytes flush。
5. 全部檔案成功後持久保存 COMMITTED 與結果；該結果只證明編輯／readback，不取代任務契約與獨立 linked VERIFY。
6. 一般錯誤透過同一交易模組保存 ROLLING_BACK，逐檔恢復並核對 original SHA；失敗保存 RECOVERY_REQUIRED、不可誤報 ROLLED_BACK。
7. 下次啟用相同 workspace／任務接續時，僅定位其已知交易，檢查 owner／授權／備份／目標 SHA。不做跨 workspace 的自動全盤恢復。
8. 未 commit 的交易：只有目標內容等於 original 或此交易已寫出的 AFTER、且原權限仍有效，才允許安全回滾／接續。其他 SHA、損壞備份、reparse 或失去授權都保留檔案並回報 blocker。
9. 已 commit、但回覆遺失：沿用持久 receipt，不再次 apply；接回 action/evidence ledger，仍可做原 action 的 linked VERIFY。
10. 使用既有 process_file_lock／JSON I/O 的適用能力，避免同 workspace 交易同時改到同一批檔。鎖在 crash 後須可安全重新取得；鎖不是外部編輯器防護保證。

## 狀態

`PREPARED → APPLYING → COMMITTED`

例外／crash未完成：`APPLYING → ROLLING_BACK → ROLLED_BACK`。

不能安全判定或恢復：`RECOVERY_REQUIRED`。不可用 `COMMITTED` 自動宣告 Task SUCCESS。

## 預計改動

| 檔案 | 責任 |
|---|---|
| 新 `source/agent_core/edit_transaction_store.py` | 交易identity、持久備份、狀態、receipt、驗證與安全恢復 |
| `source/agent_core/batch_apply.py` | 接入備份／write intent／原子發布／commit及回滾，不重播副作用 |
| `source/agent_core/tools.py` | 每個target與交易metadata權限、request scope、task/action identity傳遞 |
| `source/agent_core/tool_result_policy.py` | 分開已commit、已回滾、待恢復、拒絕與未知；保留可查transaction_id |
| `source/WebAgent/tool_context.py`、`source/agent_core/task_continuation.py` | 最小接續入口與receipt重用；只有證據顯示缺口才擴到protocol_loop |
| 新專用UT／隔離subprocess IT、驗證表 | 真process結束與恢復、狀態／磁碟獨立比對 |

不新增模型必填欄位；identity與transaction_id由Runtime持有。若需要新恢復工具或使用者操作入口，先確認介面再實作。

## 必須通過的測試

- PREPARED後、首檔發布後、第二檔intent後、全檔發布但未commit、commit後未回覆、回滾途中：逐一在專用測試子程序强制結束。
- 重新載入後核對來源bytes、備份SHA、交易狀態、effective verification及action執行次數，不能只看模型文字。
- crash後外部修改、備份毀損、權限撤銷、junction、跨workspace/owner、並行交易：拒絕不安全恢復，不覆蓋、不擴大scope。
- COMMITTED receipt重讀與重複接續不得再次修改，仍能正常 linked VERIFY。
- T30與一般T31回歸；正式ChatGPT Web IT只測安全的可觀察接續，不能強制關閉使用者正在使用的RemoteAgent。真crash注入只操作owned子程序與合成fixture。

完整通過這些測項後，才將T31整體由PARTIAL改為PASS。
