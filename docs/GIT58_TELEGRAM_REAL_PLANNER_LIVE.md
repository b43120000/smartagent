# Git58 — one-message Telegram real GPT Planner Semantic Repair E2E

Based on Git57, without enabling a production-wide injector or changing ACL/Turbo.

## Deploy locally, once

1. Stop the existing RemoteAgent/Telegram Listener and all active workers using the usual application stop procedure. Do not switch Git trees while the old Python processes are running.
2. From the SmartAgent installation root (example C:\smartagent), save any local Git changes, and switch to the tested Git58 commit.

~~~powershell
cd C:\smartagent
git status --short
git fetch origin
git switch --detach origin/git58/telegram-real-planner-live-20261113
$env:PYTHONPATH='source'
python -c "from agent_core.protocol_manifest import write_protocol_manifest,require_protocol_manifest; p=write_protocol_manifest('.'); print(p); print(require_protocol_manifest('.') .get('hash_verification'))"
~~~

3. Start RemoteAgent/Telegram using the normal installed launcher. Restart is mandatory to load the new Worker and receiver Python source. Use the existing configured authenticated private Telegram chat.

Note: manifest regeneration updates Runtime-owned checksum metadata under localdata, not the tested folder or ACL/Turbo. It is necessary because telegram_transport.py and telegram_webagent_worker.py belong to the fail-closed source protocol manifest.

## Exactly one Telegram message

~~~text
/git58_live_repair
~~~

The receiver checks the private sender's existing authorization, then creates a fresh, single-directory, read-only original request:

~~~text
這個資料夾是否存在？

<configured Telegram workspace>\GIT58_LIVE_NONEXISTENT_<Telegram update ID>
~~~

It carries one trusted request-bound test selector in ingress metadata, not in the GPT prompt or the Frozen Goal Contract. The directory must be absent; the test does not create it.

During the ONE task, the RemoteAgent worker wraps the actual `live_planner(client, prompt, expected, attachments)` function. The Git57 injector changes only the first valid Finish's observed-exists expected_runtime claim after the real inspect_directory result has been persisted. On the retry, the raw real GPT response is used without reinjection. The injector is discarded at the end of the task.

## Expected Runtime evidence

- One actual `inspect_directory` ToolResult, `observed_exists=false`, `observation_status=CONFIRMED`.
- One `GIT57_TEST_FINISH_INJECTION` event (actual false -> declared true).
- One `MODEL_FINISH_SEMANTIC_REPAIR` event, `MODEL_RUNTIME_MISMATCH`.
- Real GPT corrected Finish without a second ToolCall.
- Frozen Goal Contract `DIRECTORY_OBSERVATION`, terminal `FROZEN_GOAL_STATE_SATISFIED`, Runtime PASS.
- Worker appends `GIT58_REAL_PLANNER_LIVE_PASS` and a JSON receipt with task_id, request_id, evidence_ref and event counts to the same Telegram reply.

On missing injection, unexpected model output, an extra ToolResult, mismatched evidence or failed comparator: never call it a real Planner Live PASS. The Worker raises an explicit `GIT58_REAL_PLANNER_LIVE_FAIL` and returns failure through the existing Telegram task delivery path. Consult Blackboard `localdata/runtime/executor_run_blackboard/<TASK-ID>.json` for full events.

**Boundary:** Git58 GitHub Actions verifies Receiver/Worker source, binding, guard tests and real filesystem Runtime calls with a deterministic test Planner. It cannot exercise your locally authenticated Telegram/CDP GPT session. Only a real `/git58_live_repair` result from your running local RemoteAgent can establish true GPT Planner Live PASS.
