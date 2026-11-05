# P0 Terminal Goal Target Contract (2026-11-06)

## Purpose
A deployment Action Verification PASS does not guarantee the package was
delivered at the destination specified in the original user's Goal.

## Pre-execution
Runtime extracts an explicitly labeled deployment root from the original
request, NOT from a later Planner suggestion. An ambiguous root fails closed.
A separate exception flag records if the user permitted relocation in the
presence of a verified overwrite conflict.

Runtime makes a read-only, request-scoped top-level directory observation
before the first Tool Action. It distinguishes ABSENT, EMPTY, OCCUPIED,
CONFLICT and UNKNOWN. Scope denial, I/O error, reparse points and oversized
directory enumeration are UNKNOWN, not missing. No writes or move operations
are introduced by this gate.

A prompt can declare expected direct-root entries using the label
預期包含：case_a、case_b、case_c、fixture_manifest.json.

## Terminal gate
COMPLETE/SUCCESS needs BOTH ordinary Action/Condition Evidence PASS and
this immutable requested-target check to PASS. A target must retain its
pre/post identity and have an unambiguous newly delivered direct payload.
Preexisting entries cannot be silently overwritten. Unchanged already-present
content requires independent reuse proof rather than a new deployment claim.

Relocation PASS requires an original-request exception, a genuine
pre-mutation collision on a declared expected entry (existing directory
alone is not collision), a uniquely identifiable new isolated destination,
independent filesystem observation of expected entries, and disclosure of
the actual destination to the user. It does not bypass authorization.

A failed check returns terminal_goal_target_unverified with a precise reason
and repair guidance. The accepted Terminal Receipt includes requested_target,
actual_target, pre_mutation_state, post_observation_state and
relocation_justification. No invented PASS or silent path rewrite.

## Existing Case D layout
Requested:
E:\SmartAgent_GoalDriven_IT_GH_v1
Currently observed deployment:
E:\SmartAgent_GoalDriven_IT_GH_v1\case_d_deploy_20261106_01\SmartAgent_GoalDriven_IT

If the requested root contains only that wrapper, the requested root is
OCCUPIED but has no direct case_a/case_b/case_c; simply having the wrapper
does NOT justify relocation. The Runtime must not move it automatically.
An authorized Planner may choose a non-destructive copy/deployment into the
requested root, followed by independent checksum and exact-path verification.
The path checker itself performs zero mutation.

## Regression
python -m unittest source/WebAgent/tests/test_target_path_contract.py -v

Windows Live validation remains needed: ACL, junctions, paths with drive
letters, actual terminal acceptance, and fixture checksum verification.
