# Git52 P0–P2: Native executable trust, bounded recovery, Planner capabilities

Base: git52/turbo-full-access-approval-20261112. The next phase, Case D
**Live P3**, is deliberately not claimed completed by this change.

## P0 — Precise executable/script trust, no OS permission bypass

- Ordinary ACL OFF admission remains unchanged. Hard command bans still win.
- \`inspect_command()\` checks **all** recognized externally invoked target paths,
  not only the first path of a compound command. Denial reports a safe
  SCRIPT/EXECUTABLE kind without exporting the actual path.
- Native Turbo uses \`inspect_native_command()\`. Explicitly invoked executable
  and script paths **outside the Turbo Root** require exact operator-maintained
  \`trusted_executables\` SHA256 entries in the Windows security profile.
- \`is_sha256_pinned_executable()\` validates the exact path and file contents
  without the ACL OFF shortcut. Missing or changed files fail closed.
- Existing in-root commands still go through Action Admission, Command Security,
  the native broker, and OS attestation. Bare PATH command resolution and
  indirection in free-form shell grammar are **not** proof of a pinned binary;
  there is no claim of identity attestation solely from software parsing.
- No new trusted records are auto-added. A tool being present or mentioned by
  the Planner never modifies the operator security profile.
- Native broker enrollment and its per-action kernel proof remain mandatory.
  No Host CMD or ordinary Executor fallback is introduced.

## P1 — Denied native precheck -> bounded Planner replan

For an admitted \`run_command\` rejected *before execution*:

- Only \`TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED\` +
  \`execution_target_outside_workspace\` +
  \`command_security_approval_eligible=true\` may request a different action.
- The denied action is **not dispatched**, does **not** get a successful
  ToolResult, and cannot count as filesystem observation or execution proof.
- Executor writes \`TURBO_NATIVE_CONFIRMATION_BLOCKED\` then, when eligible,
  \`TURBO_NATIVE_REPLAN_REQUESTED\`. It passes only typed codes and digests
  to the next Planner turn, never the raw shell command.
- The same rejected command digest cannot be re-proposed; at most three
  distinct denials can be replanned within one Executor task. Hard
  command-security restrictions, out-of-root CWD, unverifiable policy and
  unavailable native Broker remain terminal BLOCKED.
- A replacement command is fully readmitted and still OS-attested. Replanning
  does not grant permissions, write outside the Turbo Root, or suppress normal
  test failure evidence.

## P2 — Planner capability/trust hints

\`turbo_planning_snapshot()\` now attaches \`trusted_native_tool_hints\` for
operator-pinned Git, Python, PowerShell Core and tar candidates:

- \`operator_sha256_pin_present\`: configuration exists.
- \`trusted_executable_verified\`: on-disk SHA256 matched the pin.
- \`native_execution_verified\`: always **false** in an advisory snapshot;
  only the actual executed action's native OS receipt can substantiate runtime
  execution.

This is **not** the path, invocation, OS authorization, or a credential. A
bare command that happens to resolve in PATH is not implicitly trusted.

## Diagnostics

\`collect.bat\` exports \`executor_native_confirmation_blocks.json\` with
classification kind and replan decision and the new
\`executor_native_replans.json\` with matching Action IDs and hashes.
\`rca_causal_coverage.json\` reports PARTIAL if a marked replan event is
missing or cannot be linked.

## P3 Live validation checklist (operator)

1. Verify the production checkout matches this release SHA; restart Runtime.
2. Verify ACL ON + Turbo ON and Turbo Root
   \`E:\AIspace\SmartAgent_Turbo_Test\`.
3. Run Case D under normal Runtime, not Telegram Host CMD.
4. Before any write, independently inspect existing root files. No deletion,
   rename, overwrite or cleanup of existing user data.
5. If an executable is not pinned, expect a preexecution denial and Planner
   replan, not a human permission prompt or ordinary Executor fallback.
6. Every **actually executed** command must provide WIN32_APPCONTAINER,
   identity_verified=true, kernel negative-access proof and exit-code result.
7. Verify commit, fixture SHA256, root-level five entries, manifest and source
   CSV hashes independently after deployment.
8. If blocked, collect the new Task and Request IDs via collect.bat and inspect
   \`executor_native_confirmation_blocks.json\` and \`executor_native_replans.json\`.
9. A Finish comparator generic PASS is not proof of all Case D conditions.
   Missing independent evidence must be reported UNVERIFIED.

This change is not a security assessment of general PowerShell program
semantics or an assertion of OS production Full-Access certification.
