# Git41: Same-task command-family consent

Base: Git40 `bc0d1ccf099dd419994ce0e9473aa8dfdf7f9c04`.

## Behavior

1. Telegram and WebCopilot now explicitly display **「允許本任務同類指令」** rather than claiming a single exact-command grant for UNKNOWN_COMMAND and resume challenges. EXECUTION (external executable) and DELETE approvals keep their existing once-only semantics.
2. A suspended command is still approved and consumed by exact matching through the Git40 pre-resume gate. No command executes before the original user selection.
3. Only **after** a newly admitted UNKNOWN_COMMAND action returns a concrete `SUCCEEDED` ToolResult with `executed=true`, the Executor records a one-hour command-family consent in protected Security state. It is bound to Task ID, request ID, task epoch, user/transport owner and Workspace.
4. A subsequent UNKNOWN_COMMAND in this **same task** can reuse the decision, provided the executable, script, operation and all non-path options/arguments match. Plain path arguments may differ only when they are contained in the same Workspace. A reused consent emits `HUMAN_APPROVAL_REUSED` and a receipt reason `same_task_command_family_reused`.
5. Each action still passes new Action Admission, Scope, command security, Tool execution and verification. No filesystem/network/ACL grant is implied by the family match, and no changes to a task's Write Ceiling are made.

## Fail-closed boundaries

- Does not follow the user to a different Task, run, epoch, owner or Workspace. Expires within one hour; no permanent allowlist.
- Never generalizes changed Python/Node scripts, executables, package module execution, PowerShell/CMD script strings, compound shell operators or arbitrary code expressions.
- Never generalizes deletion, privilege management or other excluded high-risk command families.
- Treats Git repository URLs and all other non-path arguments as literal, not placeholders; Git destructive subcommands are not generalized.
- A changed path outside the Workspace cannot use a family grant; ordinary policy may still refuse execution regardless of approval.
- No family grant is created on denied/expired approvals, rejected Admission, execution failures, timeout or unverified outcomes.

## Examples

`Get-ChildItem -LiteralPath "E:\\Lab\\case_a"` → `Get-ChildItem -LiteralPath "E:\\Lab\\case_b"`: same family if Workspace is `E:\\Lab`.

`python check.py E:\\Lab\\case_a` → `python check.py E:\\Lab\\case_b`: same family in the same working directory.

`python check.py` → `python other.py`, changed flags, a different Task ID, a different Git URL or a path outside the Workspace: new explicit permission required.

## Verification

Git41 CI compiles the new module and Executor and runs focused family-grant tests along with Git40, Git39 and minimal Executor regressions on Windows and Linux. CI does not replace a real Telegram / WebCopilot Live test.
