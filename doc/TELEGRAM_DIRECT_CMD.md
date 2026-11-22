# Telegram「執行cmd」— 已配對使用者的 Windows 手動 Host CMD

## 信任邊界

`run cmd by TG` 是**使用者明確輸入的遠端管理命令**，不是 Planner/Executor 自主動作。
仍須通過原本的 Telegram 私人對話配對／白名單：User ID + Chat ID、非 Bot 私人訊息。
不需要第二套 CMD User ID/Chat ID 設定；非私人對話、未配對、偽造訊息均拒絕。

- **ACL OFF/ON × Turbo OFF/ON 四種組合，執行方式完全相同。** 以 Telegram Listener 的 Windows Token 建立一個可見的 `CREATE_NEW_CONSOLE` CMD，依序執行每行指令。
- 不走 Agent `bounded_process`、Restricted Executor、Native AppContainer Broker、Workspace Access Policy 或 `inspect_command`。使用者可自行輸入 `del`、`rmdir`、腳本等命令；`cmd path` 可為任何實際存在的絕對路徑。
- **真正的 Windows 權限仍有效：** CMD 不提高權限、不關閉 ACL、不提升 UAC、不修改 Turbo 設定。操作成功與否取決於 Listener Windows 帳號當下擁有的權限。
- Agent 透過 Telegram 進行的一般工作、WebGPT/Planner 提出的 `run_command` 和本機寫入／刪除工具**不能走這條手動通道**；仍維持 ACL/Turbo Action Admission，Turbo ON 必須走 Native Broker。

**注意：** 手動 CMD 故意不受 Turbo Root 限制，可能修改 Root 以外的資料；若 Listener 以提高權限執行，CMD 也可能繼承該權限。因此「ACL ON 保護 Root 以外」僅適用 Agent OS 隔離路徑，**不適用你主動授權的手動 Host CMD**。務必保護 Telegram 帳號與 Bot Token，因為失竊可能讓第三方取得主機控制權；避免透過 Telegram 發送密碼。

## Telegram 使用範例

在已配對 Bot 的私人對話點選「查詢遠端功能 → 執行cmd」，修改模板後回傳：

```text
run cmd by TG
cmd path: "C:\smartagent"
command:
"whoami"
"echo %CD%"
"git status --short"
```

最多 12 行，單行預設 300 秒、整批 900 秒。依序回傳每行 Exit Code、stdout、stderr；失敗或超時停止後續指令。同一個 CMD Session 內的 `set`/`cd` 等狀態可沿用。獨立 `pause` 會跳過，BAT 內 `pause` 需要自行處理或會等到逾時。

可見 CMD 必須在互動 Windows Desktop Session；如果 Listener 在 Session 0，會回覆 `TG_CMD_DESKTOP_SESSION_REQUIRED`，不會悄悄轉成隱藏或 Agent Executor。

## Windows regression

```powershell
$env:PYTHONPATH = "source"
python -B source/WebAgent/tests/validate_telegram_cmd_shared_auth_it.py
python -B source/WebAgent/tests/validate_telegram_direct_cmd_it.py
python -B source/WebAgent/tests/validate_telegram_cmd_acl_modes_it.py
python -B source/WebAgent/tests/validate_telegram_visible_console_it.py
```

CI 的刪除測試只使用 Runner 自建暫存檔。實機先使用 `whoami`、`echo %CD%`，再用自己新建的測試檔案驗證刪除；不要拿真實專案資料執行破壞性測試。
