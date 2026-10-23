# SmartAgent Runtime Evidence Foundation v1

Status: **feature branch implementation — Windows / full WebAgent regressions pending**.

## Design principle

Planner interprets user intent, predeclares the factual predicate, and decides
what to do next. Runtime observes local reality in the already-authorized
scope, preserves the raw producer response, normalizes a fact, and evaluates
the predeclared predicate. No natural-language comparison or dynamically
executed predicate code is permitted.

```text
User intent → Planner claim / completion_contract.success
            → Runtime admission (typed schema + exact condition binding)
            → authorized tool observation (raw evidence retained)
            → Evidence Adapter → normalized observation
            → Predicate Evaluator (PASS / FAIL / UNKNOWN)
            → Action Evidence / terminal verification → Planner
```

## First canonical fact tool: observe_fact

`observe_fact` provides an exact filesystem `file.exists` observation.
The `path` must be an authorized, absolute path. The effective path is
resolved by the existing `SecurityContext.require_read` boundary before
probing. It never mutates a file.

Example (condition text must be included in the task's predeclared
`report_progress.completion_contract.success`):

```json
{
  "tool": "observe_fact",
  "action_id": "ASSERT-NO-FILE",
  "path": "E:\\card\\release\\missing.txt",
  "condition_id": "The target file must not exist",
  "predicate": {
    "field": "file.exists",
    "operator": "equals",
    "expected": false
  }
}
```

| Raw filesystem observation | Normalized fact | Verification |
|---|---|---|
| authorized stat succeeds | `file.exists=true` | FAIL (expected false) |
| parent is accessible; final entry absent | `file.exists=false` | PASS (expected false) |
| parent missing / access denied to stat | `UNKNOWN` | UNKNOWN |
| request is outside read scope | `REJECTED`, no fact | Cannot prove absence |
| malformed claim / unsupported operator | `REJECTED`, not executed | SPEC_INVALID |

For a *successful negative assertion*, the business `raw_evidence.result`
stays `NOT_FOUND`, the normalized observation has `value=false`, and
Runtime's `expectation_status=PASS`. Tool execution status `SUCCEEDED`
means the observation completed, **not** that the file exists.

Without `predicate`, the read-only tool returns a fact and
`verification_status=UNVERIFIED`; Runtime never invents the user's intended
comparison after the tool runs.

## Evidence record v1

```json
{
  "schema": "SMARTAGENT_EVIDENCE_V1",
  "field": "file.exists",
  "value": false,
  "observation_status": "CONFIRMED",
  "source": "filesystem.stat",
  "scope": "E:\\card\\release\\missing.txt",
  "version": "",
  "observed_at": "2026-10-23T04:00:00+00:00",
  "freshness": "CURRENT",
  "raw_evidence_ref": "ASSERT-NO-FILE"
}
```

`value` is **absent** for UNKNOWN evidence. `observed_at` records the
observation instant; `freshness=CURRENT` describes the result when observed,
not a promise that a mutable filesystem fact remains current forever.
A later action must refresh or invalidate evidence before relying on it.
`version` is populated when available (e.g. Project Snapshot ID);
no invented file version is supplied by `stat`.

## Predicate contract

Supported fields: `file.exists`, `process.exit_code`,
`project.query_status`, `project.snapshot_id`.

Supported operators: `equals`, `not_equals`, `contains`,
`greater_than`, `less_than`. Field/operator/value type restrictions
are checked at admission. Arbitrary expressions, `eval`, regular-expression
code, and user-supplied Python are rejected. A generic `in_range`
without an explicit bounded range schema is intentionally deferred.

Evaluation is three-valued:
- **PASS**: a current confirmed observation satisfies the predicate.
- **FAIL**: a current confirmed observation contradicts it.
- **UNKNOWN**: absent, inaccessible, incomplete, stale, wrong-field, or
  mismatched-type observation.

`PASS` applies to one verified claim. It is not permission to bypass
filesystem access controls or to declare the entire task complete without
satisfying the other completion conditions.

## Additional read-only adapters

This first version also attaches `data.observations` to the existing
Runtime result for:

- `query_project`: structured `project.snapshot_id` and
  `project.query_status` observations from intact, nontruncated query
  results. The snapshot ID is used as the evidence version.
- `run_command`: `process.exit_code` **only** when Runtime already has
  a machine-owned `process_state=EXITED` and integer `exit_code` receipt.
  Arbitrary stdout/stderr text is never used to assert process facts.

For both tools, original execution status, VPCS Expected Outcome Contract,
error codes, and terminal admission remain **unchanged**. This v1 connects
their raw facts to a common schema; a wider cross-tool assertion API is a
separate, test-gated development step.

## Future work

Evidence persistence/immutability receipts, scoped invalidation after
mutations, cross-source contradictions, full typed ranges, and composition
(`all`/`any`) have **not** been implemented yet. The current branch
must not be treated as a completed universal fact database.

## Tests

PowerShell from repository root:

```powershell
$env:PYTHONPATH = "source"
python source/WebAgent/tests/validate_runtime_evidence_foundation.py
python source/WebAgent/tests/validate_vpcs_expected_outcome.py
python source/WebAgent/tests/validate_vpcs_runtime_pinned_snapshot.py
python source/WebAgent/tests/validate_query_project_matrix.py
python source/WebAgent/tests/validate_p1_result_contract.py
python -c "from agent_core.protocol_manifest import validate_protocol_manifest; print(validate_protocol_manifest('.'))"
```

Expected new focused marker: `RUNTIME_EVIDENCE_FOUNDATION_PASS`.
Confirm the actual WebAgent → Runtime terminal PASS in a local end-to-end
request before promoting the branch to `main`.
