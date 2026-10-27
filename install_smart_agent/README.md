# SmartAgent installer helpers

此目錄集中存放根目錄 `install_smart_agent/launchers/install_smart_agent.bat` 使用的安裝與驗證檔案。

- `install.ps1`：Windows 安裝主流程。
- `acl_status.ps1`：以 `ACLstatus.bat on|off` 切換 Windows ACL 模式；`off` 保留 SmartAgent 命令安全檢查。
- `requirements.txt`：Python 依賴鎖定版本。
- `verify_environment.py`：不接觸個人資料的環境驗證。
- `install-state.json`、`install_report.txt`：執行後產生，不應提交。

一般使用者只需執行根目錄的 `install_smart_agent/launchers/install_smart_agent.bat`。

## Release build 與更新狀態

維護者應從乾淨的 Git checkout 建立完整 release artifact：

```powershell
python install_smart_agent/build_release.py --root . --output-dir C:\release-artifacts
```

Builder 會固定 source commit/tree、在隔離 staging 內重新生成 strict protocol manifest、source build provenance 與完整 update manifest，驗證每個受管檔案的 SHA-256 和 inventory，再輸出 ZIP 與 sidecar report。輸出目錄必須位於 source checkout 外，source tree 必須乾淨。

目前 root `update.bat` 刻意 fail-closed，因為受保護的更新入口尚未完成。Builder 會拒絕產生可部署 artifact，並回報 `release_update_entrypoint_not_ready`；不要直接執行 `manifest_update.ps1` 來繞過此 Gate，也不要以關閉 ACL 當作替代方案。

`manifest_update.ps1` 的目前支援範圍仍限 ACL OFF。它會驗證 package 的 protocol manifest、完整 update manifest、build provenance、release sequence 與受管檔案雜湊；只移除舊 manifest 曾管理且目前 hash 仍相符的廢棄檔案，未知或已修改的檔案會保留。ACL ON 的受保護 updater、machine authorization 更新與 restricted executor refresh 尚未完成，因此不宣稱整條 release/update 路徑已可部署。

Build provenance 目前沒有發布者數位簽章。Runtime 可以記錄 loaded module code identity 與 unsigned build commit claim，但不得將該 claim 升格為 `TaskResult.source_commit`；該欄位必須維持 `NOT_ATTESTED`，直到來源由受信任簽章或受保護 machine-authorization binding 證明。

Release sequence 由 `build_update_manifest.py` 定義，現行候選序號為 `2026102701`，高於舊安裝的 `2026102203`。Builder 負責在 staging 內依序產生 protocol manifest、provenance 與 update manifest；不要手動改寫 manifest 雜湊來通過 Gate。

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

一般安裝預設不設定 restricted executor／Windows ACL，並在每個新安裝的 `localdata\secure\windows_security\acl_mode.json` 明確寫入安裝根綁定的 `mode: off`；M4 會標示為 `SKIPPED`。同一台電腦可各自安裝多個軟體防護模式的 SmartAgent。只有明確執行 `install_smart_agent/launchers/install_smart_agent.bat -ConfigureSecurity` 才會要求管理員設定 ACL。ACL ON 使用單一全機 machine authorization 與 restricted-executor task，不支援多個獨立安裝同時啟用；不同安裝需要同時運作時，請保持各安裝的 ACL OFF。

ACL OFF 時，即使該安裝尚無 security profile，也可直接執行 `Edit_workspace.bat` 設定 workspace 和 Telegram 配對；輸入的 workspace 必須存在且目前 Windows 使用者可寫，流程不會啟動 ACL provisioning 或 UAC。

切換目前安裝的 Windows ACL 限制：`ACLstatus.bat on` 啟用安裝時的 restricted-executor ACL；`ACLstatus.bat off` 放寬 SmartAgent 管理目錄與安全 profile 所列 executor 路徑的 ACL、停止 restricted executor，並讓啟動與執行流程略過 restricted-executor/profile attestation，但保留 SmartAgent 軟體層的命令與路徑安全檢查。兩種切換都需要 UAC；切換後應重新啟動 SmartAgent，使所有行程讀取同一個持久化模式。

`off` 是便利安裝／除錯模式，不是受保護模式：目前 Windows 使用者與 executor 可修改受管程式檔與 ACL 管理目標；命令檢查仍會執行，但本機管理者可修改程式或安全 profile，不能視為防竄改邊界。

若複製來的安裝目錄因既有 ACL 無法建立 `.venv` 或寫入安裝資料，先在該目錄執行 `ACLstatus.bat off`，再執行 `install_smart_agent/launchers/install_smart_agent.bat`。若安裝流程套用了 restricted-executor 設定，會依持久化的 `off` 模式在設定完成後恢復 ACL 放寬狀態。

只驗證：`install_smart_agent/launchers/install_smart_agent.bat -ValidateOnly`
