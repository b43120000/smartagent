# P1–P3: Evidence-Gated Revision and Exhaustion Contract

Base commit: ca433e0e06179d16b0dad86ba07000c8a03c2398.
Branch: feat/planner-runtime-flexible-recovery-p1-p3-20261104.

## Immutable boundaries

1. Original goal and its acceptance criteria are stable across revisions.
2. Request ID, Task ID, Epoch and original authorization/scope never change on a Planner's proposal.
3. All tools, paths, hosts, proxies and network methods still require their independent Runtime Tool/ACL admission.
4. A new Progress revision may change strategy, order and remaining tasks, but may not erase executed effects, unknown outcomes, evidence, or global GAR / Bounded Repair budgets.
5. Executed tool results are not inferred from prose. Evidence receipts are accepted only from the Runtime ledger.
6. An alternative connection method is eligible only when that exact method is registered and authorized. An unapproved proxy, user account, credential, browser, domain or filesystem parent cannot become authorized because earlier methods failed.

## P1 — Semantic Resolution Proposal

Allow the Planner to propose a source-grounded interpretation or request clarification.
Runtime must verify the original-request literal span, role (existing input, existing directory, new output),
and whether the proposed target stays within the immutable authorized scope.
Untrusted candidate existence and model-written path correction never themselves grant write authority.
Ambiguous quoted/unquoted spans, ADS, symlink/.. escape, missing write scope and multiple valid candidates
must remain blocked/clarified. Do not silently turn a missing output file into a missing prerequisite input.

## P2 — Planner Revision Lifecycle

Separate planning strategy from same-turn tool execution.
A Planner may supersede a *not executed* next-action declaration and produce a new Progress revision,
subject to Runtime's action/receipt correlation and pending-effect checks.

Accept revision n+1 only when it has a materially different authorized route or new
independently verified evidence, a changed prerequisite state, or an attested external state change.
A changed action_id, repeated prompt, reworded Progress or change in percentage is not fresh evidence.
Do not reset the cumulative retry/replan/transport budgets at a revision boundary.
If a command has UNKNOWN or pending effect, PAUSE instead of rerunning it.

The safety classifier in `source/agent_core/replan_safety.py` is a pure
Runtime-side building block, NOT authorization or a complete P2 implementation.

## P3 — Flexible Prerequisite Discovery and Exhaustion

Runtime supplies finite, scope-checked eligible methods and the missing facts.
Planner selects the next legal method; Runtime independently checks its scope and
evidence receipt. Once all eligible methods are verifiably failed or policy-blocked,
return an evidence-backed exhausted condition rather than requesting another replan.
A policy-blocked method must be identified as NOT EXECUTED, not a failed connection.
If a method remains unattempted, route availability is RECOVERABLE.
If any attempted method's outcome is UNKNOWN/UNVERIFIED/conflicting, PAUSE.
If a method succeeds in reaching the resource, CONTINUE: reachability is not goal completion.

Exhaustion is relative to the actual execution environment and the finite authorized
method catalog at this time. Never claim that a URL is globally/permanently unavailable.

## Required Planner-facing final report on confirmed exhaustion

Report the unchanged original task goal and that it could not be completed.
Include the scope and time context of attempted access, each attempted method,
its evidence-backed result and error category, and methods not attempted because
Runtime did not authorize them (if relevant). Cite Runtime Action IDs/evidence_refs.
State that all *currently authorized and available* alternatives were exhausted,
not that all conceivable techniques were tried. Do not claim terminal SUCCESS.
Use the normal V9/Progress terminal gate; if INTERRUPT/UNKNOWN cannot
be admitted safely, PAUSE with the runtime blocker and evidence.

## Mandatory negative integration matrix

- Single timeout + another authorized untried route: REPLAN; cannot claim exhaustion.
- All three independently verified authorized failures: EXHAUSTED, each evidence_ref preserved.
- All methods rejected before network I/O by scope: EXHAUSTED with AUTHORIZED_ROUTES_BLOCKED_NO_NETWORK_ATTEMPT, NOT site unreachable.
- A method with UNKNOWN execution or missing verification: PAUSE and do not repeat effect.
- Same route with new action_id/progress revision but no new evidence: PAUSE; no infinite loop.
- Retry budget exceeded: PAUSE; no reset via progress revision, fresh page or new task plan.
- Unauthorized parent/sibling path, proxy, new host/domain or browser capability: REJECT.
- New evidence that contradicts prior failure: REASSESS, not stale terminal claim.
- Authenticated user access required but no authorization: USER_INPUT/BLOCKED; never fabricate credentials.
- If original success criteria are not met, a reachable URL is not terminal SUCCESS.
- Pure text-only goal retains existing Tri-One answer-only boundaries.
- Success-claim verification, PR-B/C/D, GAR and existing SPR V1 regressions remain mandatory.

## Current integration status

The reusable route-exhaustion guard and its focused negative tests have been added
to the feature branch. Full P1 semantic proposal admission, P2 revision persistence,
P3 Tool/Progress integration and an end-to-end multi-revision Live test remain
separate acceptance requirements. Do not label P1–P3 complete until all are verified.
