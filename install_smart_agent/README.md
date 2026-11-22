# SmartAgent installer helpers

此目錄集中存放根目錄 `install_smart_agent/launchers/install_smart_agent.bat` 使用的安裝與驗證檔案。

- `install.ps1`：Windows 安裝主流程。
- `acl_status.ps1`：以 `ACLstatus.bat on|off` 切換 Windows ACL 模式；`off` 保留 SmartAgent 命令安全檢查。
- `requirements.txt`：Python 依賴鎖定版本。
- `verify_environment.py`：不接觸個人資料的環境驗證。
- `install-state.json`、`install_report.txt`：執行後產生，不應提交。

一般使用者只需執行根目錄的 `install_smart_agent/launchers/install_smart_agent.bat`。

## 更新既有安裝

下載新的完整 release 後，從新 release 執行根目錄 `update.bat`，再貼上目前實際使用、已完成 Windows 授權的 `SmartAgentv1` 路徑。更新器會先驗證下載包的 protocol manifest 與完整檔案 manifest，並驗證目標路徑就是 `%ProgramData%\SmartAgent\machine_authorization.json` 綁定的安裝位置；任何驗證失敗都會在停止服務或寫入前中止。正式覆寫前，release 會先複製到 `%ProgramData%\SmartAgent\updates` 的管理員專用候選目錄，再次完整驗證後才部署，避免驗證與複製之間讀到不同內容。

更新會替換 application-owned code；若舊安裝已有 `update_manifest.json`，也會移除該 manifest 已列出、但新版已廢棄的受管程式檔。第一次從沒有 update manifest 的舊版升級時，無法安全判定額外檔案是否屬於使用者，因此只清理由新版受管目錄內 `/MIR` 可確定的舊檔，不會猜測刪除未知的頂層檔。更新器會沿用既有 security profile 刷新 restricted executor code 與 machine authorization。它不會執行完整 install/reinstall，也不會重建 executor SID、旋轉排程憑證、清除 Telegram token 或要求重新掃描 QR。

以下內容永遠保留：`localdata` 使用者資料、`.venv`、`config\debug_config.json`、`.git`、`.agents`、使用者捷徑，以及 `%APPDATA%\WebLLMScraper`。更新需要一次 UAC，因為受保護 executor 與 ProgramData 授權只能由系統管理員更新；取消 UAC 時不會修改目標。更新若被斷電或強制終止，下次執行會先使用 ProgramData 中的交易備份回復舊版。若 requirements 或 security policy 版本改變，更新器會在寫入前回報 `UPDATE_MIGRATION_REQUIRED`，不會嘗試隱性修改環境或安全邊界；較舊 protocol 版本也不得覆蓋較新安裝。

第一次成功更新會在 `%ProgramData%\SmartAgent\updater` 發布 ACL 保護且由 machine authorization 綁定雜湊的 updater 副本；後續 `update.bat` 的 UAC 階段改由這個副本執行。首次導入時尚無受保護副本，因此該次仍必須信任目前下載包。更新只刷新 executor code、啟動既有排程並更新 authorization，不會重算 workspace/runtime ACL 或 trusted-executable allowlist。

manifest 雜湊可確認同一 release 內的檔案一致性與完整性，但目前 release 尚未使用發布者數位簽章，因此不能單靠 manifest 證明下載包的發布者身分。只應從可信來源取得 release，並在 UAC 視窗確認是自己剛啟動的更新。若未來要做到無互動更新或更強的來源鑑別，必須加入簽章 release 與受保護的 SYSTEM updater；不能用略過 UAC 取代。

發布新版前，維護者必須先遞增 `build_update_manifest.py` 的 `RELEASE_SEQUENCE`，再依序重建 `protocol_manifest.json` 與 `update_manifest.json`。更新器只接受 sequence 大於目前已安裝 release 的套件，避免同一 protocol 世代被舊包降級。

SmartAgent 直接在目前資料夾內建立執行環境；父資料夾名稱與位置不影響安裝。啟用 Windows restricted executor 時，預設安全路徑為：

- `%USERPROFILE%\SmartAgentWorkspaces\default`：Agent 可讀寫、建立與刪除。
- `<SmartAgentv1>\localdata\secure\windows_security\skills`：從 `%USERPROFILE%\.codex\skills` 複製，Agent 僅能讀取與執行。
- `<SmartAgentv1>\localdata\secure\windows_security\executor_code`：Agent 僅能讀取與執行。
- `<SmartAgentv1>\localdata\secure\windows_security\security_profile.json`：Agent 僅能讀取。
- `<SmartAgentv1>\localdata\runtime\security`：restricted executor 的佇列與服務狀態。

Telegram 的 `localdata\secure\telegram.enc` 不在 Windows security ACL 子邊界內，修改 workspace 或重新套用 M4 不會要求重新配對。

完成 M4 後，系統會在 `%ProgramData%\SmartAgent\machine_authorization.json` 建立唯讀的本機授權索引。相同 release 的其他資料夾複本會共用索引指向的 `security_profile.json`、restricted executor 排程與 runtime，不需要重新建立 Windows 帳號。索引同時綁定 protocol 版本與 `protocol_manifest.json` 雜湊，不相容的程式包不會共用。

`install_smart_agent/launchers/reinstall_smart_agent.bat` 是完整 Windows restricted-executor 撤銷流程：它會要求 UAC 與輸入 `RESET`，移除共用排程、本機 executor 帳號、相關 ACL、機器授權索引、安全 profile 與 runtime。Telegram 配對、使用者 Workspace、bindings 與瀏覽器 profile 預設保留。

複製到另一台電腦時，請複製程式檔，但不要複製 `.venv`，且 `localdata` 目錄必須存在但保持完全空白。尤其不得攜出 `localdata\secure`、`localdata\bindings`、`localdata\persistent`、`localdata\runtime`、`localdata\logs`、`localdata\cache`、`localdata\metadata` 或 `localdata\temp` 的內容。`%ProgramData%\SmartAgent` 位於安裝包外，不會隨資料夾複製。

一般安裝預設為 ACL OFF。要啟用 Windows restricted executor，請在要使用的安裝目錄執行 `ACLstatus.bat on`。同一台電腦只有一筆有效的 machine authorization 和 restricted-executor 排程。

要切換 ACL 所屬安裝路徑，先執行 `ACLstatus.bat off`。此指令停止並移除已驗證的 SmartAgent 排程、撤銷 Turbo 和記錄中的 executor SID 權限，並清除目前的全機授權與安全 profile。工作區設定、執行環境、使用者檔案和對話綁定都留在原處。若舊版的授權範圍記錄不完整，畫面會列出共用該帳號的排程與服務，要求輸入 `REVOKE` 停用舊帳號。依賴舊帳號的排程在帳號停用期間無法執行。

再從要使用的安裝目錄執行 `ACLstatus.bat on`，確認列出的工作區與安全設定後輸入 `APPLY`。ON 會先檢查該版本的 protocol manifest，並以目前安裝目錄重新建立及驗證受限執行器。重新啟動 SmartAgent 後，所有行程會重新讀取已持久化的 ACL 模式。

ACL OFF 使用 SmartAgent 軟體層的命令與路徑檢查，適合便利安裝及除錯。目前 Windows 使用者與管理員仍可改動受管程式與設定，所以這個模式不具備防竄改保護。可執行 `Edit_workspace.bat` 設定現有且可寫的工作區和 Telegram 配對。

只驗證：`install_smart_agent/launchers/install_smart_agent.bat -ValidateOnly`
