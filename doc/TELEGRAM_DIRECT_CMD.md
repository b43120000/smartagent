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

## 必要的本機設定

啟動 Telegram HostSupervisor 的 PowerShell 需先設定：

```powershell
$env:SMARTAGENT_TG_CMD_ENABLED = "1"
$env:SMARTAGENT_TG_CMD_ALLOWED_USER_IDS = "<Telegram numeric user ID>"
$env:SMARTAGENT_TG_CMD_ALLOWED_CHAT_IDS = "<Telegram private chat ID>"
```

還必須符合既有 Telegram 私訊授權、Workspace 寫入政策及 Windows
Restricted Executor（ACL ON）；配對本身不授予 CMD 執行權。
預設不開放，ACL OFF 環境下拒絕執行。

每批最多 12 行、每行 20 秒、總時間 120 秒；以既有
SmartAgent Command Security 過濾不允許的命令。

## Windows Regression

```powershell
$env:PYTHONPATH="source"
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_telegram_direct_cmd_it.py
```

先用 `echo hello` 及 `echo %TIME%` 做 Live Smoke Test。
完整 Restricted Executor Live 還需要在實際 Windows 部署驗證。
