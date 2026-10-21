# Portable Python/Git release verification

2026-10-22 Asia/Taipei.

The package includes immutable offline CPython 3.13.16 + pip 26.0.1 and official MinGit 2.56.0.windows.2. Startup verifies archive/binary hashes and probes real executables before recording availability. Existing compatible virtual environments remain usable. Interrupted owned installs preserve partial directories and recover; incompatible user environments fail explicitly without replacement.

The five root operation entries are ACLstatus.bat, Edit_workspace.bat, force_stop_all_agents.bat, launch_remote_agent.bat and launch_webcopilot.bat. Installation/calibration support is under install_smart_agent/launchers and adapterUI. Git metadata stays hidden at root. Use install_smart_agent/git_release.ps1 to fetch and cherry-pick reviewed commits; old update1/sync_back entry points are retired.

Planner facts come from the environment ledger. The command executor prioritizes bundled tools. Missing dependencies follow local discovery, authorized installation, explicit verification and a new-action-ID retry. All four built-in providers and origin-isolated Other URLs use the shared binding validator; entry URLs resolve once and then pin the actual conversation/provider identity.

Published DOM defaults are dom_config.default.json. Per-machine dom_config.json overrides, DOM history, credentials, browser/session settings, logs, extracted runtimes and generated test projects remain outside Git. Existing local calibration takes precedence and is never overwritten.

## Verification evidence

- Fresh offline path with spaces: 26 checks PASS (no host Python/Git on PATH), including extraction twice, imports/pip, raw protocol hashes, five entry self-tests, isolated missing-wheel install/import retry, Git init/commit/clone/fetch/cherry-pick/replay and dirty-destination rejection.
- Real Runtime dependency loop IT-R: PASS, five rounds, four ledgered discovery/install/VERIFY/retry actions. The planner is deterministic; execution, security checks, pip, filesystem and import evidence are real. This is not a live-model test.
- Remote provider/entry/pinned-identity and browser routing tests: 18 PASS. File capability tests: 12 PASS. Publication privacy test: PASS, including immutable Git payload allowed and extracted Git/browser/local DOM state excluded.
- Actual isolated Chromium DOM/Trusted Types/Other-origin tests: 8 PASS in the implementation run.
- Installer injected-interruption recovery, incompatible user-venv preservation and compatible existing venv checks: PASS.
- Live website composer readiness: ChatGPT, Gemini and Claude PASS after waiting for client-side rendering. M365 redirects to Microsoft sign-in: BLOCKED_LOGIN. No message was sent by those read-only readiness checks. This does not establish full multi-provider end-to-end conversation success.

Raw evidence stays in ignored localdata. M365 live conversation verification requires an authenticated M365 browser session. No credential or browser profile is included in the release.
