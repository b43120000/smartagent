# Git57 Windows Runtime Live E2E: Finish Semantic Repair

Based on Git56. Test-only deterministic Planner; actual WebAgentProtocolLoop, WebAgentToolContext, Runtime Evidence Store and Executor Blackboard. No host CMD fallback and no ACL/Turbo/PATH changes. This does not test DOM/Telegram model transport or native Git/Python executable attestation.

## Prerequisites

- Run from the SmartAgent repository root using its existing Python environment.
- Workspace is an existing authorized Windows directory (example E:\AIspace).
- Target must be absent, inside workspace, under an existing parent, and leaf must start with GIT57_LIVE_.
- The requested target is never written. Runtime test Evidence is written to localdata/runtime under --progress-root.

## Scenario A: one wrong Finish, corrected, Runtime PASS

~~~powershell
$env:PYTHONPATH = 'source'
python -B source/WebAgent/tests/git57_live_e2e.py --workspace 'E:\AIspace' --target 'E:\AIspace\GIT57_LIVE_NONEXISTENT_20261113_RECOVER' --progress-root '.' --scenario recover
~~~

Expected: goal_contract_status=COMPILED, tool_execution_count=1, injection_count=1, semantic_repair_count=1, finish_attempt_count=2, evidence_unchanged=true, goal_contract_unchanged=true, final_verdict=PASS, comparator_cause=FROZEN_GOAL_STATE_SATISFIED, verdict=GIT57_LIVE_E2E_PASS.

## Scenario B: repeated incorrect Finish, bounded rejection

~~~powershell
python -B source/WebAgent/tests/git57_live_e2e.py --workspace 'E:\AIspace' --target 'E:\AIspace\GIT57_LIVE_NONEXISTENT_20261113_EXHAUST' --progress-root '.' --scenario exhaust
~~~

Expected: tool_execution_count=1, injection_count=1, semantic_repair_count=2, finish_attempt_count=3, executor_state=FINISH_CONTRACT_INVALID, final_verdict=NOT_ADMITTED, verdict=GIT57_LIVE_E2E_PASS (negative test passed, not user task success).

Every test writes a task-bound Event Sequence and reports Evidence SHA256 and Goal Contract SHA256. Any missing target parent, denied scope, missing evidence, re-execution or wrong terminal classification fails closed. Never claim the operator's machine passed from CI evidence alone.

Git57 Windows GitHub Actions exercises the real local filesystem and real Runtime action dispatch under a deterministic test Planner; it does not independently verify Telegram, WebCopilot DOM or live external-model roundtrip. For external-model verification, wrap the actual test Planner callable in an isolated host; do not enable any production injection switch.
