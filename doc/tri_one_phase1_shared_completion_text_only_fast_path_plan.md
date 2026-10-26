# Tri-One v2 — Phase 1: Shared Completion Contract & Text-only Fast Path

> Status: **PLAN_ONLY / NOT IMPLEMENTED / NOT TESTED**  
> Owner: ChatGPT (architecture, RCA, remote source changes, acceptance); Codex (local checkout, isolated UT/IT/Live execution, original evidence collection).  
> Authored: 2026-10-26 (Asia/Taipei).  
> Source baseline at planning time: `main` commit `b95be77876ac522248954f3127d4fb7b1024eed2`.  
> Document scope: **2A → 5A → 5B only**. 2B / 2C form *Tri-One v2 Phase 2*, and are NOT authorized by this plan.

## 0. Executive decision

Deliver **2A Shared Completion Contract → 5A Answer-only Fast Path Core → 5B Cross-interface Integration** sequentially. After EACH milestone: code review + isolated Unit Tests (UT) → Integration Tests (IT) → controlled Live E2E → unchanged-runtime regression → explicit acceptance. A failed gate blocks promotion, but safe independent cases should still run to preserve per-case evidence, without bypassing safety gates.

**Definition of Fast Path:** *one planner decision, zero operational Runtime tool actions* for requests that require neither external information nor side effects. It **does not** skip request identity, browser/session ownership, smartagent_tool parser, progress/terminal admission, durable TaskResult, delivery state, user scope, or safety checks. Do not treat `no file path in prose` as sufficient for this route.

**Important code fact:** `WebAgentProtocolLoop._terminal_evidence_verdict()` currently returns `UNVERIFIED` with no action-scoped evidence. Therefore 5A requires an explicit, narrowly scoped **answer-only acceptance path**, distinct from task-world verification. Never coerce a nonexistent action to PASS and never weaken the ordinary tool-backed SUCCESS rule.

## 1. Relationship to the nine-item roadmap

- #1 Runtime evidence/current state: the latest Runtime IT cycle is formally closed, not blanket permission to weaken the new release's safety requirements.
- #2 Tri-One shared interface: **2A** is Phase 1; **2B + 2C** are Phase 2 and out of scope.
- #3 VPCS/GitHub Hybrid: not modified; preserve pinned snapshot and lazy retrieval.
- #4 Prerequisite Resolution: do not bypass when action is required.
- #5 Pure-natural-language fast path: **5A + 5B** are Phase 1.
- #6 Historical Runtime IT: reuse historical evidence where applicable; each changed source SHA requires its own appropriate regression/acceptance.
- #7 Action Compiler/Preflight, #8 staged input, #9 dual WebGPT URL: no implementation in Phase 1.

Historical negative-safety boundary: IT19 CASE = `CLOSED_FORMAL_PASS`, but its Runtime Task = `PAUSED/UNKNOWN`, with staged OPEN write / no Terminal Receipt. IT20 was separately closed formally. Do **not** replay, resume, abort, commit, cancel or clean any protected historical Runtime task to exercise new tests.

## 2. Existing source inventory and non-goals

### Verified integration seams (current `main`)

| Boundary | Existing code | Target |
|---|---|---|
| Ingress/identity | `source/agent_core/agent_gateway.py`; `transport_message.py`, `transport_sessions.py` | Preserve request/task/session identity, durable enqueue |
| Planner + canonical V9 turn | `source/WebAgent/protocol_loop.py`, `protocol.py` | Preserve parsing, progress, ACK/turn_commit, action admission |
| Evidence and lifecycle | `task_progress.py`, `action_loop_state.py`, `tool_result_contract.py`, `remote_events.py` | No weaker terminal rules; build explicit answer-only lane |
| Telegram worker/route | `source/RemoteAgent/telegram_webagent_worker.py`, `telegram_delivery.py`, `telegram_artifacts.py` | Map accepted completion to common TaskResult; send via original route |
| Direct WebAgent | `source/WebAgent/controller.py`, `browser_client.py`, `session.py` | Reuse same completion contract |
| WebCopilot/LocalAgent | Existing adapters and tool loops (requires connector-specific call-path confirmation in 5B preflight) | No speculative routing or refactor |
| Regression basis | `source/WebAgent/tests/validate_progress_terminal_lifecycle.py`, `validate_evidence_backed_completion.py`, `validate_standalone_loop.py`, `validate_plan_only_telegram_delivery.py` | Preserve ordinary SUCCESS and failure gates |

Existing `_terminal_evidence_verdict` requires real action evidence; `_terminal_recovery_requirement` currently routes unverified SUCCESS to verifying tools. 5A must prevent spurious VERIFY for *legitimate* text-only answers **without suppressing VERIFY for tool tasks**. This is the primary engineering risk.

Do not replace the current Runtime state machine in 2A/5A/5B. No general protocol rewrite, new browser instance, permission widening, automatic Git operations, inferred live data, additional ChatGPT URL, or ad-hoc "SUCCESS" bypass.

## 3. Target architecture

~~~text
LocalAgent / WebCopilot / Telegram/RemoteAgent ingress
                 |
  authenticated normalized request + route + scope
                 |
        existing Planner / V9 Protocol
                 |
       request capability adjudication
           /                    \
  ANSWER_ONLY_CANDIDATE     TOOL_REQUIRED or AMBIGUOUS
           |                       |
  bounded terminal contract   existing Action Loop
           |                       |
        trusted TaskResult v1 (same schema)
                 |
        existing event/outbox/delivery adapter
                 |
           final delivery receipt
~~~

The Planner can *propose* an answer-only result; the Runtime must decide if it is admissible using the **original authoritative user request, active scope, attachment status, accepted task/progress state and pending action/verification ledger**. A model assertion such as "this needs no tools" is not authorization and is not evidence.

### 3.1 Proposed common `TaskResultV1` (design schema; not an existing API)

Normalized **internal persisted** record, produced only after a trusted terminal transition; the user-facing text stays presentation-only. Proposed fields:

~~~json
{
  "schema": "TRI_ONE_TASK_RESULT_V1",
  "request_id": "RR-...",
  "task_id": "TASK-...",
  "task_epoch": "...",
  "intent_digest": "...",
  "source_interface": "TELEGRAM|WEB_COPILOT|LOCAL_AGENT|LOCAL_TEST",
  "reply_route_id": "transport-owned opaque route ID",
  "source_commit": "verified product SHA",
  "completion_kind": "ANSWER_ONLY|EVIDENCE_BACKED",
  "task_state": "COMPLETED|FAILED|INTERRUPTED|PAUSED",
  "outcome": "SUCCESS|FAILED|PARTIAL|UNKNOWN",
  "terminal_acceptance": {"status": "ACCEPTED|REJECTED|MISSING", "receipt_id": null},
  "operational_action_count": 0,
  "evidence_refs": [],
  "answer_text": "final accepted text",
  "artifacts": [],
  "delivery": {
    "status": "PENDING|DELIVERED|FAILED|UNKNOWN",
    "attempt_count": 0,
    "delivery_receipt_id": null
  },
  "result_id": "stable idempotency key",
  "created_at": "UTC timestamp"
}
~~~

Precise names/enums may be finalized **before** 2A coding after inspecting existing persisted queue/outbox records; record any compatibility mappings in a versioned contract and tests. Do not introduce silent changes to existing on-disk schemas. `reply_route_id` must be opaque to the model and produced by trusted transport. `answer_text` is never executable authority. `source_commit` must reflect *loaded* code attestation for Live, not merely repository HEAD.

**Separate axes**:
1. Task state: durable lifecycle (COMPLETED/PAUSED/etc.).
2. Outcome: correctness relative to a declared task contract, not transport success.
3. Terminal acceptance: evidence that V9 admitted the final turn.
4. Delivery: external interface delivery evidence (QUEUED is not DELIVERED).
5. Completion kind: ANSWER_ONLY vs EVIDENCE_BACKED. Only the latter asserts observation/side-effect evidence.

Required invariants: task identity immutable; no duplicate completion for same task/epoch; no duplicate delivery after receipt; no terminal success if pending/OPEN writer, unresolved queued/running/unknown operation, required verification, invalid/stale receipt, or mismatched scope; prior action evidence cannot be hidden by "answer-only".

## 4. Milestone 2A — Shared Completion Contract (no behavior change)

**Purpose:** introduce one normalized completion/result contract, compatible adapters and migration tests, without changing planner turns or authorization.

**Implementation sequence**:
1. Inventory existing `queue.complete`, `RemoteEventStore.emit`, `render_task_reply`, Telegram/WebCopilot/Direct controller success and failure emissions, result/receipt structures. Produce a field mapping table. Verify LocalAgent call path rather than assuming parity.
2. Define `TaskResultV1` schema + deterministic construction/validation/serialization. Only trusted lifecycle code may construct terminal acceptance or mark delivery status.
3. Add an adapter/factory from *existing admitted terminal* to common result. Keep existing external payload shapes; dual-write internally only if needed, using versioned backward-compatible fields and transactional/idempotent save.
4. Make delivery acknowledgement independent of task execution outcome. Existing retry rules must not duplicate user-facing response.
5. Feature flag OFF by default until UT + IT pass; ability to fall back to legacy result mapper without breaking historical tasks.
6. No changes to `WebAgentProtocolLoop._terminal_evidence_verdict` in this milestone.

**2A UT**: schema validation/round-trip; allowed enums; missing or contradictory receipt; state/outcome cross-product; request/task/epoch mismatch; stable result ID; duplicate terminal event and delivery replay; PII/secret field non-leakage; legacy payload compatibility.

**2A IT**: mocked accepted terminal, mocked FAILED/UNKNOWN/PAUSED; queue→Result→Events→Outbox→Delivery; Telegram and WebCopilot delivery adapters via fake clients; source/artifact and plan_id preservation; interruption before and after outbox commit; idempotent restart/recovery.

**2A Live**: bounded (1) existing text reply, (2) existing harmless read-only Tool task **per currently available adapter**; verify no visible behavior change, distinct delivery receipt; do not silently claim unsupported LocalAgent Live as PASS. Baseline vs new planner-turn count should match or variance be justified.

**2A acceptance**: all mandatory UT and IT PASS, approved Live scopes PASS, identical existing workflow decisions, no accidental duplicate delivery; document final schema fields and migration.

## 5. Milestone 5A — Answer-only Fast Path Core

**Purpose:** one legitimate first planner turn can produce admitted `report_progress + final_response + turn_commit`, with **zero operational actions** and no invented verification, for requests needing only a textual response.

### 5.1 Admission contract

A candidate can be accepted only if **all** are true:
- Original trusted request's goal is satisfied by composing/explaining/transforming text already available in the message or accepted conversation context.
- No attachment/file/repository read; filesystem/process/network/browser mutation; current remote state; latest/time-sensitive fact; tool invocation; artifact creation; external side effect; or pending user action is necessary.
- No prior operational action in current request, and no inherited unresolved action/verification/result blocker, no active OPEN writer, no unknown delivery or continuation side effect.
- Exactly one valid canonical V9 turn with complete first `report_progress` contract, legal condition ID, terminal `final_response`, and correct `turn_commit(action_count)`.
- No contradiction between original goal, completion contract, tool declarations, and response content. No unsupported claim to have checked/changed external state.
- Existing request scope/attachment ownership and policy checks pass.

Ambiguous request → **TOOL_REQUIRED or a normal admissible request clarification path**, *not* automatic optimistic SUCCESS. Classification should be conservative and inspect structured prerequisites and active metadata, not solely regex matching or a self-declared model flag.

### 5.2 Separate terminal semantics

Introduce an explicit answer-only acceptance reason, conceptually `ANSWER_ONLY_RESPONSE_ADMITTED` and `completion_kind=ANSWER_ONLY`. It certifies: an in-scope text answer was admitted with no tools and no outstanding Runtime duties. It **does not** certify the accuracy of external facts, observation of remote state, file persistence, transport delivery, or a `verification_status=PASS` from nonexistent actions.

Do **not** change `_terminal_evidence_verdict` globally to return PASS for empty refs. Add a dedicated narrow branch in terminal admission *after* parsing, progress/action-state safety gates, and before generic missing-tool-verification recovery. It must not mutate `condition_result_ledger` or create fake `ActionResult` records.

### 5.3 Behavioral constraints

- No new browser page / independent WebGPT call; the existing request's bound Planner conversation is used.
- First-turn fast path should avoid secondary model turns; failure to classify safely falls back to unchanged Action Loop, without replaying prior actions.
- Preserve `Progress` initial/final semantics; user-visible progress messages can be suppressed where no useful intermediate state exists, but durable lifecycle/receipts are retained.
- No hard-coded text answers in the Runtime classifier. Runtime is *admission*, Planner is *semantic answer*.
- Session continuation: a later request that requires tools must go through normal authorization and Runtime actions.
- Feature flag (`TRI_ONE_TEXT_ONLY_V1` as **proposed** name), default OFF; implement staged rollout, explicit OFF behavior, negative telemetry and rollback.

**5A UT**: FP01–FP07 fixtures including textual transformation, explanation, source-required, current-data-required, conversation continuation, disguised side effects, forged model claims, invalid turn/receipt, ledger OPEN/UNKNOWN; adversarial mixed intents.

**5A IT**: real parser+state machine+Progress Ledger+Terminal Acceptance through fake Planner; verified no Tool dispatch/no `action_result_ledger` entries for FP01–03; FP04–05 must not fast-complete; FP07 must reject fabricated/unverified tool success; subsequent tool request still works.

**5A Live**: isolated existing authorized test conversation, run FP01, FP02, FP03; negative FP04/FP05/FP07 use safe fixtures/read-only request or controlled fake provider cases only as applicable. Capture exact web response + protocol events; no unsafe live mutation needed. A negative case's intended rejection can be a formal PASS.

**5A acceptance**: genuine answer-only cases admitted in one Planner turn with 0 operational actions; no false-positive fast path in negatives; terminal receipt present and outcome states correct; existing Runtime regression PASS.

## 6. Milestone 5B — Cross-interface Integration

**Purpose:** use the same 2A TaskResult and 5A admission contract via Telegram/RemoteAgent, WebCopilot, and eligible LocalAgent path without replacing their transports.

**Implementation sequence**:
1. Confirm actual current adapter call paths, browser ownership, transport scopes and capability profile. A missing adapter path is `NOT_IMPLEMENTED` not a passed test.
2. Route admitted `TaskResultV1` through existing Telegram Events/Outbox and WebCopilot/Direct delivery; each adapter controls its own format and delivery receipt. Preserve `plan_id`, artifacts, replies and origin metadata.
3. Add `completion_kind` and delivery status to persisted audit, but not model-supplied action blocks.
4. A rejected/ambiguous fast-path decision must continue through the original Planner/Tool loop *on same request and current browser owner*; prevent duplicate send or second live request.
5. Implement per-transport feature switches and a fail-closed rollback.
6. LocalAgent support remains conditional on actual shared-engine entry point: do not label 5B complete for all three product adapters if that requires 2B/2C. Explicitly define **Phase 1 required** adapters as Telegram/RemoteAgent and WebCopilot; capture LocalAgent compatibility evidence if available, else defer full convergence to Phase 2.

**5B UT**: mappings and delivery idempotency for each supported adapter; refusal/timeout/retry; preserved task metadata; no delivery after PAUSED; no duplicate owner or new CDP instance.

**5B IT**: FP01–FP08 via fake ingress + mocked WebGPT; identical TaskResult semantics across supported transports; one intentional restart between accepted terminal and delivery; distinct task/outbox completion evidence.

**5B Live**: real original Telegram and WebCopilot intake, each required applicable FP01–FP08 case. For FP04/FP05, observe legal fallback (read-only tool or safe explicit interruption) rather than demand external connectivity. For FP06, ensure normal tool path on a subsequent user request. For FP07, induce malformed/unverified candidate in isolated controlled test—not deliberate corruption of a production conversation. FP08 check receipt exactly once. Use only explicitly bound browser instance / selected test conversation; never create `edge5` or assume a spare profile.

**5B acceptance**: all mandatory FP cases formally adjudicated, required adapters PASS, Live original transport replies correct, zero duplicate deliveries, preserved fail-closed safety, measured latency/turn reduction for text-only cases. Any missing interface is `NOT_RUN`, not `PASS`.

## 7. Cross-stage verification and strict promotion gates

Every 2A / 5A / 5B stage has an immutable candidate product SHA and a stage-specific evidence package:
1. **Preflight**: exact source SHA, official Manifest PASS, clean tracked tree, correct repo/worktree, isolated fixture, current queue/Writer/Outbox state, authorized browser binding, no ambiguous composer, no unapproved outstanding task. Do not restart active services merely to pass the preflight.
2. **UT**: pure and fixture-based, no browser or user data; report case ID, expected/actual, exit code, raw log hash.
3. **IT**: isolated native protocol/test environment; actual parser, event/ledger/queue/store/adapter components, deterministic fake Planner; negative safety assertions; request/task/epoch correlation. Test independent cases even when an earlier case FAILs *where safe*, but stop any case that would cross a failed prerequisite.
4. **Live**: explicit test conversation and interface; one fresh request per case, never replay historical tasks. Capture exact user input (hash), model raw turns (byte hash), action counts, accepted/rejected receipts, worker loaded SHA, user-facing delivery status.
5. **Regression**: full applicable existing tests, especially protected Runtime evidence, action admission, terminal/paused boundaries, Writer isolation, delivery and Protocol V9. Do not treat historical PASS on other SHA as current-source regression.
6. **Acceptance**: stage owner classifies each test `PASS/FAIL/BLOCKED/NOT_RUN` with immutable evidence. All mandatory gates PASS before proceeding; no "PASS by not attempting" and no promotion on UNKNOWN.

Suggested folder layout on *private test evidence repository* `b43120000/test`, in a NEW task namespace (not old IT19 paths):
~~~text
tasks/TRI-ONE-P1-20261026/<stage>/
  task-spec.md
  source-pin.json
  ut/{results.json, logs/, hashes.json}
  it/{results.json, request-ledger.json, logs/, hashes.json}
  live/{results.json, request-ledger.json, logs/, receipts/, hashes.json}
  regression/{results.json, logs/, hashes.json}
  acceptance.md
~~~
Only actual evidence created by Codex should be committed to the evidence repo; do not fabricate files to match this skeleton. Avoid uploading tokens, passwords, chat cookies, or private user content; store unredacted sensitive evidence only in approved restricted local locations and publish hashes/redacted metadata.

### 7.1 Stage acceptance protocol

Use identifiers `TRI-P1-2A`, `TRI-P1-5A`, `TRI-P1-5B`. Each stage requires:
- source branch/commit, base main SHA, source diff summary;
- UT, IT, Live, regression **separate** results and raw-evidence references;
- action count and planner round count by request; final/receipt evidence;
- any FAIL/BLOCKED with RCA and next corrective task, no success by omission;
- explicit acceptance note before next stage, and only then release/merge to `main` after reviewing PR.
- preserve previous closed-case evidence: no changing historical formal verdicts as side effect.

## 8. Quantitative targets (hypotheses, not measured results)

| KPI | Target | Measurement |
|---|---|---|
| Text-only Planner turns | 1 accepted turn for FP01–03 | `planner_response_received` + correlated accepted turn |
| Text-only operational actions | 0 | accepted action ledger; never count `report_progress`/ `turn_commit` as operational |
| Fast-path false positives | 0 / mandatory negative fixtures | FP04/05/07/ambiguous request classifications |
| Missing/false Terminal Receipt | 0 | accepted receipt audit |
| Duplicate delivery per task/outbox | 0 | delivery receipt and idempotency key |
| Existing critical safety regressions | 100% mandatory PASS | stage-pinned runtime regression list |
| Protocol rejection rate | report baseline and after; aspirational <5% | rejected turns / model turns |
| End-to-end latency | compare medians and P95, do not claim improvement before measurement | request accepted → original interface receipt |
| Context/transport bytes | record before/after, no unsupported bandwidth promises | measured prompt/response bytes per Planner round |

Record elapsed time segments separately: ingress enqueue, wait-for-worker, browser wait/model, protocol repair, operational tool execution, outbox/delivery. Report environment variance and insufficient samples.

## 9. Risk register and rollback

| Risk | Hard control |
|---|---|
| Answer-only bypasses verification | dedicated admission lane, default deny, negative matrix, ordinary `_terminal_evidence_verdict` unchanged |
| Planner labels external work as text | compare original task/capability/attachments and current blockers; deny candidate |
| Mixed text + side effects | no fast path; keep full action lifecycle |
| UI double reply / duplicate delivery | request/task/epoch, stable result_id, durable outbox and receipt |
| OPEN/UNKNOWN action hidden by later response | authoritative action state / active blocker gate; no terminal SUCCESS |
| Adapter mismatch | per-transport mapping tests and opt-in flags |
| Browser/profile contention | one owner/lease on the configured test instance, no concurrent composer clients |
| Historical task replay | immutable historical task set; no dispatch to past request/epoch |
| Source version confusion | loaded worker SHA + Manifest + recorded commit before live |
| Broad refactor risk | 2B/2C explicitly deferred; use small changes and flag-off rollback |

Rollback each milestone by disabling the **new feature flag on the exact deployed version** or reverting the milestone's isolated source commits after conflict/safety review. Do not `reset --hard` user worktrees or overwrite active runtime state. An incomplete/rejected Live must retain original evidence and enter PAUSED/BLOCKED where appropriate.

## 10. Explicit deferrals / definition of Phase 1 DONE

**In scope**: 2A complete shared terminal result contract; 5A bounded answer-only admission; 5B Telegram and WebCopilot compatible delivery; observable positive and negative FP matrix; UT/IT/Live and no safety regressions.

**Out of scope**: 2B extracting full Transport-neutral ProtocolEngine; 2C converging all three full adapters into one engine; unverified LocalAgent full parity; VPCS Hybrid; dual browser/planner; staged prompt transfer; action compiler overhaul.

Phase 1 DONE iff: 2A, 5A, 5B accepted individually; a frozen final candidate SHA passes their combined UT/IT/Live matrix and mandatory Runtime Regression; original routes provide real receipts; user signs off final promotion. `PLAN_ONLY`, `BLOCKED`, or `NOT_RUN` never qualify as DONE.

See companion [verification matrix](tri_one_phase1_verification_matrix.md) and [Codex/ChatGPT execution handoff](tri_one_phase1_execution_handoff.md). Do not commence code modification from this document alone without explicit user execution instruction.
