# Git45 — P0 Turbo admission and evidence; P1 Finish status repair

Base: Git44 `9997344535f3601f479326d22c7fc52180fc9523`.

## Scope of this change

**In scope**: generic Turbo secondary UNKNOWN-command confirmation policy, actionable audit records for every proposed `run_command`, and nonexecuting repair of model-authored Finish status mismatches.

**Out of scope**: LF/CRLF diagnosis, Case A/B/C/D-specific acceptance rules, rewriting CSV files, bypassing existing Workspace/ACL/read-only/write-ceiling gates, and automatic permission for arbitrary scripts. Source contents must be compared with the exact Git commit by the Planner's Live prompt, not by hardcoded Runtime rules.

## P0 — Turbo execution and scope

Turbo remains opt-in. The shared `workspace_access_policy.json` has to contain a `turbo_roots` descendant of the active RemoteAgent Workspace. The Executor evaluates every command *after* canonical Action Admission and records the outcome even when Turbo is OFF.

Recognized **single** commands (and their conditions):

- `git clone https://... <new-target>`: HTTPS source URL must appear in the original user request, target must be within the active Turbo directory, absent, and have an existing parent.
- `git -C <in-scope-repo> archive --format=tar --output=<new-in-scope-tar> <request-pinned-sha40> [subdir]`: verified local Git directory, commit in user request, path-bound new archive destination; no existing file overwrite.
- `tar -tf <in-scope-archive>`: single local tar inventory command.
- `tar -xf <in-scope-archive> -C <existing-empty-turbo-subdir>`: only after tar member checks (normal files/dirs, no symlinks/hard links, no parent traversal, no duplicate/colliding members, bounded archive/expanded size).
- Narrow, literal read-only Git and PowerShell commands; `certutil -hashfile` and `git hash-object --no-filters --` exact SHA verification syntax is also classified as proven read and does not require the UNKNOWN-command popup.

The eligible result is **only** a secondary UNKNOWN-command confirmation decision; it cannot bypass command security checks, independent process/executable permission, restricted ACL, working directory scope or Write Ceiling. A command considered `MUTATING` by the existing classifier follows normal mutation authorization and verification; a known read command follows its existing proven-read path.

**Not allowed without further authorization**: `git reset`, `git clean`, arbitrary `git checkout`, overwrite, existing-target clone, tar extraction into a nonempty directory, arbitrary Python/script execution, composite pipelines, command substitution, paths outside Turbo, and dynamic shell syntax. The absence of the word `delete` is not proof of harmlessness. This is not a fully sandboxed no-delete execution mode; preflight cannot eliminate TOCTOU/external executable behavior.

## P0 — Auditor / Blackboard

Every attempted `run_command` gets an `SMARTAGENT_TURBO_DECISION_V2` event (including previously rejected admission), with:

- `risk_class`, `admission_status`, `auto_confirm`, `decision`, `reason_code`, `policy_revision`.
- `workspace_root`, `working_directory`, `turbo_root`; these are protected Runtime Blackboard fields.
- `command_sha256`, `arguments_sha256`, `authorization_granted=false`.

An eligible UNKNOWN command additionally gets `TURBO_AUTO_ADMITTED`; `ACTION_ADMITTED` and `TOOL_RESULT.runtimeEvidence` identify the Turbo decision and its Actual ToolResult. `TURBO_AUTO_ADMITTED` is never evidence that a command actually executed.

The Live RCA collector `collect.bat` exports `executor_turbo_decisions.json` with bounded reason codes and SHA256 digests **instead of raw paths or command text**, plus policy revision and counters in `executor_admission_summary.json`.

## P1 — Finish evidence and wrong-status repair

The Executor prompt includes a bounded, Runtime-owned catalog of **all actual ToolResult statuses** instead of only the six most recent ToolResults, so earlier `REJECTED` and `FAILED` are distinguishable at Finish.

If and only if every `MODEL_RUNTIME_MISMATCH` discrepancy is a typed `fields.status` mismatch, e.g. model claims `FAILED` but Runtime recorded `REJECTED`, the Executor gives the Planner a bounded **nonexecuting, Finish-only** repair turn and a `SMARTAGENT_FINISH_STATUS_REPAIR_V1` diagnostic. Events use `MODEL_FINISH_STATUS_REPAIR`, and `collect.bat` includes typed `status_corrections`.

The Runtime never edits or upgrades evidence, never converts `REJECTED` into an executed `FAILED`, never alters user criteria, and never turns a real file integrity FAIL into PASS. Any remaining or repeated Finish mismatch follows the existing strict Comparator FAIL policy. Nonstatus mismatches are not repaired under this special case.

## CI and Live validation

Git45 branch workflow: `.github/workflows/git45-turbo-and-finish.yml` runs on both Windows and Ubuntu, compiles modified sources, runs Git45 targeted tests, Git43/44 regressions, prior Executor/Finish tests, ACL OFF compatibility, and validates `collect.bat` PowerShell syntax on Windows.

Live on Windows must separately confirm:

1. Start RemoteAgent with a Workspace whose child is an explicitly configured Turbo root.
2. Run one short, safely scoped read command and a new-target Git clone in Turbo. Verify no unnecessary UNKNOWN-command confirmation, but still require exact original URL/scope/normal Admission.
3. Run disallowed Git or nonempty-target `tar -xf` and confirm fail-closed. Check no existing files were overwritten.
4. Inspect fresh `collect.bat TASK-... RR-...` archive, verify `executor_turbo_decisions.json` has the same decision and reason as Blackboard.
5. Exercise a `REJECTED` ToolResult alongside a successful one, submit an incorrect `FAILED` Finish, verify the model-only repair and true postcondition evidence.
6. For Case D, have the Live Prompt independently investigate any LF/CRLF SHA mismatch without modifying existing files.

**CI PASS is not a claim of Windows/Telegram Live PASS or Case D deployment PASS.**
