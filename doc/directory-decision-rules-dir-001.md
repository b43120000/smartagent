# Directory Decision Rules — DIR-001 (2026-11-07)

Source of truth: \`source/agent_core/decision_rules/\`.

This is a **deterministic, evidence-driven policy** for directory existence, not a
shell-script template. Additional policy rules should be added to this directory
with their own rule ID, precondition ledger references, and regression cases.

## DIR-001: two-decision lifecycle

| Round | Confirmed Runtime state | Deterministic next decision |
|---|---|---|
| 1 | Unknown/not observed | \`INSPECT_DIRECTORY_REQUIRED\`: probe exactly the bound Goal target |
| 2 | Exists and confirmed | \`ENTER_EXISTING_DIRECTORY\`: use the same original target for actual Goal work |
| 2 | Absent, confirmed, explicit creation/remediation Goal | \`CREATE_DIRECTORY_REQUIRED\`: select native \`create_directory\`, exactly the observed absent target |
| 2 | Absent but read-only Goal | \`READ_ONLY\`: report absence; never mutate |
| 2 | Absent but current write authorization denied | Candidate \`NEEDS_USER_AUTHORIZATION\`; cite the actual Runtime denial |
| Any | Unknown/failed/denied probe | Do not invent an absent fact or perform creation |

### Tool interface

\`create_directory\` accepts an exact \`path\`, and an optional Runtime-issued
\`strategy_id\` as correlation metadata. The implementation uses
\`Path.mkdir(parents=False, exist_ok=False)\` (never shell execution).
Runtime must enforce all of the following **before** creating anything:

- Path must be an absolute exact Goal target within configured writable roots.
- When the Task has explicitly bound request paths, the target must be
  exactly one of those paths — not a parent or sibling.
- Immediate parent must already exist; no recursive parent creation.
- No symlink, reparse, device, UNC, traversal, wildcard or unauthorized path.
- No write-ceiling, scoped ACL or immutable original-Goal constraint bypass.
- Existing files/directories are never overwritten, deleted or moved.
- A post-creation native stat verifies the actual directory. Success has
  \`DIRECTORY_CREATED\` and typed Runtime Evidence, not a Planner assertion.
- A race, missing parent, denied permission or uncertain effect has an
  explicit non-success reason and does not become a completion claim.

The native create receipt updates \`GoalGapLedger\` as a confirmed
\`directory.exists=true\` fact for the same Request/Task-Epoch. It does not
claim that any downloaded source or file contents are verified, nor that
the **whole Goal** has completed.

### Planner/Runtime shared contract

\`planner_context_snapshot.directory_decision_rule\` and
\`shared_blackboard.sections.strategy.data.directory_decision_rule\` are
projections of exactly the same Runtime evidence. For a missing directory,
\`strategy_candidates\` contains \`GOAL_DIRECTORY_CREATE\` with its exact
\`strategy_id\`. The model must use the typed action:

\`\`\`json
{"tool":"create_directory","action_id":"ACT-NEXT-001","strategy_id":"STRAT-<Runtime-issued-ID>","path":"<exact original Goal target>"}
\`\`\`

Candidate availability does **not** grant permission. Existing Tool
Envelope, Scope, Write Ceiling, Ledger, Action Loop and Terminal Admission
remain the final authorities. No operation should target a missing directory
as a working directory.

### Motivation / latest Live evidence

- Installed revision \`e1c210362a24cc9b79fb19311d990d9e9dd3174f\`.
- Latest task \`TASK-68D8CE67F28375EBB03F\` ended before Round 1 due to
  \`WEB_COMPOSER_UNCONFIRMED / TURN_CHANGED_OR_SUBMIT_UNCERTAIN\`.
- Therefore that Live log cannot validate the previously fixed
  \`ACCESS_VERIFIED -> source acquisition\` path.
- DIR-001 addresses a different, general short-path policy: after one trusted
  exact directory observation, choose \`ENTER\` or \`CREATE\` in the following
  Planner response, rather than spending additional turns reasoning about
  nonexistent parent CWD or ungrounded source directories.

The CI test uses a real temporary directory and the actual canonical native
tool to confirm creation, subsequent fact classification and no duplicate work.
