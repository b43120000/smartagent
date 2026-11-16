# SmartAgent Git65 — ACL OFF / Turbo OFF / Auto Approve Runtime

Base: `backup/apk-runtime-1b9030f-20261116` @ `1b9030f6fb20fb326f9122a565db5ad3170c2d31`

Branch: `git65/acl-off-auto-approve-runtime`

## Mode contract

Auto Approve is **OFF by default**, with explicit authenticated human opt-in. The toggle is stored in the **existing** `localdata/secure/windows_security/workspace_access_policy.json` (`auto_approve_requested`), preserving all Workspace and Turbo records. The intended behavior is:

| ACL | Turbo requested | Auto Approve requested | Auto Approve effective | Execution routing |
| --- | --- | --- | --- | --- |
| OFF | OFF | OFF | OFF | ordinary Windows Executor + approvals |
| OFF | OFF | ON | ON (authorized writable Workspace only) | ordinary Windows Executor, eligible secondary prompts skipped |
| OFF | ON | OFF | OFF | ordinary Windows Executor (Turbo suppressed) |
| OFF | ON | ON | ON (authorized writable Workspace only) | ordinary Windows Executor (Turbo suppressed) |
| ON | OFF | OFF | OFF | Restricted Executor |
| ON | OFF | ON | OFF | Restricted Executor |
| ON | ON | OFF | OFF | Native AppContainer Broker only |
| ON | ON | ON | OFF | Native AppContainer Broker only |

`auto_approve_effective` fails closed for an unverified ACL mode, policy, path or working directory. It is not the same as command execution success, not a workspace expansion and not a Windows OS confinement mechanism.

## Settings

Use one of the existing authenticated operator settings entry points; never ask Planner to change its own permissions.

**Telegram**, as standalone operator messages (not embedded inside a build prompt):

```text
autoapprove=on
autoapprove=status
autoapprove=off
```

**Edit Workspace:** select **Security / Workspace Access → [9] Auto Approve ON/OFF**. The existing **WebGPT 與 workspace 管理器** form also accepts `auto_approve=on` or `auto_approve=off` in its transaction block. An absent field in an old form does not change the setting. Runtime status displays ACL, Turbo Requested/Effective, Auto Approve Requested/Effective, Executor Backend, reason code and revision.

ACL selection remains controlled by existing `ACLstatus.bat`; Turbo remains controlled by the existing Turbo/Workspace controls. These are three independent user settings. In ACL OFF, Turbo effective is OFF even when the requested preference is ON.

## Deploy and preflight (Windows)

Run from the existing local repository **only after saving any uncommitted changes**:

```bat
cd /d C:\smartagent
git fetch origin git65/acl-off-auto-approve-runtime
git switch -c git65/acl-off-auto-approve-runtime --track origin/git65/acl-off-auto-approve-runtime
git rev-parse HEAD
```

If the branch already exists locally, use `git switch git65/acl-off-auto-approve-runtime` and `git pull --ff-only`; do not force-reset another branch.

Ensure that the target Android project `C:\Users\user\Desktop\remoteagent\HDRViewer\sources\Androidviewer` is contained in an *already operator-authorized writable Workspace*, with no conflicting read-only/denied root. Configure from **Edit Workspace** if necessary. The Git65 policy will NOT add a new writable Workspace automatically.

Ensure ACL mode is OFF through the **existing** `ACLstatus.bat` workflow and verify its final status. Configure Turbo OFF using the existing setting if desired. Restart the installed RemoteAgent/WebCopilot workers so they run Git65 source; simply checking out a repository that is *not* the installed runtime does not deploy it.

Run `autoapprove=on` then `autoapprove=status` through the authenticated Telegram control. Require:
```text
ACL: OFF
Turbo Effective: OFF
Auto Approve Requested: ON
Auto Approve Effective: ON
Executor Backend: WINDOWS_EXECUTOR
```

Auto Approve skips eligible **secondary human approvals** only: the original Planner tool Action admission, command blacklist, per-executable SHA256 manifest, scoped path checks, original Tool Result, Evidence Recorder, uncertain-outcome prevention and Finish Comparator are retained.

## Live Telegram → Planner → Runtime acceptance

Send the following as a **new natural-language Telegram task**, separate from the setup commands:

> 編譯以下 Android 專案的 APK：\
> `C:\Users\user\Desktop\remoteagent\HDRViewer\sources\Androidviewer`\
> 若遇到任何環境缺失或問題，持續處理，直到 APK 成功編譯出來。\
> 依照已授權的 Writable Workspace 與 Runtime 安全策略自行檢查工具、處理環境缺失、重新編譯與驗證。\
> 最後回報所有使用到的 Tool 執行檔絕對路徑、指令、Exit Code、stdout/stderr Evidence reference、APK 絕對路徑、存在性、檔案大小與 SHA256。\
> 若 Runtime 回報 UNKNOWN 或執行結果不確定，請先以唯讀方式取得獨立證據，禁止盲目重試可能有副作用的命令。\
> 不得使用 Telegram Host CMD，也不得使用 Native AppContainer Broker 作為此 ACL OFF 對照測試的替代 Executor。

APK 必須由真實 Gradle/Android build 完成，不能以 Mock、Gradle version probe 或空白檔案冒充。

For an independent local artifact check after Runtime reports a result, adapt the APK path it actually returns:

```powershell
$apk = 'C:\absolute\path\returned\by\runtime\app-debug.apk'
if (-not (Test-Path -LiteralPath $apk -PathType Leaf)) { throw 'APK_MISSING' }
Get-Item -LiteralPath $apk | Select-Object FullName, Length, LastWriteTime
Get-FileHash -LiteralPath $apk -Algorithm SHA256
```

### Evidence / acceptance distinctions

The GitHub Actions jobs run automated UT and the Windows real CMD/BAT child-process smoke IT on an isolated GitHub Windows runner, **not** on the user's Windows machine. That is not an Android APK E2E. The local acceptance is incomplete until the Telegram task actually runs and produces a verified APK and Runtime Evidence.

ACL OFF permits ordinary Windows child processes. Software guards can check *submitted commands and known paths* but **cannot enforce a kernel-level no-write boundary on arbitrary child scripts**. Use a disposable Windows VM or a backed-up test environment if external build scripts are not trusted. The Auto Approve option never claims to replace an OS sandbox.
