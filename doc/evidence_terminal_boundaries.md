# SmartAgent Evidence Terminal Boundaries — independent fix branch

Base commit: `f242e5f9ae07d84dbe20994b61e9cff1e5a3152b` (Runtime Evidence Foundation v1).

Scope: **do not change the tri-valued predicate evaluator** and do not merge this branch to `main` before Windows E2E testing. The original observed `true != false` predicate stays FAIL; an UNKNOWN observation never becomes `file.exists=false`.

## Reproduced E2E cases

- Confirmed FAIL: `observe_fact` succeeded on an existing file, returned `FOUND → file.exists=true → predicate FAIL`. Planner submitted `COMPLETE/FAILED`; the old Telegram report did not contain a Runtime terminal acceptance receipt.
- UNKNOWN: `observe_fact` finished on a child of an absent parent, returned `raw UNKNOWN → observation UNKNOWN (no value) → predicate UNKNOWN`. Prior Runtime treated tool status UNKNOWN like pending execution and repeatedly demanded other read tools; Telegram surfaced a RuntimeError.

## Boundaries implemented

1. A canonical stat producer adds `data.execution_finality=COMPLETED` even when the value is indeterminate. Tool status remains `UNKNOWN`, so existing callers do not silently change meaning. A strict `completed_unknown_fact_observation` guard checks the Runtime-owned result envelope, action identity, source, schema, UNKNOWN observation with **no value**, and completion marker. Other tools, malformed envelopes, and stale entries do not qualify.
2. The Action Loop now classifies only those receipts as `OBSERVATION_UNKNOWN` rather than `MISSING` or pending execution. It emits the objective blocker `FACT_OBSERVATION_INDETERMINATE`, prevents replaying the exact same observation, and keeps the predicate terminal SUCCESS-ineligible. Unfinished commands/writes/delivery keep their old `RUNNING/QUEUED/PARTIAL/UNKNOWN` protections.
3. The `context_request.kind=BLOCKED` interruption gate accepts only the exact current-request, active completed UNKNOWN observation. The submitted terminal `evidence_refs` must contain its action ID; unresolved mutating/delivery actions and deferred action repairs prohibit this exception. A read that **could not confirm a fact** is sufficient to report a truthful UNKNOWN interruption; it is not proof that no alternative read exists. The Planner should explain any user prohibition on alternatives. The Runtime does not infer permission from prose.
4. A `COMPLETE/FAILED` terminal with referenced `observe_fact` evidence must cite FAIL from a probe bound to an accepted `completion_contract.success` condition before the probe. An unrelated action failure cannot impersonate a failed fact assertion. Existing generic FAILED terminal checks remain.
5. Following an accepted terminal turn, Runtime emits `runtime_terminal_accepted` with a receipt: request/task/epoch, round, ACK, outcome, condition ID, evidence refs and evidence verdict. `COMPLETE/FAILED` keeps its red failed-verification outcome; a genuine accepted `INTERRUPT/UNKNOWN` is routed by Telegram as `TASK_INTERRUPTED`, not a generic tool/runtime crash. The receipt is an **admission receipt**, never proof of Predicate PASS.

## Test matrix

| Case | Execution | Observation | Predicate | Allowed terminal |
|---|---|---|---|---|
| Missing file with readable parent, expect false | SUCCEEDED | CONFIRMED false | PASS | COMPLETE/SUCCESS (subject to other conditions) |
| Existing file, expect false | SUCCEEDED | CONFIRMED true | FAIL | COMPLETE/FAILED with bound evidence |
| Child under absent parent | UNKNOWN, executed, finality COMPLETED | UNKNOWN, no value | UNKNOWN | INTERRUPT/UNKNOWN with objective receipt and no unsafe outstanding work |
| Out-of-scope path | REJECTED, not executed | No fact | UNVERIFIED | No SUCCESS, pre-existing objective rejection rules |
| UNKNOWN command or open write | UNKNOWN or RUNNING | Not a completed fact | UNKNOWN | Cannot use the fact-only shortcut |
| Unrelated FAIL / stale observation | Any | Not an admissible matched fact | Any | Cannot be used to claim a bound failed-fact terminal |

### Developer regression (Windows PowerShell)

```powershell
$env:PYTHONPATH = "source"
python source/WebAgent/tests/validate_evidence_terminal_boundaries.py
python source/WebAgent/tests/validate_runtime_evidence_foundation.py
python source/WebAgent/tests/validate_vpcs_expected_outcome.py
python source/WebAgent/tests/validate_startup_protocol_flow.py
python -c "from agent_core.protocol_manifest import validate_protocol_manifest; print(validate_protocol_manifest('.'))"
```

Expected new marker: `EVIDENCE_TERMINAL_BOUNDARIES_PASS`.

### Real Web UI / Telegram E2E before merge

- Repeat the **Confirmed FAIL** request with `sample.py`, ensuring its presence independently. Require `SUCCEEDED + FOUND + FAIL`, `COMPLETE/FAILED`, and a matching `runtime_terminal_accepted` event/receipt. Do not change or delete the file.
- Repeat the **UNKNOWN** request with a child of an absent parent. Require `executed=true + UNKNOWN fact with no value`, `INTERRUPT/UNKNOWN`, `runtime_terminal_accepted`, and Telegram `TASK_INTERRUPTED` rather than `TASK_FAILED`. No file mutation and no substitute observation.
- Repeat unsafe probes: unknown mutation, opened chunk transaction, incorrect/stale evidence ref, unsupported predicate, condition mismatch, and spoofed/noncanonical UNKNOWN result. No SUCCESS and no fact-only interrupt exception.

**Validation limit:** GitHub connector edits do not execute code on the user's Windows environment. Check the above scripts and full E2Es locally before merging.
