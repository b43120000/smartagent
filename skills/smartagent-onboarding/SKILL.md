---
name: smartagent-onboarding
description: Guide a user through installing, configuring, starting, updating, troubleshooting, or removing the Windows SmartAgent release in this repository.
---

# SmartAgent Onboarding

Use this skill when a user gives you this repository and asks how to install or use SmartAgent.

## Source of truth

- The active release is the repository root.
- `legacy/` is reference-only and is not a new-install source.
- User-facing instructions are in `README.md`.
- Installation logic is owned by `install_smart_agent.bat` and `install_smart_agent/`.
- Workspace, ChatGPT URL, and Telegram configuration is owned by `Edit_workspace.bat`.
- The primary ChatGPT web entry is `launch_webcopilot_chatgpt.bat`.

## Safety contract

- Never request or display passwords, cookies, 2FA codes, CAPTCHA answers, Telegram Bot Tokens, or OAuth secrets.
- Login and account verification are always performed manually by the user.
- Do not copy `.venv`, `localdata`, `.agents`, browser profiles, or machine authorization from another computer.
- Do not enable ACL protection, change ACLs, install dependencies, or delete files unless the user has asked for that operation.
- Do not add a broad disk root as Writable. Prefer the smallest project directory needed.
- Do not use files under `legacy/` for a new installation.
- If you cannot operate the user's computer, provide exact steps and wait for the user to paste the result. Never claim that a step ran when it did not.

## Guided setup

1. Confirm the user is on 64-bit Windows 10 or Windows 11 and has internet access.
2. Confirm the repository is extracted to a normal user-writable directory.
3. Ask the user to run `install_smart_agent.bat` from the repository root.
4. If local execution is available and the user asked you to install, run the launcher from the repository root and report the first failing stage exactly. Do not bypass a failed security check.
5. After installation succeeds, guide the user through `Edit_workspace.bat`:
   - add the smallest required Writable Workspace;
   - add Read-only roots only when needed;
   - set the Local or Remote Workspace;
   - enter a normal ChatGPT conversation URL;
   - configure Telegram only if requested.
6. Ask the user to complete ChatGPT login manually when the browser opens.
7. Start the primary workflow with `launch_webcopilot_chatgpt.bat`.
8. Explain that the launcher window must stay open while ChatGPT uses local Agent actions.

## Quick verification

After setup, use a read-only request first:

```text
列出已授權 Workspace 根目錄的檔案，只讀取，不要修改或刪除。
```

Verify that:

- the correct ChatGPT conversation is open;
- the request reaches SmartAgent;
- the resolved path stays inside the authorized Workspace;
- a real result is returned to the same conversation;
- no file was modified.

## Troubleshooting intake

Ask for these items before suggesting a change:

- the exact launcher used;
- the full error text;
- `event_id`, `request_id`, and `task_id` when present;
- whether ACL mode is ON or OFF;
- the intended Workspace path and ChatGPT URL, with secrets removed;
- the relevant log excerpt from `localdata\logs`.

Classify the failure before changing code: installation, workspace authorization, browser/DOM capture, protocol parsing, tool execution, artifact delivery, or Telegram transport.

## Update guidance

For an installed package in ACL OFF mode, use:

```text
update.bat "C:\path\to\installed\SmartAgent"
```

The downloaded repository is the source. The target keeps its own `.venv`, `localdata`, browser profile, and machine-specific settings. Do not point the updater at `legacy/`.

## Removal guidance

1. Stop running agents with `force_stop_all_agents.bat`.
2. If ACL protection was enabled, run `ACLstatus.bat off` and complete UAC.
3. Run `reinstall_smart_agent.bat`, complete UAC, and type `RESET` when prompted.
4. Close command windows that use the repository.
5. Delete the SmartAgent folder manually.
6. Do not delete `%ProgramData%\SmartAgent` when another SmartAgent installation may still use it.

The reset preserves user Workspace files and the browser login profile. State inside the deleted SmartAgent folder is removed with that folder.
