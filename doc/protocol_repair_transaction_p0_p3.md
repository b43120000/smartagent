# Protocol Repair Transaction V1 — P0–P3

## Grounded incident
Case D task TASK-9350D1A86BB47B28D42A, request
RR-TELEGRAM-5700AFB7D6B90AA2, installed commit
3dd2701ca4c7cb486b9860a34ca8293a5a6d7cf2.

RCA evidence: request target successfully bound, Task Progress 1/4,
read-only Action Evidence produced. Round 4 rejected
query_project_structured_queries_required. Rounds 5–7 then rejected
invalid_progress_payload (matched_condition_id, outcome, steps). Round 8
rejected query_project_structured_queries_required again. BoundedRepair:
6 rejected turns without new evidence, PAUSED. No Terminal Goal verdict.

## P0 — Runtime-owned read-only Proposal transaction
- On a malformed query_project action, validate an associated CONTINUE
  Progress candidate *without writing Progress Ledger*, and hold only if
  the proposal is already valid, has one read-only query_project action,
  has no goal_transition, and does not claim completion.
- Hold Request ID, Task Epoch, a digest of the accepted Progress Ledger
  and the Action Result Ledger, and the proposed query Action identity.
- A later partial Progress retry may reuse the held *unadmitted* valid
  Progress only when all anchors still match, the exact read-only Action
  identity is preserved, and no explicitly changed Goal/Step/Condition/
  Evidence/Decision/next_action conflicts with the cached candidate.
- Any full admission success closes the cache. Different Request/Epoch,
  evidence, ledger revision, wrong Action or changed semantic field cannot
  reuse it. This is NOT an Action Result or a new authorization.

## P1 — Minimal PATCH inheritance
On an already accepted Progress Plan, normalize and omit only unchanged
inherited fields (base_evaluation, total_steps, current_step, steps,
completion_contract). Inherit from the accepted Ledger. Any new or
conflicting field stays explicit and goes to existing validation, not
silently corrected. Per-turn decision, outcome and evidence are never
inherited by this mechanism.

## P2 — Deterministic explicit Query schema adapter
- queries={operation:'search_text',query:'needle'} can become a one-item list.
- queries=[{search_text:{query:'needle'}}] can become an explicit operation
  item, ONLY when the operation is a finite known name and the embedded
  value is a mapping without conflicting operation.
- Never invent a missing operation or parameters, change the original
  project root, alter read/write scope, or execute a malformed read.
- Log a bounded repair event. Normal Action/Progress/Scope/V9 admission
  must run after the transformation.

## P3 — CCRC + integration regressions
Classify query_project_structured_queries_required as a QUERY field
repair. When the query is the only defect, instruct a Query-only fix plus
a minimal Progress PATCH, instead of regenerating accepted Progress.
No CCRC/Budget thresholds change: a merely converging schema is never
Action Evidence. Synthetic (NOT raw) reproduction tests query/progress
oscillation, successful execution of one correct read, and receipt-only
SUCCESS after full independent admission; negative tests assert no
cross-request/epoch/evidence reuse or forged rights.

Windows + Ubuntu Python 3.13 CI runs new integration tests and existing
CCRC, Progress, Query Project, GAR Hold, Terminal Goal, Env P0 and RCA
regressions, plus SHA256 manifest attestation.

## Live acceptance
Use the exact CI-green commit; keep ForceStop modifications at the
original 3dd2701 baseline unchanged. A fresh Case D task should issue
query_project searches through canonical queries[] without re-generating
valid accepted Progress fields. Demand actual Action Evidence before any
Terminal Goal PASS. A synthetic CI pass does not prove the original
case's missing prerequisites or Windows live deployment.
