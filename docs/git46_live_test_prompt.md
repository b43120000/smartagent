# Git46 Live acceptance prompt — two gates, NO destructive real-project operations

Git46 branch: `git46/turbo-os-sandbox-fail-closed-20261110`.
Git45 base: `090d2d9961c339a6f2432431fab66f082b6572d4`.

## Phase A — current branch (safe fail-closed only)

Work on the installed Windows SmartAgent instance. Do not run commands that
write, delete or overwrite real projects. Do not modify the source project,
Case D fixtures, ACLs, user registry or system security settings as a workaround.

1. Check exact running Git46 commit and display the shared policy. Show
   `acl_mode`, `turbo_requested`, `sandbox_status`,
   `sandbox_attestation_passed`, `turbo_effective`, `policy_revision`,
   and `reason_code`. Use the shared status resolver, not guessed UI state.
2. With ACL OFF, attempt the explicit setting
   `turbomode=E:\SmartAgent_GoalDriven_IT_GH_v1` (only if configured as an
   authorized child of `workspace=E:\`). It must return
   `TURBO_REQUIRES_ACL_ON` and must not create a full-access session.
3. With ACL ON, explicitly request the same Turbo root. The current Git46
   backend is intentionally unavailable. Report Requested=ON,
   Effective=OFF, Sandbox=NOT_READY, Reason=SANDBOX_UNAVAILABLE.
4. Propose a **read-only** Turbo run_command, e.g. `git --version`. It
   must be **BLOCKED before any process is dispatched**, not succeed under
   the ordinary restricted executor. Confirm Blackboard event
   `TURBO_SANDBOX_BLOCKED`, `executed=false`,
   `os_execution_result=NOT_EXECUTED`, `os_access_denied=false`.
5. Confirm Telegram and WebGPT Workspace Manager show the same effective
   state. Export `collect.bat TASK-ID REQUEST-ID` from a matching task
   and inspect `executor_sandbox_evidence.json`; raw commands, tokens and
   sensitive paths must not appear.
6. With ACL ON -> OFF, verify Turbo roots/request are cleared and effective
   remains OFF. With ACL OFF -> ON, verify Turbo stays OFF until explicit
   re-request. No old Sandbox lease may survive a policy revision.
7. Explicitly set `turbomode=off` and confirm ordinary Git45 non-Turbo
   read-only commands still work. Restore intended ACL operating mode.

A software-level BLOCKED result is expected in Phase A. It is **not**
evidence that Windows rejected an out-of-root file operation. If any
Turbo command really runs while no OS Sandbox broker is attested,
report a CRITICAL FAIL and stop.

## Phase B — future independent Windows OS sandbox implementation ONLY

DO NOT RUN PHASE B until a disposable Windows fixture, distinct identity,
verified AppContainer/NTFS backend, and automated hard-fail preflight exist.

Use temporary disposable paths **different from** real
`E:\SmartAgent_GoalDriven_IT_GH_v1`. The fixture must include a Turbo
root, parent sentinel, same-workspace sibling sentinel, different-volume
sentinel, hard link, symlink/junction, and a root-delete probe.

Require evidence for:
- inside read/create/overwrite/delete/recursive-delete + git clone + Python
  subprocess + compiler and DLL loading;
- out-of-root direct Win32 File API read/write attempts under the actual
  isolated child token returning OS `ERROR_ACCESS_DENIED (5)`;
- PowerShell/CMD/child process absolute-path denial, links/hardlinks, root
  deletion, reboot, concurrent revocation and policy revision changes;
- explicit cause if a test cannot run: UNAVAILABLE, not PASS.

Every test record must separate
`software_admission_verdict` from `os_execution_result` and
`os_access_denied`, with PID, token/AppContainer SID attestation,
policy digest, command digest and test fixture identifiers. Redact secrets.
Do not launch Phase B against any real project or unisolated executor.

## Reporting format

Return a matrix with separate columns:
Unit/Contract, Windows OS Sandbox, Real-machine Live.
For each, state PASS/FAIL/BLOCKED/NOT RUN, observed evidence reference,
the concrete failed API/operation and the unfulfilled guarantee.
Do not report overall Sandbox PASS unless all OS boundary cases pass.
