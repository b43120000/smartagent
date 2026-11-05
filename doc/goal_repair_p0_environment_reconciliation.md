# P0 Contract Repair and Project Sync Goal Reconciliation

Base: 89ffa6ed0e2972d45894dcdf2cd084189ae28c47

## P0-1 Repair convergence

- CCRC inspects nested Command diagnostics, not just the outer admission reason.
- A single unadmitted action reconstruction can be followed by bounded
  field-only repair instead of immediate strategy replan.
- Contract issue convergence requires the remaining field defects to be a
  strict subset. Changed action IDs or error wording are not progress.

## P0-2 Repair Budget

- Up to two strictly verified contract-convergence credits per Evidence generation.
- Cumulative reject, repeated-reason, and GAR hard ceilings remain in force.
- No external evidence, task progress, success, or authorization is fabricated.

## P0-3 Progress/Action isolation

- Pending Progress actions are still unadmitted proposals.
- A previously committed/executed Action ID cannot enter a pending hold
  or be restored from one.
- The normal full admission chain remains mandatory.

## Goal Environment Reconciliation

project_sync(strategy=INDEX_ONLY) now accepts optional goal_conditions.
Existing build_project_capsule creates the Project Snapshot; reconciliation
reads that existing snapshot without another filesystem inventory pass.

Allowed predicates: file_exists, file_sha256, git_head, dependency_candidate.
Each predicate has a unique id and explicit expected values where needed.
Outputs: VERIFIED, MISSING, UNSATISFIED, UNKNOWN.

Ignored paths and dependency index candidates yield UNKNOWN, not verified.
These observations never grant write permission or imply Goal completion.
Re-probe real dependency usability with authorized Runtime tools.

## Regression

    python -m unittest source/WebAgent/tests/test_goal_repair_p0_env.py -v

Run alongside the Manifest validator and Project Sync/Progress/Command suites
on the Windows deployment machine before merging into main.

## P0-4: completed negative directory observations

`inspect_directory` now treats **one** authorized and complete filesystem-stat
`path_not_found` as a successful observation (`DIRECTORY_ABSENCE_OBSERVED`),
while preserving `FAILED/REJECTED/UNKNOWN` for permission errors, incomplete
enumeration, mismatched identities, and unsafe scope.

GAST rechecks the exact committed receipt and original Action path before
classifying a missing directory as `VERIFIED_NEGATIVE_FACT`; this unlocks a
Planner-selected `PREREQUISITE_RESOLUTION` proposal through existing admission.
`build_transition_readiness` exposes bounded environment facts with explicit
`PREDECLARED` / `UNBOUND` Goal condition linkage. Neither observation nor
readiness grants authorization, creates a directory, or verifies overall Goal
completion.

Regression: `python -m unittest source/WebAgent/tests/test_goal_repair_p0_env.py -v`.

## P0-5: Goal predicate reconciliation with trusted Runtime directory receipts

`project_sync(strategy=INDEX_ONLY)` supports explicit `directory_exists`
conditions. A directory containing inventoried files is VERIFIED from the
existing snapshot. An empty or absent directory is UNKNOWN unless the same
Request/Epoch already has an ACTIVE, canonical `inspect_directory` receipt
bound to the exact target path. A verified negative read produces MISSING,
not a Scope denial or a premature Task failure.

The Project Sync snapshot is reused without extra scanning, and the latest
24 Runtime receipts form a bounded source. Conflicting snapshot and live
observations stay UNKNOWN. No Planner-generated fact or old Request can supply
authorization or mark Task completion.
