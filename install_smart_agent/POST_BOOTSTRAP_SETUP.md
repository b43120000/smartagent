# SmartAgent deterministic milestone provisioning

This is the mandatory Phase 1 contract after `install_smart_agent.bat` has made WebCopilot available.
Software scripts own the installation. WebCopilot only runs the fixed loop below and assists when a milestone reports a concrete compatibility failure.

## Fixed milestones

| ID | Milestone | Completion signal |
|---|---|---|
| M0 | WebDirect Bootstrap | Minimal virtual environment and WebDirect verifier pass |
| M1 | Full Python Dependencies | Every required module imports successfully |
| M2 | Playwright Chromium | A headless Chromium process starts and exits successfully |
| M3 | Lightweight Runtime | WebGPT runtime prerequisites are ready |
| M4 | Restricted Executor Security | Security profile and scheduled task verify, or the saved option marks it skipped; the default install leaves ACL protection disabled |
| M5 | Final Validation | Packaged environment/finalization checks pass; development layouts also run every required regression self-test |

`PASS` and an explicitly configured `SKIPPED` are complete states. `PENDING`, `FAIL`, `BLOCKED`, and `NEEDS_USER_ACTION` are incomplete.

## Required loop

Run all commands from the SmartAgent application root.

1. Read the current live status:

   `InstallCheckList.bat --json`

2. If `overall` is `PASS`, stop. Installation is complete.
3. Otherwise read `next_milestone` and run only that milestone:

   `powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install_smart_agent\install_milestones.ps1 -Action Install -ProjectRoot . -Milestone Mx -NonInteractive`

4. Run `InstallCheckList.bat --json` again. Never infer success from the installer exit code alone.
5. Repeat until `overall` is `PASS`.

## Failure and user-action policy

- Do not edit user projects.
- Do not invent package names, versions, installer commands, registry changes, ACL changes, or security exceptions.
- Do not edit `install-state.json`, `milestone-state.json`, or `provisioning_state.json` to manufacture success.
- If a milestone fails, report its exact `reason_code`, detail, command exit code, and `install_smart_agent\install_report.txt` location.
- Compatibility work must stay inside the failing milestone and must be followed by the same checklist command.
- If M4 reports `NEEDS_USER_ACTION` or `NEEDS_ADMINISTRATOR`, ask the user to approve the Windows elevation/security step. Never bypass it.
- Do not enable M4 merely because another SmartAgent installation has a machine-wide authorization. ACL provisioning is opt-in through `install_smart_agent.bat -ConfigureSecurity`.
- Login, 2FA, CAPTCHA, and Windows UAC remain user actions.
- Stop after three failed attempts of the same milestone and report the blocker instead of looping indefinitely.

On success, respond exactly and concisely:

`Installation complete. Run Edit_workspace.bat to choose the normal workspace and ChatGPT conversation.`
