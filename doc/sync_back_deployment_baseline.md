# Sync-back：獨立部署基準

預設倒回目的地：`E:\card\release\SmartAgentv2`。
必須與部署紀錄的 source_root 相同；其他路徑不是任意鏡像目的地。
先停止涉及的 Agent 與其他程式碼寫入者，再從執行包執行：

```bat
sync_back.bat -DryRun
sync_back.bat
```

正式寫入需輸入 `SYNC_BACK`。不會自動重啟 Agent。
從開發包直接使用預設倒回會因來源/目的地相同被拒絕。

## 部署基準

manifest_update.ps1 與 update.ps1 在部署檔案 SHA-256 通過後保存
`localdata/update/deployment_baseline.json`，包含安裝根目錄、來源根目錄、版本與實際部署檔案雜湊。
基準不會隨發布 manifest 重建改變；更新失敗時與檔案一起回復。
沒有基準的舊執行包必須先透過新版 updater 更新成功一次，不能從現有 manifest 猜測舊版本。
這不是數位簽章；ACL OFF 下使用者仍能修改基準紀錄。

## 三方比較

- 執行包未修改：保留開發包修改。
- 執行包修改、開發包仍為部署基準：允許倒回。
- 兩端修改後相同：不需覆寫。
- 兩端不同修改，或開發包刪除但執行包修改：拒絕衝突。
- 執行包刪除基準檔案：要求人工確認，不自動刪除開發包檔案。
- 新增檔案限 source、doc、install_smart_agent 及根目錄腳本/Markdown，仍接受排除與衝突檢查。

排除環境、localdata、Git、快取、config、browser_profile、.codex、.aws、.ssh、.env 與常見私鑰檔案。
發布 manifest 在倒回後重建，不倒回執行包的設定。
這不是原始碼秘密掃描，不能把 token 寫入 source 或 doc。

## 交易與復原

DryRun 不寫檔。正式執行重新檢查基準與兩端雜湊，並暫存、备份、覆寫、重建及核對 manifest。
兩種 updater 與 sync-back 以 mutex 互斥。路徑跳脫、ADS、reparse point（含 Python 路徑）拒絕。

備份保留於執行包 `localdata/sync_back/transactions/<id>/`，state.json 保存交易狀態。
失敗嘗試回復已寫入檔案；回滾不完整會保留備份並報出路徑。
PREPARED/STAGING 或未知交易狀態會阻止後续倒回，需要人工復原；尚無斷電後自動復原。
此機制不鎖住外部編輯器，請勿並行改檔。

## 驗證與部署

隔離測試：validate_sync_back.ps1、validate_manifest_update.ps1。
涵蓋基準不受 manifest 重建影響、三方衝突、缺少基準拒絕、預覽不寫入、故障回滾/備份、
中斷交易阻擋、發布重建、第二次部署替換基準、發布後故障回復舊基準及設定/環境保留。

完成這些隔離測試不代表實際執行包或網頁 IT 已通過。實際 update2 與正式倒回由使用者另行發起。
