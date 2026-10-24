# SmartAgent IT4 Candidate — Implementation Scope and Validation Holds

## Base and release identity

- Immutable source baseline: `309305b2eeeb79aed0925dc4dfee45af5193f8b5`.
- IT3 source candidate and IT4 direct parent: `3ce1d74dd797acee66f27db44fbd8a5425b29664`.
- IT4 branch: `fix/it4-runtime-evidence-repair-20261024`.
- Run IT4 using the **final pinned IT4 commit SHA** in `doc/runtime_collect_evidence_IT4.md`, never floating HEAD/main. A committed source tree is **not** a built or running Windows package. Codex must attest real Supervisor/Worker source loaded SHA in the approved TEST instance.
- Old IT3 package, existing ChatGPT conversation, binding, existing browser, source baseline and active unrelated tasks must not be silently modified.

## What this branch actually implements

### Command exit evidence (source/agent_core/tools.py)

A runtime-owned PowerShell epilogue resets `LASTEXITCODE`, executes the admitted user command **unchanged** inside a scriptblock, then exits with the actual native subprocess `LASTEXITCODE` when available (otherwise the post-scriptblock PowerShell success status). The original command still goes through the same `require_command_allowed` and `run_bounded_process` security checks; the extra shell syntax is not user authority.

This is a narrower remedy for IT16. It does not introduce a general native argv executor. A Windows regression checks a real child `SystemExit(7)` returns 7 and a shell-only read-only command returns 0. Source still records raw `execution_status=FAILED` for nonzero codes, with a **separate** `expected_failure` assertion when predeclared. No exit code is fabricated or rewritten into SUCCEEDED.

**Remaining risks to verify:** quoting/nested PowerShell semantics, exact native interpreter used, command security admission, and verification of negative expected failure against exit code 7. A malformed quoting case must fail closed and be reported; do not auto-rewrite user commands.

### Bounded Repair & Semantic Stagnation (bounded_repair.py + WebAgent/protocol_loop.py)

Keep IT3's ACK/parse rejection budget. Add a request-scoped normalized blocker tracker for repeated Scope/Preflight/Authorization rejection **even when tool name/action_id changes**; four consecutive same blocker types cause a deferred safe PAUSED checkpoint before the next Planner call. Do not call an already attempted action again, and do not convert negative `observe_fact` predicate FAIL into an infrastructure error.

Add a **soft** elapsed guard: if three or more protocol rejections occurred and at least 600 seconds elapsed on the active request, pause before the next Planner turn. The limit does not forcibly terminate an in-flight tool/browser call, is not a hard wall-clock SLA, and yields **PAUSED with no terminal receipt**, not an invented FAILED/SUCCESS. See deterministic pure-function gate regressions.

**Remaining work:** persist budget and pending blocker states safely across continuation/resume; consider stale-but-SUCCEEDED read-result detection using normalized digest; no global forced deadline or arbitrary cancellation is implemented in IT4.

### Prerequisite Resolution v2 advisory (prerequisite_resolution.py)

Classifies explicit Project Query intents and adds `project_root`, `project_workspace` and `project_snapshot` check requirements before choosing `query_project`. It instructs Planner to obtain exact authorized Root/Scope/Snapshot and select a normal authorized `INDEX_ONLY` route only if required and permitted.

This remains a **non-executing advisory**. It does not install tools, create files, synthesize snapshots, grant parent-directory access, infer a user-approved project root, or mark a missing prerequisite as PASS.

### Terminal reference provenance (evidence_identity_audit.py + protocol_loop.py)

Emit a separate `terminal_evidence_identity_audit` event before receipt rendering, comparing actual current-request Action Result ledger references and their Request/Task Epoch. Records only counts/status, not raw private identifiers, paths, tokens or ChatGPT URL.

This is **audit/observability only**, not a claim that IT3's repeated sanitized `ACTION_008` means leakage, and not a changed terminal success admission gate. If raw evidence confirms truly foreign or unknown references, stop live tests and do safety RCA before altering receipt validator.

### Test suite

- `source/WebAgent/tests/validate_it4_runtime_repairs.py`: native PowerShell exit 7, shell-only exit 0, no-bypass prerequisite, repeated blocker guard, pure elapsed threshold, request-bound evidence identity.
- Existing IT3 regression validators remain in `doc/runtime_collect_evidence_IT4.md`.
- **No Windows tests or live SmartAgent IT have been executed by GitHub editing.** All new code is a candidate until Codex's pinned local Regression and actual-runtime checks.

## Items intentionally not rewritten

- **IT07:** Live model may refuse to emit an illegal condition. Test deterministic Tool Admission boundary separately without invoking user data/side effects; distinguish pre-admission Planner refusal from actual Runtime admission rejection.
- **IT17:** Project scopes remain fail-closed. Planner advisory can guide legal probes; no automatic scope authorization or snapshot forgery.
- **EV-01:** IT3's 12 `PLANNER_DELIVERY_NOT_OBSERVED` rounds must first be classified by whether a new Runtime Tool Result was actually due. The collector was not replaced on this branch; Codex does the offline raw-input correlation audit, retaining `NOT_OBSERVED` where evidence is missing.
- **IT23:** Production callback count and exactly-once receiver delivery require original local outbox/callback evidence, not a synthetic local archive callback or ChatGPT self-report. No production transport callback edits in this branch.
- **IT19:** `SAFETY_BLOCKED` while independent chunked-write state root/writer isolation is not proven.
- **Fast path / three-one interface / Versioned Project Context:** separate planned tracks, not silently merged into IT4.

## No-regression constraints

Do not loosen any existing scope, path authorization, Condition Ledger binding, Tool execution identity, expected-failure timing, UNKNOWN truth value, receipt provenance or external callback semantics. Safety denials are not blanket product failures; judge negative IT cases by the exact original predicates and by what really executed.

## Suggested sequence for Codex

1. Verify IT3 source SHA and absence of active/pending unknown task; preserve original baseline and browser binding.
2. Deploy pinned IT4 candidate into user-authorized TEST package; prove actual Supervisor/Worker loaded SHA.
3. Run focused new validator then all listed prior validators; any failed gate means **NO LIVE DISPATCH**.
4. Offline audit EV-02 actual unredacted cross-request refs and EV-01 expected/new result classes.
5. Only if safe, dispatch `doc/runtime_collect_evidence_IT4.md` 9 new cases, sequentially, with no second browser/chat and no retries of ambiguous requests.
6. Write `C:\Users\user\Desktop\remoteagent\release\testreport\runtime_collect_evidence_IT_roundresult4.md`, each item immediately; do not change product source or perform root-cause fix in the Live test phase.

The user will bring the resulting report back to ChatGPT for analysis; Codex should not start IT5.
