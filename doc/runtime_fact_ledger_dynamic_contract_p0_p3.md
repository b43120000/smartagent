# Runtime Fact Ledger + Dynamic Progress P0–P3

## Baseline / changes

- Baseline: 70a41f60e1aa1e19831a71eccd86cae477ab3ed4.
- Incident: TASK-26E7AF456DFD5DA3BE25, request RR-TELEGRAM-366BE693F6AB025A.
- Round 1: accepted list_directory + inspect_directory. Native inspection reported
  DIRECTORY_ABSENCE_OBSERVED. Earlier B-P1 positive observation receipt
  reported OBSERVATION_PRODUCER_UNRECOGNIZED. Round 2/3/4 Planner emitted
  CONTINUE but no operational Action; Runtime eventually paused.
- Related earlier failures: repeated invalid_progress_payload without an
  accepted initial plan, and query_project/Progress schema repair oscillation.

## P0 — Action provenance and native facts

- Request/epoch bound immutable snapshots: original Planner action,
  fully admitted canonical action, actual action passed to ToolContext.execute,
  and native ToolResult status/reason/executed/digest.
- Full Action payload and prepared Tool Result stay in the existing
  Runtime-owned Action Ledger. Planner receives bounded snapshots with path,
  operation, safe short command (or SHA256 when sensitive/large), and finite
  typed actual data. Raw stdout and arbitrary nested tool payloads are not
  included. The action_id is the lookup reference for complete output.
- When inspect_directory returned a completed native
  DIRECTORY_ABSENCE_OBSERVED for exactly one target path, record
  directory.exists=false as an observed fact. This is NOT a terminal success,
  does not grant permission, and cannot substitute independent deployment
  verification. A denied/failed list_directory alone does NOT create that fact.
- Explicitly distinguish attempted-but-failed, executed-and-observed-negative,
  not dispatched (null return), and unknown tool outcome.
- Never bind a result with mismatched action identity or tool identity.
- Stale Action Evidence remains in historical trace but is NOT a current fact.

## P1 — Dynamic Progress Contract

- Runtime publishes the dynamic contract per Planner context:
  FULL for no accepted plan, PATCH with inherited fields for an accepted plan.
- Per-turn decision, evidence, condition selection and terminal authority never
  inherit silently. Inapplicable next_action can be null in terminal cases;
  UNKNOWN fact status cannot be represented by null.
- The *existing* Progress/Action/V9/Scope/Terminal validators remain
  authoritative. This feature changes what is advertised, not what is verified.
- The no-action text-only terminal is admitted only through the pre-existing
  trusted text-only/answer-only gate.

## P2 — Decision/Action and negative observation convergence

- A completed negative directory observation becomes OBSERVED_NEGATIVE and
  REPLAN_REQUIRED in the Action Loop. Planner remains responsible for choosing
  an authorized prerequisite strategy; Runtime never invents a source path.
- A Progress-only CONTINUE without an in-flight Runtime wait receives one
  explicit action-required guidance response with current facts and allowed
  scope. A second no-action CONTINUE pauses, regardless of text paraphrases.
- Existing pending prerequisite routing and scope/authorization gates remain.
  A denied action or unreachable prerequisite never becomes verified success.

## P3 — Regression / CI

- New synthetic, tool-envelope-grounded unit tests cover:
  native absence vs failed listing, positive bool observation, identity mismatch,
  original vs canonical vs dispatched action, null non-dispatched result,
  safe command disclosure / credential redaction, bounded return values,
  stale/scope-bound projection, conditional FULL/PATCH, no-action consistency,
  and absence-triggered replan without terminal success.
- Existing protocol repair, action-loop state, Progress reply, flexible Progress,
  PR-A and GAR Hold regressions run in the same Windows+Ubuntu Python 3.13 CI.
- SHA256 manifest includes all modified core files, new runtime module and
  regressions.

## Security / limitations

This is a code-and-synthetic-regression feature. GitHub CI cannot establish
that the user's Windows E:\ deployment root really exists or that Case D
end-to-end deploy/VERIFY succeeded. Verify on a fresh local Task, with original
Goal, actual writable authorization and independent source/target checks.
The provenance boundary records the Action passed to ToolContext.execute; a
subprocess's exact lower-level spawn arguments require separate dispatcher
instrumentation if it rewrites them after that boundary.
