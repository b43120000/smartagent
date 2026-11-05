# Tri-One Phase 1 — Verification Matrix, UT / IT / Live Acceptance

> **Test SPEC only. No test has been executed by this document.**  
> Parent: [implementation plan](tri_one_phase1_shared_completion_text_only_fast_path_plan.md).  
> Source planning baseline: `main@b95be77876ac522248954f3127d4fb7b1024eed2`.  
> Scope: 2A → 5A → 5B. Test IDs are planned definitions, not historical Runtime IT01–IT23.

## 1. Testing vocabulary

- **UT:** pure isolated functions, mock stores/transports and synthetic state; no real Browser, Telegram or user task execution.
- **IT:** several real runtime components with *isolated sandbox and deterministic fake Planner*. Preserve V9 parse, progress, identity, admission, ledger, outbox and Terminal Receipt behavior.
- **Live:** original user ingress → real bound WebGPT Planner → Runtime decision → actual reply in original interface. Every actual request must be fresh and safe.
- **Safety-negative PASS:** intended REJECTED/BLOCKED/INTERRUPTED behavior observed with exact reason/evidence; not a Task SUCCESS. Never relabel a FAIL observation as PASS.
- **Mandatory:** gate blocking when FAIL/BLOCKED/NOT_RUN. **Conditional Live:** requires safe browser/transport fixture; if not safely possible, record BLOCKED, keep stage unaccepted or explicitly narrow acceptance scope with user's approval.

Each formal result must include: `case_id`, `stage`, `level`, `source_sha`, `worker_loaded_sha` (Live), `request_id/task_id/epoch` (applicable), `expected`, `actual`, `verdict`, `reason`, `evidence_files`, exact log hashes and attempt count. Individual attempts are immutable; never silently overwrite failure evidence on retry.

## 2. Phase 1 feature acceptance FP01–FP08

| ID | Input / exact intent class | Canonical expected behavior | Operational actions | Primary stage |
|---|---|---|---:|---|
| **FP01** | General timeless knowledge explanation, e.g. explain RAII | Answer-only admitted, text returned, accepted terminal receipt | 0 | 5A |
| **FP02** | Rewrite/translate only user-provided text | Answer-only admitted; source text not sent to unnecessary file/network tools | 0 | 5A |
| **FP03** | Explain a code snippet included in message (no project files) | Answer-only admitted; no fabricated project read | 0 | 5A |
| **FP04** | "Read this workspace file then summarize" | Fast path denied; legal tool-backed read or safe refusal; no pre-emptive SUCCESS | >=1 if authorized read executes; 0 if safely denied | 5A |
| **FP05** | Current exchange rate/weather/latest GitHub status | Fast path denied; tool/capability route or explicit uncertainty, never pretend live verification | dependent on authorized capability | 5A |
| **FP06** | Earlier text-only request, subsequent user request requiring a file | Next task retains valid conversation context but goes to tool path; request scope never inherited as permission | >=1 if authorized and executed | 5B |
| **FP07** | Invalid `turn_commit`, fake evidence, missing receipt, pending OPEN/UNKNOWN, model-forged "done" | Reject fast terminal or pause appropriately; NEVER SUCCESS or fake receipt | 0 for rejected candidate (possible legal recovery later) | 5A |
| **FP08** | Same accepted terminal result delivered via Telegram/WebCopilot, including transport retry/restart | Exactly one semantic completion and one confirmed user-facing delivery per route; no duplicated reply | 0 for text-only | 5B |

These eight cases are the **feature matrix**, not all tests. Each stage runs the relevant subset plus separate compatibility and security regressions.

### Common FP assessment: mandatory checks

For each applicable FP case capture:
- User-submitted body/hash, authorized workspace and attachment metadata, origin interface, source commit.
- Initial input to bound WebGPT, all model turns, parser/admission/terminal events, action IDs/count and whether side effects occurred.
- Expected/actual completion kind, task state/outcome, receipt (or objectively missing receipt), delivered/unknown status.
- Observed final text in the **originating** Telegram/WebCopilot transport, not merely model UI text.
- Negative case has specific denial reason and no invented action/evidence/receipt.
- FP01–03 one model turn target applies to **Task Planner loop**; one-time session Bootstrap is separately measured and not counted as an operational action.

## 3. Stage 2A — contract mapping verification

### 2A-UT (mandatory, isolated)

| ID | Test | Expected |
|---|---|---|
| 2A-U01 | Build/validate minimal `TaskResultV1` from trusted admitted completion | Typed, complete, deterministic result |
| 2A-U02 | Old Telegram/WebCopilot payload deserialize/upgrade compatibility | Legacy field names and content survive; no silent loss |
| 2A-U03 | Request/Task/Epoch/intent mismatch | Reject before persisted result |
| 2A-U04 | Outcomes SUCCESS/FAILED/PARTIAL/UNKNOWN and terminal acceptance combinations | Contradictory success or missing receipt not promoted |
| 2A-U05 | PAUSED/INTERRUPTED and OPEN writer state | Not representable as successful terminal |
| 2A-U06 | Delivery PENDING/QUEUED/FAILED/DELIVERED separation | Delivery cannot set task outcome SUCCESS |
| 2A-U07 | Repeated terminal/outbox retry with same idempotency key | Exactly-once semantic completion |
| 2A-U08 | Artifact / plan_id / original reply-route preservation | No loss, no reauthoring from model |
| 2A-U09 | Wrong/malformed schema and sensitive field injection | Fail closed; no sensitive values in logs |
| 2A-U10 | Feature flag off | Exact old behavior; no new result required to start legacy task |

### 2A-IT (mandatory)

| ID | Real connected components | Expected |
|---|---|---|
| 2A-I01 | Gateway → queue → mock admitted terminal → TaskResult → Event Store | IDs/outcomes/receipt intact |
| 2A-I02 | Telegram fake delivery adapter → outbox retry | One delivery receipt, no duplicates |
| 2A-I03 | WebCopilot fake adapter → outbox retry | Same common contract; interface formatting unchanged |
| 2A-I04 | Worker PAUSED/UNKNOWN → outbox/status | No false completed event |
| 2A-I05 | Crash/restart after queue.complete and before deliver | Durable resume delivers once |
| 2A-I06 | Existing plan-only completion/artifact handling | plan_id and artifact policy unaffected |
| 2A-I07 | Progress + Terminal Receipt identity correlation | request/task/epoch all match, no foreign receipt |

### 2A-Live (controlled, bounded)

- **2A-L01** Telegram or LOCAL_TEST original route: safe timeless text; legacy output preserved; accepted terminal and real outbound receipt.
- **2A-L02** Telegram or LOCAL_TEST original route: read-only authorized fixture; objective evidence/receipt preserved.
- **2A-L03** WebCopilot original route, only after actual ingress/capability preflight: text and read-only smoke; record if unavailable.
- **2A-L04** Post-completion replay check: inspect delivery log only; do not dispatch same request a second time.

2A exit: contract and adapter mapping accepted; original Planner/action execution path unchanged. The 2A Live cases are **compatibility smoke**, not a claim Fast Path is active.

## 4. Stage 5A — core admission verification

### 5A-UT

| ID | Case | Expected |
|---|---|---|
| 5A-U01 | FP01/02/03 with trusted plain text input | Eligible ANSWER_ONLY |
| 5A-U02 | FP04 mentions local file, URL resource or repo | Ineligible; no fake read |
| 5A-U03 | FP05 requests current facts, external account state, tool capability | Ineligible even with no path |
| 5A-U04 | Mixed "explain + edit/save/send/post" | Ineligible; tool path required |
| 5A-U05 | Pending OPEN writer / QUEUED / RUNNING / UNKNOWN / stale verification | Ineligible for fast terminal |
| 5A-U06 | Claimed "tool not needed" in model response vs authoritative request | Trust original request, reject unsupported fast candidate |
| 5A-U07 | Missing/invalid progress, condition_id, turn_commit or final_response | No terminal acceptance, no delivery |
| 5A-U08 | One valid terminal with no operational actions | `ANSWER_ONLY_RESPONSE_ADMITTED` distinct from evidence PASS |
| 5A-U09 | Legacy ordinary `_terminal_evidence_verdict([])` | Remains UNVERIFIED, not globally modified |
| 5A-U10 | Scope change or attachment supplied on later request | Re-adjudicate; no cached broad permission |
| 5A-U11 | Feature flag OFF + rollout disable | Exact existing route/fail closed |

### 5A-IT

| ID | Components | Expected |
|---|---|---|
| 5A-I01 | V9 parser → initial Progress → terminal admission → receipt (FP01/02/03) | One Planner turn, 0 operational actions, accepted answer-only receipt |
| 5A-I02 | Authoritative original request requires file (FP04) | Denied fast path; normal read or safe blocked |
| 5A-I03 | External/time-sensitive task (FP05) | Denied fast path, no invented current result |
| 5A-I04 | Forged SUCCESS evidence and malformed response (FP07) | Rejection/recovery, no success |
| 5A-I05 | OPEN/UNKNOWN Action Ledger fixtures (FP07) | Existing nonterminal blockers remain |
| 5A-I06 | Text-only task followed by new Tool-required task (FP06) | Next task uses Action Loop with new scope |
| 5A-I07 | No action result ledger item for legitimate text-only | No fake Action PASS/Condition PASS inserted |
| 5A-I08 | TaskResult 2A mapping from 5A receipt | Completion kind and user text correct |

### 5A-Live

- **5A-L01/02/03** one fresh request each for FP01/02/03 from isolated, authorized original ingress; observe one Planner turn, 0 operational tools, accepted receipt, real delivery if integrated.
- **5A-L04/05** safe negative requests representing FP04/05, using authorized benign read or explicit capability-unavailable outcome. Do not intentionally perform web lookup if current platform/permissions do not offer it.
- **5A-L07** invalid terminal in deterministic isolated Planner test; a live invalid response occurs only if naturally observed or expressly allowed by controlled test injection without tampering with productive conversation.
- FP06/FP08 full multi-adapter Live requirement deferred to 5B; 5A records NOT_APPLICABLE, not PASS.

5A exit: 0 false-positive fast-path classifications in mandatory negative suite; FP01–03 one-turn/zero-operational target; ordinary tool tasks unaffected.

## 5. Stage 5B — cross-interface delivery verification

### 5B-UT

| ID | Case | Expected |
|---|---|---|
| 5B-U01 | Telegram Result mapping | Preserves reply origin and task ID |
| 5B-U02 | WebCopilot Result mapping | Same canonical completion semantics |
| 5B-U03 | Missing LocalAgent adapter support | Explicit unsupported/deferred, not silent PASS |
| 5B-U04 | Accepted answer-only result and failed initial delivery | Outbox pending with retry; TaskResult outcome unchanged |
| 5B-U05 | Duplicate retry / queue event replay | Exactly once visible reply |
| 5B-U06 | Fallback to tool path | Same request/owner, no second parallel conversation |
| 5B-U07 | PAUSED/UNKNOWN/OPEN resource and adapter response | No successful delivery representation |
| 5B-U08 | Per-interface feature OFF | Legacy flow preserved |

### 5B-IT

| ID | Integration | Expected |
|---|---|---|
| 5B-I01 | FP01/02/03 through Telegram mock ingress → fake WebGPT → real outbox → fake delivery | Accepted text-only with 0 operational actions |
| 5B-I02 | FP01/02/03 through WebCopilot mock ingress | Equivalent TaskResult; origin delivery |
| 5B-I03 | FP04/05 across both ingress routes | No incorrect fast success |
| 5B-I04 | FP06 sequential requests in same session | Text-only followed by authorized Tool Action |
| 5B-I05 | FP07 pending Writer/verification | No terminal successful result |
| 5B-I06 | FP08 outbox retry / restart / same event | Exactly one receipt, no duplicate send |
| 5B-I07 | Interleaved distinct Task IDs under one conversation owner | Serialized browser ownership; no cross-task evidence |
| 5B-I08 | Backward compatibility to original Telegram/LOCAL_TEST Worker | No deviation for tool results/plan artifacts |

### 5B-Live

For **each currently supported original ingress**, run the full **FP01–FP08 applicability matrix**, recording separately:

- Telegram/RemoteAgent: actual authenticated Telegram transport or approved LOCAL_TEST as *distinct evidence categories*. LOCAL_TEST is not interchangeable with Telegram for user-facing delivery certification.
- WebCopilot: real supported ingress and original delivery UI only after verifying a functioning binding. Do not equate an Agent2 conversation controlling a worker with an independent WebCopilot ingress.
- LocalAgent: compatibility smoke where accessible; full 3-adapter convergence is deferred to Phase 2.

A minimal suggested ordering: FP01, FP02, FP03 → FP04, FP05 → FP06 → FP07 (controlled negative) → FP08 (delivery receipt audit). Preserve each attempt independently; if safety preflight fails, record BLOCKED before sending, not a fabricated success.

**Acceptance:** mandatory two-interface cases PASS with source/turn/action/terminal/receipt identities; zero duplicate delivery; no lost Progress/Outcome; full regression PASS.

## 6. Regression suite and freeze-point

Mandatory candidate-source regression categories at all affected milestones:

1. `validate_progress_terminal_lifecycle.py`
2. `validate_evidence_backed_completion.py`
3. `validate_standalone_loop.py`
4. `validate_plan_only_telegram_delivery.py`
5. `validate_narrative_v9_bridge.py`
6. `validate_action_loop_runtime_state.py`
7. `validate_evidence_terminal_boundaries.py`
8. `validate_it19_task_scoped_runtime_writer.py`, `validate_it19_open_write_isolation_offline.py`
9. `validate_it20_bound_workspace_planner_repair.py`, `validate_it20_command_workspace_scope.py`
10. `validate_protocol_transport_normalization.py`, `validate_single_fence_transport_contract.py`
11. Relevant `validate_project_access_indexed_query.py` / VPCS pinned snapshot when touching context logic.
12. Any official Manifest and product-specific validation bundle required by source-change scope.

This is a **candidate regression inventory**, not a claim all scripts can run in one environment. Before first implementation, confirm each script's CLI, OS requirements, fixture behavior and safety using source review. Prepare a concrete executable command list pinned to stage SHA; report SKIPPED/BLOCKED without classifying PASS. Never re-use a Live IT19 OPEN writer as a fixture.

## 7. Gate summary by stage

| Stage | UT | IT | Live | Exit condition |
|---|---|---|---|---|
| 2A | 2A-U01..U10 | 2A-I01..I07 | 2A-L01..L04 (as supported) | TaskResult compatibility; unchanged behavior |
| 5A | 5A-U01..U11 | 5A-I01..I08 | FP01–05 positives/negatives, controlled FP07 | Zero operational tools when eligible; no fabricated evidence |
| 5B | 5B-U01..U08 | 5B-I01..I08 | FP01..FP08 per required supported ingress | Correct original-interface delivery & no duplicates |
| Phase 1 final | combined UT | combined IT | final source SHA cross-interface smoke | independent signed acceptance + regressions |

A single `UNKNOWN`, missing mandated receipt, unverified loaded SHA, or untested mandatory ingress keeps the stage **NOT ACCEPTED**. Completing later cases after a failure is encouraged only where independent and safe. Do not use the live task's runtime outcome alone to mark a *negative* formal case.

## 8. Evidence template (proposed)

~~~json
{
  "case_id": "5A-I01",
  "stage": "5A",
  "level": "IT",
  "attempt": 1,
  "status": "PASS|FAIL|BLOCKED|NOT_RUN",
  "formal_expectation": "answer_only_admitted",
  "task_outcome": "SUCCESS|FAILED|PARTIAL|UNKNOWN|null",
  "product_sha": "<actual sha>",
  "loaded_source_attestation": "OFFLINE_FIXTURE|MATCH|NOT_ATTESTED",
  "request_id": "<actual or null>",
  "task_id": "<actual or null>",
  "task_epoch": "<actual or null>",
  "planner_turns": 1,
  "operational_action_count": 0,
  "protocol_rejections": 0,
  "terminal_receipt": {"status": "ACCEPTED", "id": "<actual or null>"},
  "delivery": {"status": "DELIVERED|PENDING|UNKNOWN|NOT_APPLICABLE", "receipt": "<actual or null>"},
  "raw_sha256": {"planner": "<actual>", "log": "<actual>"},
  "reason": "real observations only"
}
~~~

The schema above is for **test reporting**, not the persisted production `TaskResultV1` contract. Every logged `<actual>` placeholder MUST be filled with observed values or `NOT_OBSERVED`; never invent IDs, hashes or a terminal receipt.
