# Git44 — Telegram WebGPT / Workspace Manager Turbo field

Base: Git43 `1f051bfe136500074374286fd4d59fdeccfd2b4a`.

This change extends the existing authenticated, task-idle, 10-minute
`[WEBGPT_WORKSPACE_UPDATE]` Telegram transaction. It does not add a
separate bot button, duplicate Turbo state, change project_sync, or weaken
the Workspace/ACL/Write Ceiling checks.

## Operator workflow

In the Telegram private bot chat tap **查詢遠端功能** -> **WebGPT 與 workspace 管理器**.
The software returns a transaction-specific form:

```text
[WEBGPT_WORKSPACE_UPDATE]
transaction_id=WWM-<generated transaction id>
webgpt_url=https://chatgpt.com/c/<conversation id>
workspace=E:\Workspaces
turbomode=off
skill_path=C:\Users\user\.codex\skills
[/WEBGPT_WORKSPACE_UPDATE]
```

The default is **`turbomode=off`**. If Turbo was enabled for that
binding Workspace, the form displays its current real path, read from
the common Workspace policy. Operators may replace the line with an
absolute path that is a strict child of the Workspace:

```text
workspace=E:\
turbomode=E:\SmartAgent_GoalDriven_IT_GH_v1
```

A form with `workspace=C:\Users\user\Desktop\remoteagent` and
`turbomode=E:\SmartAgent_GoalDriven_IT_GH_v1` is **rejected**:
the Turbo directory is not a child of the requested Workspace. Set the
Workspace to the matching authorized writable root, or leave
`turbomode=off`.

The result message includes the current effective Turbo setting.
`Edit_workspace.bat` reads the same
`localdata/secure/windows_security/workspace_access_policy.json`
`turbo_roots` list, so a subsequent manager view sees the change.

## Transaction safety

* The Telegram update form remains bound to one authenticated chat,
  a one-time transaction ID and a 600-second expiry.
* Existing, trusted URL, writable Workspace and skill path validation
  still applies.
* Turbo path is validated against the *candidate* binding Workspace
  before any callback updates the RemoteAgent binding.
* Runtime Host independently revalidates the form's chosen Turbo
  mode, performs its task-idle/OS preflight and hot-applies the binding.
* Turbo shared-policy commit is the final Host step, guarded by a
  revision compare-and-swap; a concurrent edit is rejected rather
  than overwritten. If commit fails, the existing binding rollback
  runs.
* `turbomode=off` disables Turbo only in the form's Workspace; Turbo
  roots under other configured Workspaces are left intact.
* A pre-Git44 form without `turbomode` is still accepted and makes
  **no Turbo change**.
* A Markdown code fence wrapped around exactly the standalone
  `turbomode=...` line is accepted to accommodate Telegram copy/paste.
  The returned form itself uses plain lines without fences.
* No model-authored tool call may grant Turbo. The registry remains
  authoritative; a setting does not bypass ACL, Write Ceiling,
  destructive-operation protections or independent Runtime validation.

## Verification

Run Git44 Windows and Ubuntu CI and then confirm in live RemoteAgent:
1. Open the Telegram **WebGPT 與 workspace 管理器** control
   and verify it shows `turbomode=off`.
2. Apply an authorized Workspace child Turbo path; confirm result.
3. Open `Edit_workspace.bat` -> [1] and verify that same child path.
4. Remove it via Workspace Manager -> [6] + [7], request a fresh
   Telegram form, verify `turbomode=off`.
5. Send a stale transaction or out-of-Workspace path; confirm explicit
   rejection with no binding change.
6. Ensure the original `WEBGPT_WORKSPACE_UPDATE` URL, Workspace and
   skill path updates still work.

CI is not a substitute for the live Telegram and Windows ACL checks.
