# Telegram「執行cmd」— 與一般 Bot 功能共用授權

## 不需要第二次登入，也不需要 CMD 專用 User ID / Chat ID

本功能直接使用 **現有 Telegram Bot 的身分與回覆路由**：

1. 在已配對的 SmartAgent Bot 私人對話點選「查詢遠端功能 → 執行cmd」。
2. Bot 傳送一份可修改的固定範例。
3. 把範例傳回同一個 Bot 私人對話。
4. 既有 Telegram Receiver 檢查：private chat、非 Bot 發送者、
   且 **User ID + Chat ID 已通過原本的配對或原本的靜態白名單**。
5. 固定軟體流程識別 `run cmd by TG`、執行命令，並用收到訊息的
   `chat_id` 和 `message_id` 回覆**原本的 Telegram 對話**。
   不經過 Agent Task Queue / Planner / WebGPT。

範例（`cmd path` 由目前 SmartAgent 安裝根目錄自動填入）：

```text
run cmd by TG
cmd path: "C:\Users\user\Desktop\remoteagent\smartagent"
command:
"echo hello"
"echo %TIME%"
```

**不需要設定** `telegram_cmd_settings.json`、
`SMARTAGENT_TG_CMD_ALLOWED_USER_IDS`、
`SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS`、
`SMARTAGENT_TG_CMD_ENABLED`。這些是早期試作的重複設定，
這一版不再讀取。若你的 `localdata` 有舊檔案，它不影響目前授權。

現有 Bot Token、Telegram 配對/原本白名單仍照舊運作；
Telegram Bot 的顯示名稱不需要放在這個指令的任何欄位。

## ACL 模式和安全界線

新安裝仍採 **ACL OFF**，既有 ACL ON 安裝不會被更新自行切換。

- **ACL OFF：** SmartAgent `bounded_process` + Software Guard；
  主機使用者的 Windows 權限仍存在，不等同 OS sandbox。
- **ACL ON：** `bounded_process` 呼叫 Windows Restricted Executor。
- **兩種模式都必須先通過原本的 Telegram 私人對話授權**。
  CMD 控制會在執行前重讀目前配對或原本白名單的驗證結果；
  非 Telegram、未配對、非私人對話均拒絕。
- 自訂工作路徑仍須受 Workspace Access Policy 授權；
  原 SmartAgent 安裝根目錄可當作固定模板的工作目錄。
- 命令安全分類、每行 20 秒、每批 120 秒、最多 12 行、
  輸出量上限、失敗即停止後續行仍保留。

注意：由於 ACL OFF 使用一般 Windows 使用者權限，
Shell 的複雜重導向或間接呼叫並非全面 OS 層隔離。
**只讓可信任且有權操作本機的 Telegram 帳號配對**。

## Windows Regression

```powershell
$env:PYTHONPATH = "source"
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_telegram_direct_cmd_it.py
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_telegram_cmd_shared_auth_it.py
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_telegram_cmd_acl_modes_it.py
```

實機 Telegram Live 測試：從已配對的 Bot 私人對話傳送上述
`echo hello`、`echo %TIME%` 範例，應回覆到同一個對話，
每行 Exit Code 0，並顯示 ACL OFF / Software Guard
或 ACL ON / Restricted Executor。
