# Shared Semantic Registry + Action Loop Blackboard — Live Task-1D73 RCA

**Base**: 4e51e38442420e75e103c6e14eef460b17b13204
**Regression**: TASK-1D73A945513F4CABF694
**Original request**: \`創立這個資料夾\nE:\SmartAgent_GoalDriven_IT_GH_v1\`

## Root cause (Live evidence)

1. Round 1 native \`inspect_directory\` succeeded with
   \`DIRECTORY_ABSENCE_OBSERVED\`.
2. Round 2 Planner correctly proposed \`create_directory\`, but Action Loop
   rejected it with \`required_transition_action_missing\`.
3. Round 3–5 repeated \`run_command\` repair and failed verification schema
   and working-directory preflight.
4. Round 6 Progress payload was rejected and the task was paused.
5. On every context snapshot the Strategy Registry reported zero candidates.
   This was NOT a filesystem write failure: the correct action never ran.

## Integration (no second Goal Intent Resolver)

- Existing \`semantic_capability_resolution.py\` now owns a canonical
  semantic **tool catalog** generated from Tool Envelope schemas; every
  registered Tool receives one canonical action descriptor, a typed-field
  schema, side-effect metadata and non-authorizing scope/verification caveats.
  Fixed Chinese/English aliases are registered at the same location.
- The legacy SCR model-selected read capability contract is preserved.
  \`translate_goal_action(original_request)\` is only a semantic hint, **not
  a permission or a claim of path existence**.
- \`conditional_goal_policy\` consumes the same translator for creation
  intent. \`target_path_contract\` can bind an exact original-request
  DIRECTORY_ONLY target without misusing deployment-only keywords.
- \`semantic_path_resolution\` recognizes the already translated creation
  intent and never incorrectly treats a creation request as read-only.

## Shared Blackboard and Action Loop causal semantics

- Action Loop receives the actual current-epoch \`reconstruct_goal_gaps()\`
  output and the same \`DIR-001\` rule used by Planner Context.
- A **confirmed** last native \`inspect_directory\` result for the **exact
  original Goal** plus DIR-001 \`UNTRIED\` and actual Scope Preflight is required
  before \`create_directory\` may appear in \`allowed_next_actions\`.
- It remains impossible to infer permission from negative observation, a
  rejected Tool Result, or merely mentioning \`E:\`.
- \`ACTION_LOOP_DECISION_EVALUATED\` is a new read-only Blackboard event
  describing state_id, required transition, candidate tool set, evidence
  state, rule ID, reason code and originating observation ref. This event
  reports a **Runtime decision**, not an execution result.
- \`sections.actions.data.action_loop_decision\` and
  \`sections.progress.data.action_loop_state\` are projections of the same
  authoritative Action Loop state. The Planner Context also carries that
  decision and the semantic translator's non-authorizing result.
- The admission boundary checks stale/mismatched DIR-001 Blackboard
  state identifiers. Every actual Tool Action still passes the canonical
  Envelope, current request's Write Ceiling, ACL and native Tool Scope.

## E2E regression requirements

Original Traditional Chinese command must produce
\`CREATE_DIRECTORY\` and bind the exact target; Round 1 must stat an absent
directory; Round 2 Action Loop must admit canonical \`create_directory\`
and cite the first observation's EvidenceRef; a real native temp-directory
creation must succeed and become \`directory.exists=true\` on the next
Goal-Gap reconstruction. No two-step simulation may bypass evidence or
grant parent/sibling write scope.

Completing a directory-only Goal additionally uses
\`target_kind=DIRECTORY_ONLY\` in the independent target compliance check:
an empty new directory is sufficient **only after independent evidence
admission**, unlike a source deployment that must contain payload entries.

This is a general correction of semantic/tool/Action Loop alignment rather
than a hardcoded exception for the specific E: path.
