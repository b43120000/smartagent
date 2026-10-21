# Action-scoped admission and recovery

Execution package change, 2026-10-13. Shared WebAgent protocol loop is used by RemoteAgent and WebCopilot.

## Contract

- Transport JSON, commit/count, unique IDs, one Progress, terminal exclusivity and enabled round budgets remain batch barriers.
- After those barriers, independent valid actions are admitted. Invalid operational actions are reported separately as `REPAIR_REQUIRED`; successful siblings are not retained for replay.
- `depends_on` is optional structured data: up to 32 unique same-request action IDs. References must be earlier in the batch or in the current request ledger. Forward/self references are rejected. Dependency satisfaction is checked immediately before invocation, against active, identity-matched, successful typed results; stale, failed, rejected, unknown, queued, running or unverified results cannot authorize a dependent.
- A failed executed action is immutable. Remedy it using a new action ID and update the waiting dependent's references. A never-executed action may be corrected; schema replanning using a fresh ID can specify `replaces_action_id` for an existing deferred action only.
- Native source, edit-plan, file transaction, verification and path-security checks remain mandatory when dependencies are omitted.
- Non-observational actions are rechecked at execution time, after earlier results. Deterministic file mutations can be considered independent only when their resource paths are provably disjoint from every active blocker. Opaque commands cannot prove this.
- A command's `operation=INSPECT` is not read-only proof. Only a small anchored positive grammar gets observation bypass; other commands use normal conservative admission. UNKNOWN side effects require reconciliation, not automatic retry.
- Admission and dependency blocks are `NOT_EXECUTED`, not synthetic successful tool evidence. They are represented separately in Runtime action states and blockers, and cannot prove terminal success. Terminal normalization cannot discard unresolved actions.
- All request-local repair maps are reset for a new request. Audit events retain original count, admitted IDs, deferred action payloads and diagnostics. This does not add durable process-crash resume of pending operations.

## Entry points

- `agent_core/action_dependencies.py`: dependency references, authoritative predecessor outcomes, positive observational command grammar.
- `agent_core/smartagent_protocol.py`, `protocol_v8.py`: schema vs transport separation; malformed JSON/identity/commit still reject globally.
- `WebAgent/protocol_loop.py`: partial admission via normal strict validation, pending repairs, execution-time checks, replay caching and terminal gates.
- `agent_core/action_loop_state.py`: action-scoped admission blockers distinct from tool-result evidence.
- `WebAgent/protocol.py`, `agent_core/tool_capabilities.py`: model-facing fields and repair instructions.

## Verification

`python -m WebAgent.tests.validate_action_scoped_admission`

Fixed local planner replies exercise the real protocol loop and temporary filesystem tools. Cases cover malformed sibling + valid read, correction without sibling replay, missing-file predecessor + blocked dependent + successful independent read, new prerequisite resolution, same-round UNKNOWN command with overlapping write blocked but independent read executed, commit/ID/budget rejection, cross-request/stale dependency rejection, fake INSPECT mutations, and normalized terminal rejection.

These are deterministic local integration/UT checks, not live browser or Telegram IT. A live model may return one action per reply; this change does not guarantee it will emit every independent action in one round. If read-only command effects or arbitrary mutation independence cannot be proven, the Runtime remains conservative.
