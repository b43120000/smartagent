# SmartAgent

SmartAgent 讓 ChatGPT 網頁對話窗具備本機 Agent 能力。

你可以直接用自然語言交代工作；ChatGPT 負責理解、規劃與回覆，SmartAgent runtime 則在 Windows 電腦上執行經過檢查的檔案、命令、專案與瀏覽器操作，再把真實結果送回同一個 ChatGPT 對話。

目前主要支援 **ChatGPT 網頁版**。Gemini、Claude 與自動 UI 校準仍屬開發中功能，不建議當成主要使用入口。

> [!WARNING]
> SmartAgent 可以讀寫檔案並執行命令。只應授權你信任的 Workspace、ChatGPT 對話與 Telegram Bot。帳號登入、密碼、2FA 與 CAPTCHA 必須由使用者手動完成。

## 也可以直接交給 ChatGPT 或 Codex 帶你安裝

如果你不熟悉安裝流程，可以把這個 repository 提供給 ChatGPT 或 Codex，然後直接詢問：

```text
請先讀取 AGENTS.md 與 skills/smartagent-onboarding/SKILL.md，
再依照我的 Windows 環境，一步一步帶我安裝、設定並啟動 SmartAgent。
每一步先說明會做什麼；遇到錯誤時不要猜測，請根據實際輸出排查。
```

- `README.md` 是給使用者閱讀的操作說明。
- [`skills/smartagent-onboarding/SKILL.md`](skills/smartagent-onboarding/SKILL.md) 是給 AI 閱讀的安裝、設定、啟動、排錯與刪除契約。
- [`AGENTS.md`](AGENTS.md) 會提醒 Codex 優先讀取這份 skill。

ChatGPT 若無法直接操作你的電腦，仍可依 skill 逐步指導；請把每一步的實際輸出貼回去。Codex 若具備本機執行權限，也必須先說明並遵守安全邊界，不能自行跳過 UAC、ACL 或路徑檢查。

## 主要能力

- 從 ChatGPT 網頁對話接收自然語言工作。
- 列出、搜尋、讀取及修改已授權 Workspace 內的檔案。
- 執行有範圍限制的命令並回傳 exit code、stdout 與 stderr。
- 追蹤多輪任務的 Progress、Runtime evidence 與完成狀態。
- 下載本輪新產生的檔案或圖片，或把必要檔案交付給 ChatGPT。
- 透過 Telegram RemoteAgent 遠端送出工作與查看狀態。
- 使用 `smartagent_tool` v9 協議驗證 action、結果與回合提交，避免直接執行未驗證的自然語言。

## 系統需求

- 64 位元 Windows 10 或 Windows 11。
- 網路連線與可使用 ChatGPT 網頁版的帳號。
- 可安裝 Python、Playwright Chromium 與專案相依套件的環境。
- 建議先使用獨立測試 Workspace 與獨立 ChatGPT 對話。

## 下載

可從 GitHub 下載 ZIP 並解壓縮到一般使用者可寫入的資料夾，或使用：

```powershell
git clone https://github.com/b43120000/smartagent.git
cd smartagent
```

不要從其他電腦複製 `.venv`、`localdata`、`.agents`、瀏覽器 profile、Token 或暫存檔。

## 安裝

1. 在 SmartAgent 根目錄雙擊：

   ```text
   install_smart_agent.bat
   ```

2. 等待安裝程式建立本機 `.venv`、安裝 Python 套件與 Playwright Chromium。
3. 安裝完成後，預設為 **ACL OFF**：保留軟體層的命令與路徑安全檢查，但不啟用 restricted executor 的 NTFS 防竄改保護。
4. 若只想檢查環境、不進行安裝，可執行：

   ```text
   install_smart_agent.bat -ValidateOnly
   ```

安裝失敗時先查看視窗中的第一個錯誤，以及 `localdata\logs` 下的安裝紀錄；不要直接複製別台電腦的 `.venv` 來補。

## 使用設定

安裝完成後雙擊：

```text
Edit_workspace.bat
```

依選單完成以下設定：

1. 加入允許寫入的 Workspace。
2. 視需要加入 Read-only 路徑。
3. 設定 Local 或 Remote Workspace。
4. 貼上要使用的 ChatGPT 一般對話 URL。
5. 若要使用 Telegram，再依引導設定及配對 Bot。

ChatGPT URL 應是可正常開啟的對話頁面。第一次啟動瀏覽器時，請自行完成登入；SmartAgent 不會代填帳號、密碼、2FA 或 CAPTCHA。

## 啟動與使用

### 主要方式：ChatGPT 網頁 Agent

雙擊：

```text
launch_webcopilot_chatgpt.bat
```

接著：

1. 貼上已設定或要使用的 ChatGPT 對話 URL。
2. 等待瀏覽器開啟並完成 protocol readiness。
3. 在 ChatGPT 對話窗輸入自然語言需求。
4. 保持啟動視窗開啟；SmartAgent 會執行核准的 action，並把結果送回同一個對話。

範例：

```text
列出 C:\project\demo 下面有哪些檔案，只讀取，不要修改或刪除。
```

```text
在 C:\project\demo 執行測試，回報 exit code 與主要失敗原因，不要修改 source code。
```

### Telegram RemoteAgent

先透過 `Edit_workspace.bat` 完成 Telegram 設定，再雙擊：

```text
launch_remote_agent.bat
```

看到接收器進入等待狀態後，保持視窗開啟即可從 Telegram 發送工作。

### 停止

一般情況可在啟動視窗按 `Ctrl+C`。若仍有背景程序，再執行：

```text
force_stop_all_agents.bat
```

## 刪除 SmartAgent

刪除下載資料夾前，先撤銷執行環境與 ACL 狀態：

1. 關閉所有 SmartAgent、瀏覽器自動化與 Telegram Agent 視窗。
2. 執行 `force_stop_all_agents.bat`。
3. 若曾開啟 ACL 保護，執行 `ACLstatus.bat off`。
4. 執行 `reinstall_smart_agent.bat`。
5. 接受 Windows UAC，依畫面輸入 `RESET`。這會撤銷 restricted executor、排程、相關 ACL 與本機 runtime；不會刪除使用者 Workspace 或瀏覽器登入 profile。
6. 流程成功後關閉命令視窗，再刪除整個 SmartAgent 資料夾。

如果 Windows 仍顯示檔案使用中，先重新開機再刪除；不要使用不明來源的強制解鎖工具。若同一台電腦還有其他 SmartAgent 安裝，不要手動刪除 `%ProgramData%\SmartAgent`，以免破壞其他安裝。

## 更新

下載新的完整 release，在新版資料夾執行：

```text
update.bat "C:\path\to\installed\SmartAgent"
```

更新只允許目標安裝處於 ACL OFF，並保留目標自己的 `.venv`、`localdata`、瀏覽器 profile 與本機設定。

## Repository 結構

```text
source/                         SmartAgent runtime、WebAgent、RemoteAgent 與共用模組
install_smart_agent/            安裝、ACL、更新與驗證腳本
config/                         protocol 與 release manifests
defaultworkspace/               預設 Workspace 目錄
skills/smartagent-onboarding/   給 ChatGPT/Codex 使用的安裝與操作 skill
install_smart_agent.bat         安裝入口
Edit_workspace.bat              Workspace、ChatGPT 與 Telegram 設定
launch_webcopilot_chatgpt.bat   ChatGPT 網頁 Agent 主要入口
launch_remote_agent.bat         Telegram RemoteAgent 入口
force_stop_all_agents.bat       停止背景 Agent
reinstall_smart_agent.bat       撤銷安裝狀態，供重裝或手動刪除前使用
legacy/                         保留但不再作為主要入口的舊版
```

## 安全與隱私

- 不要提交 `.venv`、`localdata`、`.agents`、cookies、Token、個人 ChatGPT URL 或 Workspace 絕對路徑。
- Writable / Read-only 清單是實際的本機權限邊界；只加入必要路徑。
- 不要同時讓多個自動控制器操作同一個 ChatGPT 對話。
- ChatGPT UI 或 DOM 改版可能需要重新適配 selector。
- 執行命令、寫檔與下載 artifact 都是真實的本機副作用。

## 舊版

原本的 GitHub 版本保留在 [`legacy/2026-09-02/`](legacy/2026-09-02/)，但不再作為安裝與使用入口。

## 開發狀態

SmartAgent 仍在開發中。建議先在測試 Workspace 驗證，再用於重要專案或長時間 unattended 任務。

目前 repository 尚未附開源授權；除非另有書面授權，著作權依法保留給 repository 所有者。
