# Tri-One Phase 1 — Implementation Ownership & Codex Handoff

> **PREPARATION ONLY.** This is an execution runbook to use **after the user explicitly starts** the next development task. Creating this plan does not authorize code changes, runtime restarts, task replay, Live dispatch, deployment, or new test fixtures.

Parent documents:
- [Architecture and milestones](tri_one_phase1_shared_completion_text_only_fast_path_plan.md)
- [Detailed UT / IT / Live matrix](tri_one_phase1_verification_matrix.md)

## 1. Planning baseline & intended branches

- Repository: `b43120000/smartagent`
- Source baseline when plan was written: `main@b95be77876ac522248954f3127d4fb7b1024eed2`
- Planning-only branch: `docs/tri-one-phase1-plan-20261026`.
- Expected implementation branches (suggestions, not yet created):
  - `feat/tri-one-p1-2a-shared-task-result`
  - `feat/tri-one-p1-5a-text-only-terminal`
  - `feat/tri-one-p1-5b-cross-interface-delivery`
- A stage branch begins from the **accepted** preceding stage; no unreviewed main merges into active testing. All stage testing records source SHA and loaded Worker SHA separately.
- Private evidence repository: `b43120000/test` — create a **new** namespace `tasks/TRI-ONE-P1-20261026/` after user authorizes execution. Existing historical evidence is read-only.
- Windows candidate checkout: user environment historically `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`; read actual current checkout and working tree before assuming this location or changing anything. User may designate an isolated worktree.

## 2. Separation of responsibility

| Responsibility | ChatGPT | Codex |
|---|---|---|
| Inspect / perform source RCA and plan change | **Owner** | Gather requested local facts only |
| Decide semantics and compatibility of contract | **Owner** | No independent specification changes |
| Edit and commit source to remote GitHub | **Owner** after user authorizes stage | Do not independently patch source |
| Prepare exact UT/IT/Live run instructions and candidate SHA | **Owner** | Follow exact runbook |
| Deploy candidate in authorized isolated local environment | Review readiness and pin | **Owner** |
| Run UT/IT and safe Live | Review test definitions | **Owner** and collect unmodified evidence |
| Evaluate results and decide PASS/FAIL | **Owner**, evidence-backed | Report facts / statuses only |
| Rework fixes and open stage PR | **Owner** | No opportunistic fixes |
| Final acceptance / promotion to main | User approval + ChatGPT review | No independent merge |

If a local test reveals a bug, Codex should stop *only the affected unsafe operation*, preserve evidence, send ChatGPT a factual failure report and wait for a new pinned Git commit/runbook. Unrelated safe cases can continue where the test plan permits.

## 3. Milestone dispatch protocol (per stage)

### Step A — ChatGPT: scope and source preflight
1. Fetch `main` and planning document SHA; confirm changes since baseline.
2. Read actual current source interfaces and persistent schema. Finalize contracts (do not silently rename production fields).
3. Publish **stage-specific change design**: list of source paths, compatibility invariants, rollback flag, exact tests and gates.
4. Create and commit implementation to an isolated branch. Record immutable candidate SHA and manifest/hash plan. Do not merge to main until accepted.

### Step B — Codex: local evidence-only preflight
1. Confirm repository remote URL, intended workspace and branch, HEAD, clean tracked state, conflict/stash/untracked status, and official Manifest.
2. Check isolated fixture and that no protected historical tasks or OPEN Writer are being manipulated.
3. Identify active Supervisor/Worker, queue/Outbox status and test Browser/Conversation binding. Use the established approved instance, not arbitrary new Chrome/Edge profiles; no `edge5`.
4. For every interface in current stage, attest availability and ingress authority. Do not treat Telegram simulation as actual Telegram delivery.
5. If any essential gate fails, produce `BLOCKED_<precise_reason>`, no dispatch. Record exactly which gates passed.

### Step C — Codex: UT
- Deploy a pinned candidate into the approved isolated checkout using only authorized, reversible operations and no `reset --hard` of user work.
- Run stage-scoped UT and the required prior contracts.
- Store commands (including working directory and environment), exit codes, standard output/error, elapsed time, file hashes and failures.
- NO model/browser invocation for UT.

### Step D — Codex: IT
- Use deterministic fake Planner and isolated state. Do not patch production file data or import a live PAUSED task.
- Exercise actual parser, Progress, Action/Condition Ledger, event/queue/Terminal/Delivery boundaries.
- Complete independently safe cases even after one FAIL; do not run a dependent case if its prerequisite gate failed.
- Capture fresh request/task/epoch, immutable raw outputs, expected/actual, recovery outcomes, pass/fail per case.

### Step E — Codex: Live (only on separately authorized instance)
- Attest actual loaded Worker source (not just repository HEAD), required Manifest, browser ownership, target UI idle, correct bound conversation, fixture, active task state, login and transport route.
- Use one unique new natural-language input per case; preserve raw user body and its hash.
- Capture every Planner round, valid/invalid `smartagent_tool`, V9 `turn_commit` and ACK, actions/events, terminal receipt, delivery receipt.
- For negative tests, prefer read-only fixture / safe refusal. Do not invoke dangerous real mutations to manufacture failures.
- If original request would require real external live knowledge and tool capability is absent, success is **proper blocked/explicit uncertainty behavior**, NOT claimed live factual answer.
- Do not send the same request twice due to timeout. Sender timeout alone proves neither Task FAIL nor Task SUCCESS.

### Step F — ChatGPT: acceptance
- Read **all** submitted evidence and hashes, compare with pinned expected contract and test matrix.
- Produce formal verdict for EACH attempted case and overall gate: `PASS / FAIL / BLOCKED / NOT_RUN`; distinguish expected negative case success from task outcome.
- If FAIL, identify likely RCA, commit minimal fix, and issue pinned rerun scope. Preserve prior failed attempts and source identities.
- If all required gates pass, record stage acceptance and seek user approval before promoting to main / proceeding to next stage.
- Never use passing tests on the previous code SHA to certify new untested code.

## 4. Initial next action — 2A only

When the user returns and authorizes execution, begin with **2A source-interface mapping**. No 5A/5B implementation or Live test until 2A is individually accepted.

Initial read-only mapping inventory for Codex:
1. Source/load IDs: `git rev-parse HEAD`, current checked-out branch/dirty state, Manifest validity, Python environment.
2. Existing queue/task result: `source/agent_core/task_state.py`.
3. Existing events/outbox: `source/agent_core/remote_events.py`, `source/RemoteAgent/telegram_delivery.py`.
4. Worker terminal: `source/RemoteAgent/telegram_webagent_worker.py` around `queue.complete`, `TASK_COMPLETED`, `terminal_acceptance`, `task_outcome`, `render_task_reply`.
5. WebAgent terminal: `source/WebAgent/protocol_loop.py` `_terminal_evidence_verdict`, `_accepted_terminal_receipt`, and `controller.py` final render.
6. Current WebCopilot and LocalAgent ingress/adapter call-paths. If not found, state `NOT_OBSERVED`; do not invent.
7. List exact candidate UT scripts' `__main__` behavior, OS/fixture dependencies and side-effect risks; don't run until approved.
8. Return normalized mapping table `source → canonical field → sink → evidence producer → trust boundary`.

ChatGPT uses this mapping to settle `TaskResultV1` naming and file changes before writing 2A source. The user may provide a separate order authorizing the full staged plan at that point; otherwise stay preflight-only.

## 5. Standard factual report contract

Each Codex round should provide:
~~~text
DELIVERY_ID=TRI-ONE-P1-<STAGE>-<ROUND>-<DATE>
TASK_SCOPE=<2A|5A|5B> ; MODE=<READ_ONLY|UT|IT|LIVE>
SOURCE_COMMIT=<full SHA> ; LOADED_WORKER_SHA=<full SHA or NOT_ATTESTED>
WORKSPACE=<observed path> ; MANIFEST=<PASS|FAIL|NOT_RUN>
NEW_REQUESTS_SUBMITTED=<count>; HISTORICAL_REQUESTS_REPLAYED=0
UT=<passed>/<attempted>; IT=<passed>/<attempted>; LIVE=<passed>/<attempted>
REGRESSION=<passed>/<attempted>
FAILED_CASES=<IDs + objective diagnostics>
BLOCKED_CASES=<IDs + explicit safety reason>
RAW_EVIDENCE=<actual paths and SHA-256>
UNRESOLVED_ACTIONS=<OPEN/QUEUED/RUNNING/UNKNOWN/PAUSED with IDs>
NEXT_DECISION_OWNER=CHATGPT
~~~

An output directory alone is not evidence; include real file size/hash and path, exact test results, and actual submission/receipt IDs. Never claim `PASS` merely because a command exited 0 when the required terminal/delivery evidence is absent.

## 6. Release and rollback

- No source implementation is performed on this planning branch.
- Stage candidate PRs remain separate; merge only accepted staged source, never force-push `main`.
- Keep a disabled path for the new schema mapper and text-only classifier (flag names proposed in design, not existing runtime configuration).
- On incompatibility, disable only the new path and preserve original task/event ledgers and delivered receipts.
- No deployment into a currently active user production session without explicit approval and source attestation.
- No cancellation, replay, abort, commit, resume or cleanup of historical IT19/IT20 protected tasks during Phase 1.

## 7. Phase 1 acceptance checkpoints

- `2A_ACCEPTED`: compatible completion contract and both supported adapter result paths survive UT/IT/Live.
- `5A_ACCEPTED`: FP01/02/03 accepted one-turn, zero operational actions; FP04/05/07 fail-closed; no regression.
- `5B_ACCEPTED`: FP01–FP08 adjudicated per Telegram and WebCopilot original ingress, exactly-once delivery verified, safe FP06 continuation, full mandatory regression.
- `PHASE_1_ACCEPTED`: combined verification on single final source SHA, approved documentation and user acceptance.

Phase 2 (2B and 2C) is **not** implicitly authorized by any Phase 1 acceptance.
