# Git42 — Typed Finish Schema Repair and bounded repair-only grace

Base: Git41 commit `8509979aa1d5222eaa5ec3995d6d7a82da2b5d6c`. RCA: `TASK-5877389CFC02EE8D0232` (2026-11-09).

## Reproduced symptom

- Round 28: `MODEL_DECISION_REJECTED / unknown_decision_type`.
- Round 29: `MODEL_FINISH_SCHEMA_REPAIR / MODEL_GOAL_CHECK_SCHEMA_INVALID`.
- Round 30: second identical Finish repair, followed by `MAX_ROUNDS`.
- Previous Runtime gave only a generic reason code, not the failing check index or field.

## P0 — comparator diagnostics

`compare_runtime_finish` continues to be fail-closed and does **not** relax the success criterion. On structural failure of model-authored `goal_checks`, it additionally attaches a sanitized `finish_diagnostic` object: `schema`, `check_index` (zero-based), `field`, `constraint_code`, `expected_type`, `actual_type`, `allowed_values`, `evidence_unmodified=true`, `authorization_granted=false`.

Possible codes include `CHECK_KEYS_MISMATCH`, `DUPLICATED_CRITERION`, `EVIDENCE_REF_NOT_IN_RUNTIME_CLAIMS`, `FIELD_SYNTAX_NOT_DATA_SCALAR`, `FIELD_NOT_DECLARED_IN_EXPECTED_RUNTIME`, `INVALID_OPERATOR`, `EXPECTED_NOT_SCALAR`. The module never echoes user-supplied criterion text, expected values, commands or raw ToolResults.

## P1 — Planner repair feedback

`MODEL_FINISH_SCHEMA_REPAIR` Blackboard events retain the typed diagnostic and attempt count. The next Planner prompt gets the exact safe diagnostic with instructions to repair only the offending check and to refer to the real field catalog. No evidence or Action proposal is rewritten by Runtime.

## P2 — terminal-turn repair budget

- Ordinary ToolCall budget is unchanged: `min(loop.max_turns, 30)`.
- If a Finish submission is repairable at the end of the ordinary budget, up to **two additional Finish-only rounds** are available, with at most three total schema repair prompts.
- Extra turns are not Action rounds; any ToolCall in those turns is rejected without execution. The model may also return an explicit `blocked` decision.
- If the structural contract is still invalid, the terminal is `FINISH_CONTRACT_INVALID` with `goal_verified=false`, not a misleading generic `MAX_ROUNDS`.
- A valid Finish is still independently verified by the usual Runtime Comparator; no synthetic PASS.

## P3 — RCA exports

`collect.bat` additionally writes `executor_finish_diagnostics.json` with request- and task-scoped `MODEL_DECISION_REJECTED`, `MODEL_FINISH_SCHEMA_REPAIR` and `MODEL_FINISH_CONTRACT_INVALID` events, including individual constraint codes, index, repair attempts and terminal kind. Raw model replies are not included.

## Testing

A Windows/Linux GitHub Actions workflow compiles the touched modules and runs Git42 targeted tests plus Git33–41 regression suites. On-device Telegram, WebCopilot and Case D deployment remain separate Live acceptance tests.
