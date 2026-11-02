# Telegram 執行cmd

這是獨立於 Planner/WebGPT/Task Queue 的管理員軟體操作。

在 Telegram 點選「查詢遠端功能 → 執行cmd」，Bot 回傳可編輯範例：

```text
run cmd by TG
cmd path: "C:\\Users\\user\\Desktop\\remoteagent\\smartagent"
command:
"echo hello"
"echo time"
```

每行使用 Windows cmd.exe，在指定目錄各執行一次。
回報每行 stdout、stderr、Exit Code，第一筆失敗後停止後續行。
不會開啟可見的互動 CMD 視窗；`cd`/`set` 的設定不跨行保留。

## 本機固定設定（推薦）

設定檔固定放在：

`<SmartAgent 根目錄>\localdata\secure\telegram_cmd_settings.json`

只需編輯一次，後續啟動 `launch_remote_agent.bat` 時，
HostSupervisor 自動載入，不用每次設定 PowerShell `$env:`。
此路徑在 Git 忽略的 `localdata` 內，一般 Git 更新不會覆蓋它。

第一次在 SmartAgent 根目錄執行：

```powershell
New-Item -ItemType Directory -Force ".\localdata\secure" | Out-Null
if (-not (Test-Path ".\localdata\secure\telegram_cmd_settings.json")) {
    Copy-Item ".\config\telegram_cmd_settings.example.json" ".\localdata\secure\telegram_cmd_settings.json"
}
notepad ".\localdata\secure\telegram_cmd_settings.json"
```

填入自己的 Telegram 數字 ID：

```json
{
  "schema": "SMARTAGENT_TG_CMD_SETTINGS_V1",
  "enabled": true,
  "allowed_user_ids": [123456789],
  "allowed_chat_ids": [123456789]
}
```

**123456789 僅為範例。** 必須填入自己的 User ID 和私人 Chat ID。
設定數字不加引號、尖括號，勿把本機設定檔提交到 Git。

儲存後完整關閉原 RemoteAgent，重新執行 `launch_remote_agent.bat`。
啟動時會顯示 `Telegram CMD local settings: ENABLED`，
不會把 ID 印到 Log。

設定檔中的 `enabled: false` 可以永久關閉此功能。
如果 JSON 損壞、欄位缺失或 ID 無效，Runtime 一律關閉執行權限，
不會退回舊的允許執行環境變數。若完全沒有設定檔，
才保留先前臨時 PowerShell `SMARTAGENT_TG_CMD_*` 模式。

仍需符合原本 Telegram 私訊授權、Workspace 寫入政策及
Windows Restricted Executor（ACL ON），配對本身不授予 CMD 權限。

每批最多 12 行、每行 20 秒、總時間 120 秒；以既有
SmartAgent Command Security 過濾不允許的命令。

## Windows Regression

```powershell
$env:PYTHONPATH="source"
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_telegram_direct_cmd_it.py
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_telegram_cmd_settings_it.py
```

先用 `echo hello` 及 `echo %TIME%` 做 Live Smoke Test。
完整 Restricted Executor Live 還需要在實際 Windows 部署驗證。
