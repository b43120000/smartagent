# SmartAgent Prerequisite Resolution v3 — Verified Dependency Resolution & Recovery

> **Status: PLAN_ONLY / NOT IMPLEMENTED / NOT TESTED.**  
> Authoring date: 2026-10-26 (Asia/Taipei).  
> Planning source baseline: `b43120000/smartagent main@b95be77876ac522248954f3127d4fb7b1024eed2`.  
> **Implementation prerequisite:** Tri-One v2 Phase 1 stages **2A, 5A, 5B must first be formally ACCEPTED on a verified product SHA**. PR #10 is currently only a Draft planning PR; no claim that its Source features are already present. Re-baseline and re-read the *actual* accepted interfaces before starting v3 development.

Related:
- [Tri-One Phase 1 plan (planning branch)](https://github.com/b43120000/smartagent/blob/docs/tri-one-phase1-plan-20261026/doc/tri_one_phase1_shared_completion_text_only_fast_path_plan.md)
- [Current v2 advisory Source](../source/agent_core/prerequisite_resolution.py)
- [IT16 prerequisite/repair implementation note](runtime_it16_prerequisite_bounded_repair_design.md)
- [v3 stage UT/IT/Live specification](prerequisite_resolution_v3_verification_matrix.md)
- [v3 ChatGPT/Codex execution handoff](prerequisite_resolution_v3_execution_handoff.md)

## 1. Goal and intended user experience

Given an original natural-language task that requires actual operations, SmartAgent must:
1. Determine which **necessary prerequisites** must hold before each original action.
2. Distinguish actual **SATISFIED / MISSING / UNKNOWN / DENIED / STALE** facts using Runtime evidence, never pure model assertion.
3. Construct a bounded, versioned requirement **DAG**, choose an authorized remedial plan, and perform only operations necessary to satisfy the original goal.
4. Verify the *post-remedy condition*, not just command exit status.
5. Resume the **original uncompleted action** with its preserved original task/request/epoch/scope, without replaying already completed or uncertain side effects.
6. Report accurate Progress and TaskResult via the original Telegram/WebCopilot route established by Tri-One Phase 1.

**Not the goal:** install everything, make the environment “healthy” globally, create unrelated directories/repositories, bypass permissions, transform pure answer-only requests into system preflight, or interpret an executed remedy as successful task completion.

### Example: authorized build-and-test task
- Original goal: run Python tests in an exact authorized workspace.
- Requirement graph: interpreter available; exact working directory exists/in scope; test source exists; package import works; command and verification contract valid.
- Probe shows Python and source satisfied, package import MISSING.
- If scoped installation was explicitly authorized, select a vetted install into an isolated environment, persist a transaction and perform it.
- Probe import **again**; record package/version/location/evidence. Only then unblock the original test Action.
- Run original test once, verify requested observable result, and return independently attested task outcome and interface delivery receipt.

Counterexample: “Check whether this folder is a Git repository” + `.git` missing ⇒ return factual negative result; never `git init`. `MISSING` is not an automatic instruction to change the host.

## 2. Baseline inventory and integration assumptions

Currently checked at planning time:
- `source/agent_core/prerequisite_resolution.py` — `SMARTAGENT_PREREQUISITE_RESOLUTION_V2`, **advisory only**: type hints and safety checks; `executed=False`, `authorization_granted=False`, `verification_complete=False`.
- `source/agent_core/runtime_environment.py` — real probe of bundled Python and Git, Environment Ledger V1; availability ≠ installation authority or task success.
- `source/agent_core/action_loop_state.py` — `PREREQUISITE_REQUIRED`, `VERIFY_REQUIRED`, `REPLAN_REQUIRED`, `WAIT_RUNTIME`; Runtime-owned blockers.
- `source/WebAgent/protocol_loop.py` — V9 Progress, Evidence Ledger, ACK/turn_commit, bounded repair, Action Admission, Terminal Receipt; must remain the canonical protocol owner.
- `source/agent_core/task_progress.py` and `source/agent_core/task_plan.py` — existing Progress/plan contracts; avoid duplicated Task plan semantics.
- `source/agent_core/project_access.py`, `project_sync.py`, `project_context_ref.py` — VPCS `INDEX_ONLY`, lazy reads, pinned Snapshot and stale detection.
- `source/agent_core/tool_capabilities.py`, `smartagent_protocol.py` and `tools.py` — capability/Tool Schema and execution ownership.
- Tri-One Phase 1 (future accepted code) supplies shared `TaskResult`, answer-only fast path, and Telegram/WebCopilot completion adapters. Exact final schema is **not known until Phase 1 acceptance**.

**v3 must extend, not replace, the above boundaries.** Existing advisory v2 stays usable for legacy callers until an explicit compatible migration is verified.

## 3. System architecture and responsibility

```text
User ingress (Telegram/WebCopilot; LocalAgent when supported)
    |
Normalized Task / Request Scope / Answer-only routing (Tri-One Phase 1)
    |-- ANSWER_ONLY --> existing Fast Path (no unnecessary environment probes)
    +-- ACTION_REQUIRED
             |
        Planner: goal and candidate original actions
             |
        Prerequisite Resolver: typed bounded DAG, evidence requirements
             |
        Runtime Probe Registry: observe actual scoped facts
             |
        Durable Prerequisite Ledger: version + task/epoch + facts + blockers
             |
         SATISFIED? ------ yes ---> normal Action Admission / Execute
             | no
        Authorized Remediation Plan / Preflight
             |
          execute once / reconcile uncertain result
             |
        independent verification probe
             |
          SATISFIED? -- yes --> revalidate original Action + resume
             | no
       REPLAN / APPROVAL_WAIT / PAUSED / BLOCKED
             |
    Shared Progress / Terminal Acceptance / TaskResult / original delivery
```

**Trust partition:**
- Planner **proposes** goals, dependencies, candidate remedy choice, and explanations.
- Resolver validates typed plan dependencies, evidence freshness and staged transitions; it is **not an alternate Tool executor**.
- Runtime produces probes, scope/capability policy decisions, action receipts, postconditions and durable facts.
- Human authorization comes only from the existing trusted approval/ingress route. Text emitted by an Agent is never an approval.
- Action Ledger remains authoritative about whether each tool ran; Prerequisite Ledger is a view/reference of those facts, **not a replacement**.
- Progress Ledger and TaskResult do not grant permissions or assert side effects.
- VPCS provides snapshot/source context; stale refs never silently rebind and must not authorize writes.

## 4. Typed Requirement Ledger v3

Proposed schema only (names may be finalized after accepted Tri-One source interfaces are inspected):

```json
{
  "schema": "SMARTAGENT_PREREQUISITE_LEDGER_V3",
  "request_id": "RR-...",
  "task_id": "TASK-...",
  "task_epoch": "<immutable>",
  "intent_digest": "<original request digest>",
  "scope_digest": "<trusted authorized paths/capabilities digest>",
  "graph_revision": 1,
  "base_plan_revision": "<accepted original plan revision>",
  "project_snapshot_id": "<pinned or none>",
  "state": "ACTIVE",
  "requirements": [
    {
      "requirement_id": "REQ-001",
      "kind": "ENVIRONMENT|DEPENDENCY|FILESYSTEM|PROJECT_CONTEXT|EXECUTION|EXTERNAL_SERVICE|AUTHORIZATION",
      "target_action_id": "ORIGINAL-ACTION-ID",
      "predicate": {"type": "PYTHON_IMPORT", "module": "example_dependency"},
      "depends_on": [],
      "status": "UNKNOWN",
      "probe_recipe_id": "PYTHON_IMPORT_V1",
      "probe_evidence_refs": [],
      "evidence_fingerprint": null,
      "remedy_recipe_id": null,
      "remedy_action_id": null,
      "approval_ref": null,
      "attempts": 0,
      "last_checked_at": null
    }
  ],
  "blocked_original_action_ids": ["ORIGINAL-ACTION-ID"],
  "recovery_generation": 0
}
```

No code may accept `tool` name, filesystem path, shell string, `approval_ref`, or `evidence_refs` as trusted solely because the model placed it in this schema. Values are resolved against original Request + trusted bound workspace + installed, schema-validated canonical tools and Runtime-issued evidence/approval receipts.

### 4.1 Graph rules
- Deterministic, stable requirement IDs within one task graph, and immutable graph revision/history on update.
- Each node is **necessary** for one or more *not-yet-executed* original actions. A disconnected speculative requirement is rejected.
- All edges explicitly point to known nodes; duplicate IDs, cycles, path traversal, cross-task references, graph expansion without allowed scope or excessive fan-out fail closed.
- Edges form a finite DAG and bounded-depth remedy closure. No recursive “dependency of the install of a dependency” with no bounded budget.
- The original goal, actions, scope and evidence IDs cannot be rewritten by “repair text”.
- An original Action is unblocked only when **all relevant nodes** hold fresh Runtime-verified SATISFIED evidence and its normal Action Admission still passes.

### 4.2 Requirement states vs task lifecycle

| Requirement state | Meaning | Is it allowed to unblock an Action? |
|---|---|---|
| `UNKNOWN` | No adequate objective fact / probe error / timeout | No |
| `MISSING` | Valid scoped probe proved absence or failed predicate | No |
| `SATISFIED` | Fresh, predicate-specific Runtime proof | **Yes**, if all others satisfied and ordinary admission passes |
| `REPAIRING` | Authorized, identified remedy pending/running | No |
| `INSTALLED_BUT_UNVERIFIED` | Remedy may have executed; postcondition not yet proved | No |
| `DENIED` | Explicit scope/capability/policy rejection | No; no privilege escalation |
| `STALE` | Earlier proof invalid after relevant state/snapshot change | No |
| `BLOCKED` | No legal strategy / external human prerequisite / exhausted bound | No; preserve cause |

These are **node** states. They are not task outcomes. A task can be `PAUSED` while requirements are `MISSING` or `UNKNOWN`; `UNKNOWN` cannot be silently converted to `MISSING`.

### 4.3 Evidence and freshness
Each probe records: producer (trusted Runtime), `request_id/task_id/epoch`, `action_id`, canonical target/working directory, `probe_recipe_id`, observed predicate, `execution_status`, `verification_status`, trace/raw-evidence SHA, time, scope digest and relevant snapshot/file/binary identity. Model prose and an old Environment Ledger are **hints** only. For filesystem nonexistence, distinguish authoritative negative from inaccessible parent/errors.

No universal “fresh for N seconds” is safe. Bind freshness to resource identity plus operation-specific invalidation rules: project Snapshot/file hash, binary identity/version, interpreter environment prefix, dependency lock, working directory, authorization receipt. Immediately revalidate required facts at the original Action's **admission/use boundary** if mutation/TOCTOU could invalidate them. After a permitted setup changes environment, invalidate affected facts before re-probing.

## 5. Just-in-time resolution and policy levels

**Never eagerly probe all host capabilities on every user message.** Fast Path (Phase 1 `ANSWER_ONLY`) skips the Resolver entirely. For mixed tasks, resolve only prerequisites of the next actionable step, with a bounded read-only batch for independent probes.

| Level | Examples | Default policy |
|---|---|---|
| L0 scoped read-only | stat/exists/version/import/working-directory/VPCS metadata | Auto where original task grants the applicable read and tool policy allows it |
| L1 deterministic low-risk setup | task-scoped `INDEX_ONLY`, explicitly authorized output subdirectory in isolated workspace | Only within original authorized scope and existing tool safety policy; not all L1 automatically allowed |
| L2 environment mutation | create virtualenv, install package, `git init`, alter project config | Explicit original instruction or a **separate trusted approval** for exact target/version/command/scope; never infer |
| L3 high impact | admin/system-wide install, global PATH changes, secrets, destructive changes, policy bypass | Deny unless a dedicated pre-existing high-risk approval/security flow explicitly supports it; v3 does not introduce one |

Remedy approval is checked both at planning and immediately prior to each write. No new permission from an error message, model narrative, pathname embedded in a tool result, or permission to read a parent folder. Network dependency acquisition is OFF by default until an explicit integrity/supply-chain policy is designed and approved.

## 6. Staged implementation plan (PR-A → PR-B → PR-C → PR-D)

Every milestone: design/Source commit pin → **UT → isolated IT → safely authorized Live → Runtime regressions → formal ACCEPTANCE**. A failed prerequisite or missing Live evidence blocks *promotion*, but independent safe test cases may continue with distinct evidence.

### PR-A — Typed graph + durable Prerequisite Ledger

**Goal:** have a persistable request-scoped graph with typed predicates and blockers; **no automatic remedy dispatch**.

Implementation:
1. Inventory accepted Tri-One TaskResult/Progress/V9 admission APIs, source and state schema; freeze field-mapping contract.
2. Add a versioned Requirement model, canonical IDs, typed predicate/probe/remedy descriptors (non-executable), DAG validator and finite graph limits.
3. Create atomic, append-only audit-friendly Prerequisite Ledger keyed to request/task/epoch; store revisions, provenance, negative/error reason, migration policy and idempotency keys. Do not copy private tokens or arbitrary raw stdout into prompt.
4. Link requirement blockers to existing Action Loop's `PREREQUISITE_REQUIRED` and Progress by references, with no new protocol parser or ledger as second source of tool truth.
5. Provide compact Planner view of *changed* requirements, blocked action and allowed next step; no redundant full-state replay.
6. Feature flag OFF by default; no behavior change when disabled.

Candidate source areas: new `source/agent_core/prerequisite_graph.py`, `prerequisite_ledger.py`, minimal adapter in `prerequisite_resolution.py` and `action_loop_state.py`, optional thin Progress adapter; exact paths TBD by preflight.

**Scope boundary:** PR-A cannot install, create, mutate, run new remedies, claim prerequisites fulfilled without valid evidence, or alter existing Tool/Terminal admission.

**Exit:** valid graphs persist/restore correctly across restart, invalid/cyclic/cross-scope graphs fail closed, and blocked original Action is not dispatched.

### PR-B — Runtime probes + Missing Condition Detection

**Goal:** populate graph nodes with *typed, fresh, objective* Runtime facts.

Implementation:
1. Extend existing `runtime_environment` and existing canonical `observe_fact`, `inspect_project_scope`, `query_project`, `run_command` INSPECT/VERIFY with an allowlisted **Probe Registry**, not unbounded model-generated shell commands.
2. Initial adapters: exact-path exists/readability and parent-status; canonical working directory; bundled Python/Git version/executable identity; Python import under selected interpreter/env; VPCS Snapshot/index availability; read-only Git-repository detection. Distinguish probe availability from authorization.
3. Normalize observations into `SATISFIED/MISSING/UNKNOWN/DENIED/STALE` with evidence refs + source identity + scoped reason. Probe errors/timeouts/inaccessible parents must not become negative facts.
4. Just-in-time scheduling: only nodes needed by next admitted action, independent read-only probes may be batched within existing per-turn action cap.
5. Freshness/re-probe and invalidation after environment/VPCS changes; bound Runtime probe time/bytes and rate; never auto-create directories on a probe.
6. Do not invoke environment probes for answer-only requests, or fabricate an observation result from cached Planner prose.

Candidate source areas: `prerequisite_probes.py` (proposed), `runtime_environment.py`, `prerequisite_resolution.py`, `project_access.py` only where needed, `tool_capabilities.py` if capability exposure changes.

**Exit:** read-only proof drives node status, unknown/denied never unlock actions, eligible text-only traffic remains no-operational-Tool.

### PR-C — Authorized Remediation Planning & Execution

**Goal:** when a prerequisite is genuinely missing, choose and execute a legal, bounded remedy once.

Implementation:
1. Typed `RemediationPlanV1`: target requirement, original goal link, candidate immutable recipe ID + validated exact args, authoritative capability/scope proof, permission level, user approval receipt where required, expected observable postcondition, timeout/retry budget, rollback/compensation plan, action/transaction id.
2. Planner chooses between an allowlisted remedy and `BLOCKED`; Runtime performs policy preflight and maps vetted recipe to **existing canonical Tool**. No new generic “self-repair shell” entry point.
3. First permitted recipes: read-only re-probe; explicitly authorized `INDEX_ONLY` refresh; scoped output directory creation via existing authorized mechanism; narrowly authorized local Git init in a **new isolated fixture**; tightly scoped Python dependency repair **offline/local only** with pinned verified package when available.
4. Risk levels L0/L1/L2/L3, existing approval path, explicit request-owned paths, no global installer, network installation, arbitrary commands, user-home mutation or privilege escalation.
5. Persist pre-execution intent and action ID. After timeout/disconnect, reconcile Action Ledger and real side effect **before** considering retry; never duplicate a potentially executed install/`git init`.
6. After any remedy dispatch, mark `REPAIRING` or `INSTALLED_BUT_UNVERIFIED`; execution exit code alone cannot SATISFY prerequisite or complete original goal.
7. On tool failure/alternative strategy, Planner can replan within finite budget; no new authority, infinite retries, or hiding previous failed attempts.

Candidate source areas: `prerequisite_remediation.py`, existing command/path/approval/action-dispatcher interfaces. Command compiler/preflight item #7 is a *separate roadmap item*: v3 reuses existing preflight and restricts recipes; must not silently broaden #7 scope.

**Exit:** legal missing prerequisites can be remedied exactly once with corroborated action evidence; unauthorized/destructive/uncertain cases remain blocked and auditable.

### PR-D — Verify, Resume Original Action & Shared Delivery

**Goal:** ensure a repaired dependency genuinely satisfies predicate, then resume original work and deliver truthful progress/result.

Implementation:
1. Post-remedy verifier uses **independent scoped probe** with expected value/target and fresh environment/snapshot identity; `INSTALLED_BUT_UNVERIFIED` transitions to SATISFIED *only* on scoped PASS evidence.
2. Recompute DAG affected-descendant readiness and invalidate other affected facts; revalidate original action's request/epoch/intent/scope/plan revision, VPCS state and normal Action Admission.
3. Resume **only not-yet-executed** original step; before replay of a prior uncertain action, examine existing Action Ledger and objective state. OPEN Writer / QUEUED / RUNNING / UNKNOWN remain blocking. Never replay, abort, commit or erase protected historical tasks.
4. Persistent checkpoint/restart supports idempotent prerequisite progress and blocked state. No newly created top-level “repair task” that loses original task ownership.
5. Bind prerequisite receipts to accepted Progress, but do not substitute them for the original task's verification. Outcome and transport delivery remain separate `TaskResult` axes.
6. Route via Tri-One accepted Telegram/WebCopilot completion adapters; distinct user-visible updates: `CHECKING_PREREQUISITES`, `REPAIRING`, `VERIFYING`, `RESUMING`, `BLOCKED`. Map to existing Progress/TaskResult without silently introducing incompatible on-wire enums.
7. Finite evidence-aware replan/retry budget and dead-end detection. A task may truthfully finish with failure/unknown/blocked summary if no legal recovery; no SUCCESS by repair-only receipt.

Candidate source areas: `prerequisite_recovery.py`, `action_loop_state.py`, `task_progress.py`, accepted Tri-One `TaskResult` factories/adapters, `protocol_loop.py` *minimal guarded hook* only after accepted contract.

**Exit:** original requested action resumes after verified prerequisites, original TaskResult and receipt are correct, repair-only PASS never masquerades as task SUCCESS, no duplicate side effects after interruption.

## 7. Contract invariants across all stages

1. No optimistic `SUCCESS` based on Model prose, remedy command exit, missing file interpreted from probe failure, or unverified install.
2. Original user request is authorization ceiling; a prerequisite cannot expand workspace, target, tool capability or write permission.
3. One authoritative Action Ledger, one Progress/TaskResult pipeline. A new Prerequisite Ledger references evidence, not invented execution events.
4. Legacy `SMARTAGENT_PREREQUISITE_RESOLUTION_V2` advisory and disabled v3 preserve compatibility.
5. Pure TEXT_ONLY remains fast: no unnecessary full project scan, Python/Git probe or new model round.
6. Runtime identity and readiness, V9 single-fence/ACK/turn_commit, Condition Ledger, Terminal Receipt and delivery acceptance remain unchanged.
7. A stale VPCS `context_ref` must not be automatically rebound during a pinned read or justify an edit.
8. Non-terminal states remain visible: no promotion of `OPEN`, `QUEUED`, `RUNNING`, `PARTIAL`, `UNKNOWN`, `PAUSED` to accepted success.
9. On authorization denial, fail closed and ask through original user interface if a supported approval flow exists; no implied consent.
10. A negative safety IT can be formally PASS only for correct rejection — **the user task itself is not SUCCESS**.

## 8. Performance plan and release dependencies

**Measure, do not assume improvement**:
- `prerequisite_nodes_total / probed / missing / unknown / satisfied`
- `probe_count`, `probe_batch_size`, `probe_latency_ms`, `probe_bytes`, `cache_hits`, `stale_invalidations`
- `remediation_attempt_count`, `remediation_duplicate_execution_count` (target 0)
- `planner_turns`, `protocol_rejections`, `blocked_action_count`, `recovery_rounds`, `no_progress_rounds`
- `time_to_original_action_resume`, `end_to_end_latency`, `delivery_attempts`
- For TEXT_ONLY, regression target: **no additional operational Tool calls** vs accepted Phase 1 baseline.

Do not block indefinitely awaiting externally controlled approval/service. Persist state and surface a truthful wait/block reason. Defaults for finite retry/count/elapsed budgets are implementation decisions to finalize in PR-A, not fictitious measured outcomes.

## 9. Roadmap boundaries and formal DONE definition

**v3 in scope**: PR-A graph/ledger, PR-B scoped probes, PR-C authorized limited remedies, PR-D verified resume/shared result; per-stage UT/IT/Live/Regression and final single-SHA integrated formal acceptance.

**Out of scope**: full Tri-One Phase 2 ProtocolEngine refactor, unverified 3-adapter parity, blanket package auto-install / arbitrary PowerShell scripts / elevation, network supply-chain automation, GitHub Hybrid automatic modifications, dual WebGPT URL planner, global System Administrator control, and Action Compiler overhaul.

**DONE iff**:
- accepted Tri-One Phase 1 baseline is pinned and has correct loaded source provenance;
- all four v3 stages individually formally ACCEPTED (UT → IT → Live → Regression with actual evidence);
- final integrated candidate runs the full mandatory matrix and no protected Runtime negative safety regression;
- live original interface delivery receipts are confirmed, independent from original action PASS;
- user authorizes final promotion, no unresolved essential `BLOCKED/NOT_RUN` case and no fabricated evidence.

> This document only records future requirements. The accompanying test specification and execution handoff define validation; **no Source, test, Runtime deployment, Live task or evidence repository was changed by this planning effort.**
