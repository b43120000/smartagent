# Goal-Directed Prerequisite Resolution (GDPR) — V1

## Problem and acceptance target

Live Case D (TASK-C62F8E876F624B3E175E, installed fe118dee) confirms
the destination is absent. The first Planner round referenced unresolved
Request Context, the next inspected the missing destination, and a subsequent
round ended with INTERRUPT/UNKNOWN because the earlier deployment source was
not verified. **A missing destination is not proof that an existing source is
required**, and a failed list of that already-missing destination is not useful
progress.

Goal: prioritize the earliest missing prerequisite that causally blocks the
Goal, rather than repeatedly inspecting absent paths or blindly widening scope.

## Decision contract

1. Reconstruct current-epoch filesystem facts solely from committed, active
   Runtime Action receipts. Reject scope failures, unexecuted reads and stale
   epochs as absence evidence.
2. Interpret the user Goal conservatively:
   - **NOT_REQUIRED**: explicit create-new directory/file without any
     source/copy/deployment dependency. After verified destination absence,
     propose the direct Goal Action for *ordinary admission*. Do not search
     for an irrelevant source.
   - **REQUIRED**: explicit prior/existing source, restore, copy, download,
     archive extraction, etc. Discover only existing explicitly authorized
     read candidates, verify source and conflict conditions before deployment.
   - **UNDETERMINED**: vague deployment instructions. Do not infer source
     availability, invent a blank replacement, or authorize a write.
   - **NOT_APPLICABLE**: globally read-only Goal.
3. Do not re-list a destination already established as missing, including
   find_file rooted at that same absent destination. Additional observations
   require new evidence or changed circumstances.
4. Runtime publishes goal_prerequisite_resolution in the Planner Context.
   Candidate reads and action categories **are not Tool Admission**. The
   Planner chooses a concrete Action; existing workspace/ACL/approval,
   request write ceiling and tool preflight remain authoritative.
5. If an explicit creation Goal can proceed to Tool Admission, an interrupt
   asking for an irrelevant source is rejected with
   GOAL_PREREQUISITE_ACTION_PREFERRED. A request for actual write permission,
   missing implementation details, or an objectively blocked operation stays
   valid. No automatic mutation is performed.

## Case D

The explicit prior-deployment-source language in Case D makes its source
REQUIRED. With a confirmed absent target, an independently authorized source
candidate calls for scoped discovery; without one, requesting the real source
location or scope is appropriate. The private GitHub test fixture or historical
subdirectory mentioned in earlier logs is a *lead*, not a current-epoch
verified source or an automatic authorization.

## Non-goals and security

The resolver does not change Goal text, select or execute tools, recursively
search parents, infer write permission from read scope, bypass ACL, overwrite
files, fabricate success, or convert old evidence into current observations.

## Regression gates

- Direct-create Goal must prefer normal Goal Action Admission after absence.
- Existing-source Goal must preserve source discovery and original constraints.
- Confirmed absent destination must not be relisted or searched as a root.
- Same observation from another task/epoch or an unexecuted Action must not
  enable a mutation preference.
- Read-only Goals never get a create recommendation.
- Existing context, Planner, action/goal and core manifest regressions pass.
- Real Windows Case D D4–D7 remains unverified until independently run.
