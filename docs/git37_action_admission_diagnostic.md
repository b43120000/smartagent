# Git37 — Typed Action Admission Diagnostic & Evidence-Guided Schema Repair

Parent: Git36 commit 5bcca2f3743cec13d97af7c4db079e08b61da7b7.

## Root cause / Case D

Case D first ran filesystem checks but no Git command executed:
- git ls-remote was rejected as command_not_proven_read_only;
- four git clone proposals were rejected as wrong_field_type;
- the Executor persisted these rejection events under
  localdata/runtime/executor_run_blackboard/<TASK>.json;
- the legacy RCA collector only joined JSONL protocol_rejected and
  minimal_goal_action_rejected, so its exported admission rejection list
  was empty even though the Executor had rejection events.

This is an Action Admission contract/feedback gap and RCA provenance gap,
not evidence of git.exe crashing or a Tri-One state mismatch.

## Changes

1. The existing validate_tool_envelope is the single field-type and required
   field authority. Minimal Admission now retains bounded, allowlisted
   field_issues on rejection (field, expected_type, actual_type, reason).
   Raw commands, paths, credentials and actual argument values are not
   included in the rejection diagnostic. Safety and scope checks remain.
2. The tool catalog now describes actual schema-declared required and optional
   types for every tool and includes run_command verify object examples.
   String verify is invalid; it must be a list/dict of verification steps.
3. The PokeClaw Executor persists admission_diagnostic, attempt number and
   proposal hash alongside the rejection in its durable Blackboard.
   The next model turn sees these exact fields and can correct its
   *declaration*. It cannot edit past evidence or override authorization.
4. Consecutive proposals with the same tool/rejection/typed issues are
   capped at three across differing command strings. Attempt counts are
   recorded; the third identical issue stops as BLOCKED. A valid proposed
   Action goes through fresh full Admission before dispatch.
5. collect.bat loads the exact task/request/epoch-bound atomic
   Executor Blackboard, projects only sanitized rejected Action metadata,
   writes executor_admission_summary.json and merges records into
   action_admission_rejections.json. It compares the count against the
   Blackboard terminal and labels the RCA PARTIAL if they differ.
   Unknown/foreign task records are never silently merged.

## Security constraints

- No automatic rewriting of Git operations, verification predicates, or
  success criteria. Read-only classification is a positive grammar.
- A git clone is a mutation; it still requires success_criteria,
  structured verify, network authorization, scope and write ceiling.
- No tool executions are replayed after an error. Admission rejection
  remains executed=false, with no tool evidence_ref.
- The new diagnostic metadata never grants authorization or Goal PASS.
- Collector excludes raw command strings, secrets, and stdout/stderr.

## Tests

Windows/Ubuntu new tests validate typed wrong-field, missing-field,
read-only command rejection, policy denial, repeat repair budget, action
prompt/correlation, and no clone dispatch. Windows Collector tests execute
real collect.bat against a synthetic Blackboard with five rejected actions,
a mismatched terminal count and a foreign request ID. Git26/Git27 and
Git33-Git36 regressions run on both OS targets.

## Live Case D retest expectations

1. A malformed git clone action must produce actionable
   field_issues and a rejection with executed=false.
2. On repair, verify must be an object/list and success_criteria a string.
3. Only a separately admitted git command may run; denied remote/network
   access must still cause BLOCKED, not a fabricated deployment.
4. RCA must show actual Executor rejection count, and Collector must
   never claim FULL coverage with a count mismatch.
5. If a permitted Git command actually runs, capture actual exit code
   and independent deployment postcondition observations before Finish.

CI does not attest to access to the user's private Windows E: drive or
credentialed Git clone. A further Windows Live Case D is required.
