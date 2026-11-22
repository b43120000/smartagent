# Action Resolution Trace + Observed-Missing Deployment Prerequisites

Base: 302c6c056e98d011a7b42c74d71501dbdd811a6a.
Trigger: Case D observed an absent deployment root in round 1, then
GIT_INSPECT requested E:\\ (outside its command request scope) and an
absent target E:\\SmartAgent_GoalDriven_IT_GH_v1 as working_directory.
Both run_command proposals were rejected before execution; without a
complete outcome trace the Planner retried or interrupted.

## Contract

Per parsed operational Planner Action, the Planner Context now includes
`action_resolution_trace` with the following three independently
attested stages:

1. `planner_requested`: the original Planner Action, with exact safe
   command text or a digest when potentially sensitive.
2. `runtime_resolved`: final disposition, canonical/rewritten Action,
   changed fields, rewrite reasons or pre-execution admission denial.
3. `execution`: exact dispatched action and result/evidence reference;
   if not dispatched, `NOT_EXECUTED` and only an *admission* diagnostic,
   never a successful execution receipt.

The full original/canonical/dispatched Action remains in the existing
Runtime-owned Provenance ledger for *executed* actions. New pre-execution
rejections remain in a bounded, request/epoch-scoped in-memory
resolution list and are projected to the following Planner decision
round; `action_resolution_recorded` emits sanitized per-Action event
metadata for the remote Runtime event log/RCA collector. An aborted
or malformed transport with no parseable operational action remains a
protocol-level rejection, not a fabricated ToolResult.

A narrow, explicit rewrite is permitted only for a single literal,
registered `git ls-remote https://github.com/OWNER/REPO.git HEAD`
`run_command/GIT_INSPECT` and only after same-Task Runtime evidence
has already confirmed the deployment target absent. If the proposal
incorrectly uses the absent target or its out-of-request-scope parent
as command working_directory, Runtime may propose the existing
bound Runtime workspace as an *equivalent CWD*, retain the command/
repository/operation unchanged, and re-run **normal**
Tool/Scope/Security/Network Admission. The rewrite itself never grants
network access, authentication, clone, deployment, or write rights.
If the alternate CWD fails admission, no process executes and the
Planner sees a rejection with actual reason rather than silent fallback.

This is not a generic free-form Git rewrite. An arbitrary Git command,
`git clone`, a different repository, a shell chain, or an unobserved
target absence is **not** eligible.

## Target absence is a prerequisite, not a download error

`deployment_path_prerequisites` separates these facts:
- target existence: CONFIRMED_ABSENT only with current Runtime evidence
- command execution CWD: must exist and pass admission
- `git clone` destination: may be absent; clone normally creates it
- clone parent: must already exist and be authorized for write
- archive integrity: remains independent of Git remote read access
- deployment mutation: requires separate source integrity and write
  authorization; nothing in this change creates destination files

## CI + expected Live observations

Windows/Ubuntu tests cover semantic preservation, CWD/target
distinction, unknown repository and command chains blocked, confirmed
absence requirement, actual `preview_tool_scope` acceptance for the
rewritten existing bound workspace, rejected/not executed receipts,
rewritten+dispatched result identity and Planner Context delivery.
Previous Source Discovery, remote access, Case D Goal, Context, and
Action Loop regressions are also run. Protocol manifest is re-sealed.

New Runtime event examples:
- `action_runtime_rewrite_applied` with
  `changed_fields=["working_directory"]`, a verified
  `target_absence_evidence_ref`, `executed=false`
- `action_resolution_recorded` with
  `disposition=REWRITTEN_AND_DISPATCHED` after an actual Tool dispatch,
  `execution_evidence_ref` and outcome
- if blocked: `disposition=REJECTED_PREEXECUTION`, `executed=false`
- `planner_context_prepared` delivers action_resolution_trace and
  deployment_path_prerequisites in a request/epoch-scoped snapshot

GitHub Repository access, ZIP extraction, E: write access and
end-to-end Case D deployment remain separate Windows Live gates and
are **not** claimed complete by these tests.
