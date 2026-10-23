# VPCS Runtime Expected Outcome Contract (MVP)

Status: **feature branch implementation**, pending local regression / WebAgent end-to-end validation.

## Purpose

A negative test may be successful when the Runtime **actually observes the
specific expected error**. The tool's business result must remain intact even
when the Runtime verifies that its error is an expected outcome.

This is not permission to classify every tool error as success. The initial
implementation covers only a *single* snapshot-bound `query_project`
`read_context_ref` operation.

## Action contract

The Planner must declare a success condition in `report_progress.completion_contract.success`
**before** the action. The query must bind to that exact condition via
`condition_id` (either the exact condition text or its Runtime condition ID).

Example query action (the values of project_root / snapshot_id / context_ref
must come from real Runtime evidence):

```json
{
  "tool": "query_project",
  "action_id": "Q-VERIFY-STALE",
  "project_root": "E:\\card\\release",
  "snapshot_id": "<snapshot A>",
  "condition_id": "Old pinned reference must report project_snapshot_stale",
  "queries": [{
    "operation": "read_context_ref",
    "context_ref": "<context ref bound to snapshot A>"
  }],
  "expected_outcome": {
    "query_index": 1,
    "status": "ERROR",
    "error": "project_snapshot_stale:sample.py"
  }
}
```

Another supported exact `error` value is
`context_ref_snapshot_mismatch`.

Allowed errors are deliberately restricted to these VPCS semantics. A
generic `permission_denied`, `FileNotFoundError`, HTTP error, arbitrary
substring, regex or a `status=ERROR` wildcard cannot be declared as an
expected outcome in this MVP.

## Runtime outcome layers

| Layer | Exact matched negative query | Unexpected query error |
|---|---|---|
| Raw business `results[0].status` | `ERROR` | `ERROR` |
| Raw business `results[0].error` | Preserved verbatim | Preserved verbatim |
| Runtime `status` | `SUCCEEDED` (assertion checked) | `FAILED` |
| `data.observation_status` | `ERROR` | `ERROR` |
| `data.expectation_status` | `PASS` | `FAIL` |
| `data.verification_status` | `PASS` | `FAIL` |
| Terminal eligibility | `true` | `false` |

A status of `SUCCEEDED` means the **negative assertion** was satisfied,
not that the source was read successfully. The raw query error is always
retained under `results`.

If the query did not complete, the result was truncated, or the response does
not meet the trusted query schema, the Runtime must **not** declare PASS.
Preflight / scope rejections must retain their `REJECTED` status, and
unsupported expectation shapes are rejected **before execution**.

The expectation is part of the action signature and cannot be attached
retroactively to a cached action ID. A separate completion contract prevents
the Planner from inventing a passing condition only after seeing a failure.

## Compatibility

- Query actions **without** `expected_outcome` use the existing
  `query_project` result policy unchanged.
- Normal discovery queries keep their automatic index-rebuild strategy.
- `read_context_ref` remains pinned; stale reads do not silently rebuild
  the index, and a new reference requires explicit synchronization.
- Existing `run_command.expected_failure` /
  `run_command.expected_verification` contracts are unchanged.
- Actual mutation success, admission authorization and task completion remain
  separate requirements. One passing negative assertion cannot conceal other
  failed or unverified actions.

## Validation

On Windows PowerShell, from the feature branch:

```powershell
$env:PYTHONPATH = "source"
python source/WebAgent/tests/validate_vpcs_context_reference.py
python source/WebAgent/tests/validate_vpcs_runtime_pinned_snapshot.py
python source/WebAgent/tests/validate_vpcs_expected_outcome.py
python source/WebAgent/tests/validate_query_project_matrix.py
python source/WebAgent/tests/validate_p1_result_contract.py
```

Expected new markers when tests actually pass:
`VPCS_EXPECTED_OUTCOME_PASS` and
`VPCS_EXPECTED_PROTOCOL_TERMINAL_PASS`.

The new test covers precise matching, mismatched errors, schema rejection,
unchanged unmarked queries, no implicit sync, explicit renewal, prevention of
post-hoc expectation substitution, and real WebAgent terminal admission.

**Do not mark this branch verified until these tests and the user's real
SmartAgent Phase 1–6 prompt have been run.**
