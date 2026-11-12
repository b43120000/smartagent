# Git52 Runtime RCA native confirmation blockers

This Git52 change **does not** loosen command security rules or OS/AppContainer
constraints. It fixes evidence coverage for pre-execution denials.

## Native Broker decision path

When CommandAdmission denies a command, Runtime preserves the fixed
\`command_security_code\`, a boolean \`command_security_approval_eligible\`,
and a SHA256 of \`CommandAdmission.detail\`. The raw detail may contain local
paths and is intentionally not persisted. The original shell command is
also excluded from Executor Blackboard and collector bundles.

Executor Blackboard writes matching, action-scoped:
- \`TURBO_DECISION\` with \`decision=NATIVE_PRECHECK_BLOCKED\`
- \`TURBO_NATIVE_CONFIRMATION_BLOCKED\` with classifier metadata,
  \`broker_decision=BROKER_REQUIRED\`, \`executed=false\`, and
  \`authorization_granted=false\`.

This fixes the prior contradictory \`HUMAN_CONFIRMATION_REQUIRED\` audit
label when no human consent is offered.

## Collector

\`collect.bat TASK-... RR-...\` now exports:
- \`executor_turbo_decisions.json\` (with security code metadata)
- \`executor_native_confirmation_blocks.json\` (exact native precheck
  refusal and per-action digest)
- \`rca_causal_coverage.json\` (marks PARTIAL if a native command
  security rejection has no classifier code or cannot be joined to
  a matching \`TURBO_DECISION\`)

Legacy RCA ZIPs cannot gain past Runtime metadata; rerun a blocked
case with this new build to capture a specific rule code.

## Tests

Git52 unit tests verify hard-command codes and action-scoped Blackboard
events. A Windows disposable-root collector test exercises ZIP creation
and missing-code coverage. GitHub Actions runs those regressions and
PowerShell parser gate on the Git52 branch.

This release is diagnostics-only: no new Git permissions, Host CMD
fallback, unchecked auto approvals, or production-wide OS attestation.
