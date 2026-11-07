# SCR → Blackboard → Action Loop: canonical directory action integration

## Live failure reproduced

Source: \`b43120000/test\` RCA for
\`TASK-1D73A945513F4CABF694\`,
\`RR-TELEGRAM-F07E199413472CCF\`; installed baseline
\`4e51e38442420e75e103c6e14eef460b17b13204\`.

- Round 1: native inspect successfully established exact directory absence.
- Round 2: Planner emitted correct \`create_directory\`, but Action Loop
  rejected \`required_transition_action_missing\`.
- Rounds 3–5: Planner fell back to \`run_command\`; verification contract and
  command working-directory guards rejected it before execution.
- Round 6: invalid terminal Progress repair; task paused on repair deadline.
  Zero filesystem mutation actions completed.

The baseline's SCR registry had only read capabilities, its DIR-001 route
depended exclusively on the *deployment* target contract, and Action Loop's
\`OBSERVED_NEGATIVE\` branch did not advertise native \`create_directory\`.

## Single shared semantic vocabulary, not a second translator

\`source/agent_core/semantic_capability_resolution.py\` is the existing
canonical SCR owner; it now includes \`directory.create → create_directory\`.
\`ACTION_VOCABULARY\` lists fixed phrases for every finite SCR capability,
including \`創立\`, \`建立\`, \`創建\`, \`新增\`, \`新建\`, and English
equivalents. \`translate_request_action\` returns a bounded, non-authorizing
current original-request translation. \`validate_action_vocabulary\`
ensures every fixed phrase maps to an actual Tool Envelope schema.
Other tools without a fixed SCR lexicon remain model-selected and do not
acquire guessed phrase semantics.

\`conditional_goal_policy\` consumes the existing SCR translation instead
of guessing how to create a directory with a separate word list; global
negative/no-write constraints remain authoritative. The directory target
binding consumes the original user-request path literal and current SPR
preflight. \`directory_goal_contract.py\` binds an exact source-spelled,
unique creation target, never an inferred parent, sibling or alternate
workspace. The existing \`target_path_contract\` remains responsible for
deployment-specific verification and is not incorrectly reused here.

## Common Blackboard causal evidence

The read projection \`SMARTAGENT_SHARED_BLACKBOARD_V1\` contains:

- \`sections.goal.value.semantic_action_translation\`
- \`sections.goal.value.directory_target_binding\`
- \`sections.strategy.value.directory_decision_rule\`
- \`sections.strategy.value.candidate_registry\`
- \`sections.actions.value.action_loop_decision\`
- \`sections.progress.value.action_loop_state\`
- \`event_projection\` entry \`ACTION_LOOP_DECISION_EVALUATED\` with
  source \`ACTION_LOOP_STATE\`, \`state_id\`, \`rule_id\`, exact Action
  evidence ID, transition, allowed tool names, and reason code.

The exported \`read_action_loop_blackboard\` validates request/epoch and
Action Loop state ID before sharing decision data with another module.
\`WebAgentProtocolLoop._normalize_action_loop_decision\` reads the Blackboard snapshot. A legitimate accepted Progress update can
advance the Runtime-owned Action Loop after the Planner prompt was rendered.
The derived Blackboard is refreshed from current authoritative Ledgers and
`action_loop_blackboard_snapshot_reconciled` records old/new State IDs.
A stale derived projection alone never rejects an Action; the latest
Runtime-owned Action Loop still decides allowed tools, and the existing
Schema/Scope/Write Ceiling gates remain independent. A failed projection
refresh is diagnosed, never silently promoted to permission.

The durable Action/Result and authoritative Scope Ledgers remain the
sources of truth; Blackboard is a versioned read snapshot, not a new
writable event store. Nothing in SCR/Blackboard grants permissions.

\`collect.bat\` preserves sanitized
\`action_loop_decision_summary.json\` with the same correlation fields,
without exporting raw commands, target paths or browser DOM.

## Deterministic two-decision path

1. Original Goal “創立這個資料夾” with one exact path yields
   SCR \`directory.create\` and a bound \`directory_goal_contract\`.
2. Native \`inspect_directory\` returns independently confirmed
   \`DIRECTORY_ABSENCE_OBSERVED\` (not a failed Action).
3. \`GoalGapLedger\` links \`directory.exists=false\` to the original
   positive creation Goal.
4. \`DIR-001\` derives a \`GOAL_DIRECTORY_CREATE\` Candidate, using a
   non-executing Runtime Scope Preflight. No write permission is inferred.
5. Action Loop sees the **same** DIR-001 candidate/evidence and allows
   \`create_directory\` instead of incorrectly rejecting it as missing
   a prerequisite. Unresolved pending/verification/recovery remains blocked.
6. The Planner's next concrete native Action still passes Tool Envelope,
   SCR selection (including exact original-Goal attestation when applicable),
   candidate binding, request Scope/ACL/Write Ceiling and Tool Admission.
7. Native \`Path.mkdir(parents=False,exist_ok=False)\` creates **only** the
   exact admitted directory. Native stat and typed Tool Evidence confirm
   \`DIRECTORY_CREATED\`; GoalGapLedger now projects \`directory.exists=true\`.
   No shell CWD, parent directory creation, overwrite or source download.

## Regression and constraints

\`test_semantic_blackboard_directory_e2e.py\` starts from the original
Traditional Chinese Goal, executes the actual \`inspect_directory\` and
\`create_directory\` primitives against a temporary writable directory,
and walks SCR, target binding, GoalGap, DIR-001, Action Loop, candidate and
Blackboard. Also tests read-only intent, ambiguous/sibling path rejection,
SCR selection isolation and stale Blackboard rejection.

\`test_git27_collector_causal_coverage.py\` checks the new sanitized causal
decision output. CI also executes existing SCR capability/protocol tests,
Action Loop regression, Git24 canonical contract, and the earlier
Blackboard, Scope, Case D and Windows-native preexecution tests.

These are software/CI regressions, **not** evidence that the real user's
\`E:\` drive has been changed, or that actual Web UI/Telegram delivery
completed. Real Live verification is required. Unknown/denied request
scope must remain a reported blocker, not a reason to infer permission.
