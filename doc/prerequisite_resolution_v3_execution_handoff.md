# SmartAgent Prerequisite Resolution v3 — Execution Handoff / Codex Evidence Runbook

> **PLAN-ONLY HANDOFF.** This file is NOT permission for Source edits, deployment, Live requests, package installation, approval issuance, Git repository initialization or repair of existing tasks.  
> Read [v3 implementation plan](prerequisite_resolution_v3_design_plan.md) and [v3 UT/IT/Live matrix](prerequisite_resolution_v3_verification_matrix.md) before any execution.

## 1. Exact prerequisites to begin (hard stop)

1. Check `b43120000/smartagent` and confirm Tri-One v2 **Phase 1** 2A/5A/5B code, full UT/IT/Live and final acceptance, on an actually deployed product SHA. A Draft PR with documentation alone **does not** satisfy this.
2. Fetch latest `main`, code Manifest and installed Runtime evidence. Planning reference when v3 docs were authored: `main@b95be77876ac522248954f3127d4fb7b1024eed2` (2026-10-26), **not** a future implementation baseline.
3. Revisit the actual accepted Tri-One `TaskResult`, Progress, Task status and terminal/delivery schemas; any schema shown in v3 plan is a proposal and must be reconciled before PR-A coding.
4. Do **not** deploy into an active user workspace, overwrite unsaved work, reset hard, force merge, restart live workers, or re-run historical IT19/IT20 requests.
5. Only after user explicitly authorizes v3 execution may ChatGPT open a PR-A code branch; Codex does not independently decide or write architectural source changes.

## 2. Owner boundary

| Role | Responsibilities |
|---|---|
| **User** | Authorize new development, approve scoped test-side effects and critical releases |
| **ChatGPT** | Source/requirements review; design/RCA; remote Git code modifications; Source commits/PR review; stage-specific instructions; formal UT/IT/Live/Regression acceptance |
| **Codex** | Read exact pinned Source; inspect local available environment; isolated deployment; safely execute approved UT/IT/Live; produce raw logs, hashes, receipts and factual observations; do not invent verdicts or autonomously patch architecture |
| **SmartAgent Runtime** | Actually probes and executes admitted scoped operations; maintains action and prerequisite facts; never treats model prose as execution/permission evidence |

Codex may stop unsafe cases immediately but should continue safe independent test cases where user has approved the campaign. Source defects are reported to ChatGPT for RCA/new commit; never silently fix and rerun with an unpinned altered checkout.

## 3. Branch and test task layout (future, not yet created)

Suggested sequential implementation branches from **accepted prior stage**:
- `feat/prerequisite-v3-pr-a-typed-ledger`
- `feat/prerequisite-v3-pr-b-runtime-probes`
- `feat/prerequisite-v3-pr-c-authorized-remediation`
- `feat/prerequisite-v3-pr-d-verify-resume`

These names are proposals; no code branch is created by this planning handoff.

For durable test evidence, use private repo `b43120000/test`, **new namespace only**:

```text
tasks/PREREQUISITE-V3-20261026/
  intake/
    accepted-tri-one-baseline.md
    source-pin.json
  PR-A/
    task-spec.md
    ut/results.json
    it/results.json
    live/results.json
    regression/results.json
    acceptance.md
  PR-B/...
  PR-C/...
  PR-D/...
  final-acceptance.md
```

Each results file must link to **real** immutable raw logs/receipts and SHA-256; folders shown are organizational proposals, not pre-created evidence. User-private raw content/tokens/cookies must not be pushed into either repository; use redacted audit metadata/hashes in the evidence repo and approved restricted local raw logs.

## 4. Stage workflow — repeat PR-A → PR-B → PR-C → PR-D

### Stage 0: ChatGPT source design + pin
- Confirm old stage formal acceptance and user authorization.
- Review current `main`, the exact accepted prior code branch, Scope and Tool schemas, and stage-specific call paths.
- Produce explicit Source-change plan (paths, contract semantics, risks, feature flag, rollback, tests). Do not start PR-B while PR-A unaccepted.
- Commit feature branch changes to GitHub. Provide full SHA and mapping to planned test IDs.
- Verify any packaged official Manifest is updated/valid; it must not be treated as proof of loaded Worker code.

### Stage 1: Codex read-only preflight
- Verify workspace path, remote URL, clean tracked/untracked state, actual checkout SHA, Python environment, PID/worker ownership, protected tasks and official Manifest.
- Use an **isolated disposable fixture** for all write tests; never treat user’s development repo or historic IT tasks as a fixture.
- Verify approved bound conversation, CDP port, browser instance and login. Historical user constraint: Edge **instance 1, CDP port 12811 / profile_1**, no `edge5`. Reconfirm the actual authorized test binding before Live; do not assume this is currently available or start another instance.
- Inspect active Writer/Action/Outbox records read-only. If already OPEN/QUEUED/UNKNOWN, do not replay/abort/commit them.
- Verify user explicitly approved any PR-C/PR-D local write fixture: exact folder, command intent, side effects, rollback and authorization level. L2 without consent: BLOCKED; do not test by executing to see what happens.
- Confirm Tri-One Phase 1 accepted code/loaded source identity and its original-interface delivery path; no Live before source attestation.
- Publish `PREFLIGHT_PASS` or `BLOCKED_<specific_reason>`. Never invent a successful Source load from a Git commit.

### Stage 2: Codex UT
- Deploy pinned candidate into approved isolated test checkout. Avoid `reset --hard` or overwriting uncommitted user files.
- Execute only inspected UT scripts/fixtures; no model/browser/Telegram; capture stdout/stderr, exit code, invocation, cwd, environment summary, elapsed time, SHA-256.
- Check cross-stage and Tri-One compatibility: PR-A graph semantics, PR-B probe fact trust, PR-C remedy preflight, PR-D original-action resume. No synthetic success from an absent Runtime fact.
- Preserve failed attempts as separate immutable logs; continue safe independent cases.

### Stage 3: Codex IT
- Use deterministic fake Planner and isolated real action/ledger/event components. Explicit approved write fixtures only for PR-C/PR-D.
- For each case: original user intent, request/task/epoch, authorization scope, exact planned actions, prereq graph revision, evidence IDs, formal expected outcome, actual outcome.
- Inject UNKNOWN/denial/network/tool timeout using **mocks**, not destructive production operations. For a simulated ambiguous action, check reconciliation without replay.
- Where postcondition fails, assert original task action did **not** execute.
- Capture raw V9 turn blocks/ACK/turn_commit, Action Ledger, Probe Ledger, Preflight, TaskResult and Delivery Logs if applicable.
- Safe independent matrix cases proceed even after another case FAILs; dangerous cases depending on failed preflight remain BLOCKED.

### Stage 4: Codex Regression
- Run every relevant existing safety/terminal/Writer/Action/VPCS/Tri-One test on the **exact candidate SHA**. Confirm the actual CLI and side effects first; tests may require Windows-specific fixtures.
- Must preserve protected IT19 OPEN Writer fail-closed rules and IT20 scope/command boundary.
- Historical PASS of a different source commit cannot be counted as candidate PASS.
- Mark incomplete `NOT_RUN` or `BLOCKED` with objective cause.

### Stage 5: Codex Live, only if authorized
- Distinguish real **Telegram**, `LOCAL_TEST` simulation, and real **WebCopilot** origin. Simulated sender delivery is never interchangeable with real Telegram delivery evidence.
- Use unique **new** requests and Task epochs. Ensure exact approved fixture and one composer owner; no uncontrolled parallel WebGPT calls.
- PR-A Live: new graph/blocker visibility and legacy behavior; no remedy execution.
- PR-B Live: strictly read-only conclusive vs unknown facts; no write.
- PR-C Live: minimal scoped fixture mutations with specific consent and one-time receipts; no network/package escalation.
- PR-D Live: actual authorized prerequisite → independent verification → resume original action → terminal+original-interface delivery. A negative BLOCKED/UNKNOWN task stays non-SUCCESS.
- Never interpret sender timeout, stale UI text, queued upload, completed model token stream or an installer exit code as objective task success.
- Save each live request raw sanitized input/hash and model rounds, actual action/probe execution and postcondition, accepted terminal receipt, returned message/transport receipt and timestamps.

### Stage 6: ChatGPT formal acceptance / repair
- Retrieve complete actual evidence and check exact product/loaded SHA, no missing mandatory test. Compare each Test ID with expectation in `prerequisite_resolution_v3_verification_matrix.md`.
- Publish **case verdict** `PASS/FAIL/BLOCKED/NOT_RUN` separate from **Task Outcome**.
- If mismatch, perform RCA, commit minimal Source fix, reset only safe test fixtures with approved process, issue new pinned rerun instructions; never overwrite older attempt results.
- Formal `PR-X_ACCEPTED` only after all mandatory UT/IT/Live/Regression gates passed. No source promotion to next stage or `main` before user authorization.
- On final PR-D acceptance, perform combined final-source-SHA regression and cross-interface Live smoke before claiming `PREREQUISITE_V3_ACCEPTED`.

## 5. Suggested factual Codex report format

```text
DELIVERY_ID=PREREQ-V3-<STAGE>-<ROUND>-<DATE>
TASK_SCOPE=PR-A|PR-B|PR-C|PR-D
MODE=READ_ONLY|UT|IT|REGRESSION|LIVE
SOURCE_COMMIT=<actual full sha>
BASELINE_TRI_ONE_SHA=<actual accepted full sha>
WORKER_LOADED_SHA=<actual attested value or NOT_ATTESTED>
WORKSPACE=<observed canonical path>
MANIFEST=<PASS|FAIL|NOT_RUN>
BROWSER_BINDING=<actual confirmed instance/port or NOT_USED>
SAFE_FIXTURE_ROOT=<observed path or NOT_APPLICABLE>
NEW_REQUESTS=<actual count>; HISTORICAL_REPLAYS=0
UT=<passed>/<attempted>; IT=<passed>/<attempted>
REGRESSION=<passed>/<attempted>; LIVE=<passed>/<attempted>
MISSING_NODE_STATUS=<observed per requirement>
REMEDIATION_ACTION_IDS=<observed IDs, never invented>
ORIGINAL_ACTION_IDS=<observed IDs, never invented>
TERMINAL_RECEIPTS=<observed>; DELIVERY_RECEIPTS=<observed>
FAILED_CASES=<IDs and exact diagnostic>
BLOCKED_CASES=<IDs and why>
UNRESOLVED_STATES=<OPEN/UNKNOWN/RUNNING/QUEUED/PAUSED>
EVIDENCE=<actual path + bytes + SHA-256>
NEXT_DECISION_OWNER=CHATGPT
```

Results not observed must say `NOT_OBSERVED`, not “pass”. A file *directory* is not evidence that every case ran; report actual files/hash/contents.

## 6. Repair/rollback and safe stop

- Planning docs branch stays Source-free. Future implementation flags OFF by default and reversible without deleting pending ledgers.
- A failed PR-C/PR-D remedy attempt cannot trigger automatic rollback of unknown side effects. Only execute a validated compensation if its exact state, scope, action and consent are proven; otherwise report PAUSED/BLOCKED with preserved journal.
- On unexpected browser binding, missing approval, provenance mismatch, active protected task or inconsistent ledger: stop affected Live action before dispatch, record `BLOCKED`, do not improvise a new browser URL.
- No package downloads from untrusted URLs, uncontrolled `pip install`, `winget`, `curl | shell`, `reset --hard`, or admin escalation for the sake of getting a PASS.
- No production environment changes as a side effect of running read-only UT/IT.

## 7. Continuation instructions for the next conversation

When the user asks to begin v3, first check **Tri-One Phase 1 actually accepted**, find exact final Source SHA/accepted TaskResult schema, and inspect `main` divergence. Then start **PR-A only** and issue a read-only baseline/mapping task to Codex before writing implementation Source.

Do not infer acceptance from the creation of this planning document or a Draft PR. Phase 2 Tri-One 2B/2C is unrelated unless separately authorized.
