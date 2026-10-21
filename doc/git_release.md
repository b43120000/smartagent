# Portable Git release workflow

Publication checkout: `E:\card\release\SmartAgentv2`.
Execution/development checkout: `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`.

Root operation entries are ACLstatus.bat, Edit_workspace.bat, force_stop_all_agents.bat, launch_remote_agent.bat and launch_webcopilot.bat. Installer/check/calibration support entries live under `install_smart_agent/launchers`; calibration stages also live under `adapterUI`. Main instructions are in `doc/README.md`.

Run `install_smart_agent\launchers\ensure_portable_runtime.bat` to extract and validate bundled Python/Git and refresh `localdata/metadata/environment_ledger.json`. This needs no machine Python/Git. Extracted runtimes, credentials, registry, logs and `.agents` remain local. Only immutable `.venv/portable-runtime` and `.venv/portable-git` payloads enter Git. Official Git URL, version, SHA256 and source/license references are in the Git payload manifest; archive includes upstream notices.

Use `.venv\git\cmd\git.exe` to review `diff`, stage explicit application paths and `commit` in the execution checkout. Transfer a reviewed commit with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install_smart_agent/git_release.ps1 -Commit <commit-SHA>
```

The transfer uses bundled Git `fetch` and `cherry-pick -x`; it requires a clean destination, detects replay, preserves a conflict for review, and never pushes. Resolve conflicts using Git `status`, edit and stage the affected files, then `cherry-pick --continue`; use `cherry-pick --abort` to cancel. Configure Git user.name/user.email before making commits. Do not run update1.bat or sync_back.bat; retired batch files are preserved only in ignored localdata on existing machines.

Default provider DOM files remain versioned. Other URL calibration is origin-specific; never carry credentials/browser state into publication. Tests and verification programs remain under source and installer subdirectories; generated evidence belongs in localdata.
