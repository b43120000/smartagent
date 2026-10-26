# SmartAgent Round3 — IT16 Typed Path, Prerequisite Resolution, Bounded Repair

> Implementation note / v1 candidate. Baseline: `309305b2eeeb79aed0925dc4dfee45af5193f8b5`.
> This document describes the **code actually added on this branch** and separates it from the planned full Prerequisite Resolution system. It is **not** a claim that the earlier externally drafted `SmartAgent Prerequisite Resolution Design.md` has been fully implemented.

## 1. Motivation / Round2 observations

Round2 (`runtime_collect_evidence_IT_roundresult2.md`, last live section) submitted 10 requests, had 9 terminal receipts, and stopped when IT16 was still RUNNING at the 900-second sender timeout. Exact next Planner input delivery was NOT_OBSERVED for all 10. IT16 was meant to run a safe short command returning exit code 7, but hit command schema/verification, working-directory authorization, repair identity and further scope denials. The request path parser also consumed natural-language text after an unquoted Windows path.

**Do not** interpret a sender timeout as an official terminal event or relax a security permission to make this test pass.

## 2. Implemented v1

### 2.1 Typed Path lexical repair

`source/WebAgent/protocol_loop.py` recognizes additional **unquoted** Windows-path terminators only when separated by whitespace and followed by specific natural-language task predicates: `是否不存在`, `是否存在`, `測試範圍內透過`, `範圍內使用`. Quoted paths retain their literal contents; existing canonicalize/Scope authorization still runs unchanged.

This is a narrow repair. Ambiguous or novel forms must fail closed, not guessed into shorter permissions. No broad allowlist or canonical path-security check is removed.

### 2.2 Prerequisite Resolution v1

`source/agent_core/prerequisite_resolution.py` builds `SMARTAGENT_PREREQUISITE_RESOLUTION_V1` as a **request-scoped advisory** before the Planner chooses an action. It distinguishes known environment ledger tool availability from **not yet verified** exact path/working-directory/command contract and write authorization, including explicit guidance for safe recoverable and unrecoverable cases.

The integrated Planner prompt contains this description for commands and filesystem tasks. Pure response-only requests with no scoped filesystem inputs do not receive this extra host checklist. Tool existence in a ledger is *not* execution success or authorization.

**Important limits:** no auto-install or auto-directory creation is performed by this helper; authorized setup is still a distinct Planner-selected, Runtime-executed, verified Action. This v1 is the entry point for, **not the completion of**, the larger Prerequisite Resolution Design.

### 2.3 Bounded Repair v1

`source/agent_core/bounded_repair.py` tracks rejected **Planner turns** rather than individual rejected fields. `source/WebAgent/protocol_loop.py` uses it for v9 parse and ACK rejection routes. A fresh request resets the tracker.

The default limits are:
- Maximum 6 rejected rounds at the same Runtime evidence generation;
- Maximum 5 rejections of the same reason without new evidence;
- Maximum 16 rejected rounds across the request.

Only actual action-result entries marked `executed=True` advance evidence generation; changing prose/action IDs is not evidence. On exhaustion, Runtime emits `bounded_repair_observed` and `protocol_loop_paused`, transitions task to **PAUSED**, preserves evidence and raises a non-success RuntimeError. No SUCCESS, receipt, retry or additional permission is fabricated.

**Important limits:** this v1 bounds protocol/ACK rejections, not every possible pattern of legitimately executed but unproductive tools, and does not guarantee a fixed wall-clock duration. Timeouts, elapsed-time budget, safe task cancellation/resume, and no-progress detection across different executed tools remain future improvements.

## 3. Prerequisite resolution future phases (not yet implemented)

- Versioned structured requirement graph: `requirement → probe → remedy → verification → resume`, with proof of request/task ownership.
- Durable environment preflight ledger; distinguish MISSING, DENIED, UNKNOWN, AVAILABLE, INSTALLED_BUT_UNVERIFIED.
- For missing libraries/executables, plan authorized installation inside allowed workspace; independently verify binary/import before resuming original action.
- Explicit human approval route for actions crossing scope or introducing dependencies; never escalate or infer permission from the missing prerequisite.
- Finite remediation DAG/rollback & idempotency: do not replay side effects after ambiguous delivery.
- Global task deadlines and no-progress repair detection from normalized Runtime facts, not merely ACK denial.
- Transparent prerequisite progress and final report shared between Telegram and WebCopilot after the three-one interface unification.

## 4. Regression and acceptance gates

Run **locally with the pinned Git checkout, not by injecting tools into ChatGPT**:

```powershell
$env:PYTHONPATH = 'source'
python source/WebAgent/tests/validate_round3_prereq_repair.py
python source/WebAgent/tests/validate_authorized_path_extraction.py
python source/WebAgent/tests/validate_typed_paths_request_context.py
python source/WebAgent/tests/validate_action_loop_runtime_state.py
python source/WebAgent/tests/validate_runtime_evidence_foundation.py
```

These validators may have Windows-specific dependencies; capture exit code, traceback and SHA for each. **GitHub-only edits have not executed these local tests**. A passing unit test does not prove end-to-end live operation, request ownership or Planner delivery. Run the Round3 IT cases and report Raw Evidence / Ledger / Receipt / exact Planner input separately.

### Hold points

1. Existing Round2 IT16 request was RUNNING at report cutoff. Check it **read-only** and do not resubmit IT16 until its terminal state or clean worker isolation is proven.
2. No new Edge process, browser tab, ChatGPT conversation, binding or URL modification is required for this test handoff.
3. IT19 remains SAFETY_BLOCKED until chunked-write store/writer isolation is separately attested.
4. Keep `309305b` baseline immutable; deploy this **specific new candidate branch commit** only into an explicit test-only copy after verifying its SHA, never silently replace the active production package.
5. Do not lower success/FAIL/UNKNOWN/Receipt/Evidence admission thresholds.

## 5. Build/version evidence for Codex

The deployable **source revision is the final HEAD SHA on branch `fix/it16-prereq-repair-309305b-20261024`**. It is not the old production SHA `309305b`, not the evidence-doc branch, and not a separately built ZIP. Codex must record `git rev-parse HEAD`, package/release metadata, the command used to launch SmartAgent, and the actual loaded worker source SHA. If there is no verified executable built from that revision, mark `PACKAGE_NOT_BUILT/NOT_VERIFIED` rather than pretend the Git commit is an installed binary.
