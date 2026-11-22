# Git38 — Generic Enum Contract Repair / PokeClaw Executor Integrity

Based on Git37 commit 733b546773a02bf7f12f02392be66aeafa83008c.

## Motivation

Case D on Git37 stopped on the third
\`run_command_operation_invalid\` even after prior Tool Evidence. The
\`command_operation.normalize_command_operation\` runtime authority contained
the legal values and the actual invalid value in its diagnostic, but Git37
Admission projected only a generic reason and an empty field_issues list.
Git37 also keyed its repair budget solely on tool, reason and issue fields,
mistaking different commands for repeated attempts at the same Action.

The fix must not add a Git workflow, Goal Driver, Prerequisite machinery,
semantic command rewrites, or automatic authorization.

## One generic loop

- The Tool Catalog exposes \`allowed_values\` directly from
  \`agent_core.command_operation.COMMAND_OPERATIONS\`, through the small
  shared \`enum_constraints\` helper. The same metadata source is used by
  Admission, so Enum descriptions cannot silently drift from validation.
- On a known invalid Enum or operation mismatch, Admission returns a
  sanitized typed \`field_issues\` entry with \`field\`, \`actual_value\`,
  \`allowed_values\`, and a bounded \`constraint_code\` when applicable.
  Original command and arbitrary error detail never survive in the receipt.
- The *model* chooses the revised command/operation/verification strategy.
  Runtime does not auto-correct a Git or Python command, guess an intent, or
  grant network, Workspace, ACL, or Write Ceiling access.
- The Executor counts the same tool + exact command + error + field problem
  independently for up to 3 attempts. A separate budget of 10 consecutive
  Admission rejections prevents endlessly changing the command to bypass the
  budget. The per-command budget resets after an Action is freshly admitted;
  ToolResult still decides actual progress.
- The Blackboard now uses \`ADMISSION_REJECTED\` for unexecuted requests,
  without \`toolResult\` or valid \`evidence_ref\`. \`TOOL_RESULT\` is reserved
  for actions that actually reached dispatch; a failed *executed* tool remains
  an actual ToolResult with a typed failure.
- The Runtime Finish Comparator ignores Admission rejections for actual
  Tool Evidence coverage, but cannot accept a Finish with zero tool evidence
  and outstanding rejected requests. A later successfully dispatched ToolCall
  can still support Finish after a previous rejection.
- RCA collector supports both the Git38 admission-event shape and the old
  Git37 rejected \`TOOL_RESULT\` shape for historical logs. It exports only
  sanitized typed enum data and counts the rejections against the task-bound
  Blackboard terminal.

## Case D expected behavior

1. Observe target directory (typed ToolResult).
2. Model chooses the next action.
3. If it proposes \`operation="GIT_CLONE"\`, Runtime rejects with
   \`field="operation"\`, \`actual_value="GIT_CLONE"\`, and the canonical Enum
   values; \`executed=false\` and *no* ToolResult.
4. Model may choose \`GIT_MUTATE\` for an actual Git clone with declared
   success criteria and structured \`verify\`. The same original command must
   undergo **fresh** command classification, filesystem authorization, Git
   access and network policy. A correct Enum is not permission to execute.
5. A genuinely dispatched command produces a ToolResult and actual evidence.
   Model chooses subsequent checkout/sparse copy/checks; Runtime never
   preplans them or claims deployment from a single successful command.
6. Model creates a dynamic Finish only after independent source commit,
   deployment contents and SHA256 checks. Runtime compares Finish with real
   ToolResults and the original request.

This is a contract/feedback fix for any registered tool, not a Case D path
shortcut. Git clone and private repository access still require Live proof.

## Regression requirements

- Dynamic Enum values exactly match canonical Runtime values.
- Invalid/hostile enum values never leak raw command or credential payload.
- Operation mismatch gets safe causal explanation, not raw command.
- Invalid → corrected → dispatched ToolCall → evidence-backed Finish PASS.
- Different commands with the same error do not share the three-attempt budget.
- Same command three times, or ten consecutive different invalid proposals,
  stop boundedly without dispatching anything.
- Rejected-only Finish cannot pass as pure text or external execution.
- Windows Collector exports separate Admission events, preserves old Git37
  records, and marks mismatched counts as PARTIAL.
- Existing Git26–37 and Tri-One regressions remain intact.
