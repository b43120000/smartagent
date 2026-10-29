# GAR Shared Runtime — candidate implementation handoff

Status: SOURCE CANDIDATE; UT/IT NOT YET VERIFIED; Live NOT RUN; NOT FORMAL PASS.
Branch: feat/gar-shared-runtime-20261029
Base: Goal1 091568ed2bbdef3025945f495d0cb5f07f8df49d
Feature switch: SMARTAGENT_GAR_V1=1, default OFF. Existing TRI_ONE_* flags are independent.

## Observed interruption and root boundary

Observed on 2026-10-29: WEB_PROTOCOL_RECOVERY_STALE_READBACK, reason=response_sha_unchanged, round=4, attempt=2, action_executed=false, INTERRUPTED.

Source/agent_core/web_runtime.py intentionally rejects a byte-identical recovery readback: it might be a stale DOM turn or an independently generated but identical answer. It does not prove freshness. Existing recovery uses a bounded single probe and marks safe_to_retry=False. GAR must never repeat the original request or treat the old response as a fresh ACK without new evidence.

## Implemented candidate

L0: The shared Runtime receives a stale readback failure, records an identity-bound, redacted recovery checkpoint, and marks the task PAUSED without sending an additional model request. This is safe interruption, NOT a repair of the underlying browser/composer issue.

L1: Existing Protocol V9 minimal schema and Progress repair remain authoritative; no GAR exception can bypass admission.

L2: For repeated, genuinely fresh protocol rejections with no pending unsafe operation, inject an immutable original-Goal Progress Rebase proposal into the same Planner conversation, then run through normal V9 admission.

L3: A later repeated blocker permits one bounded alternative plan; a mutation semantically identical to an already executed/unknown action is blocked before execution even if the action_id is changed.

L4: No independently attested fresh-context adapter is present. The engine must fail closed with GAR_FRESH_CONTEXT_UNAVAILABLE and may not invent a new ChatGPT page, reset ACKs, or let a new Worker claim old actions.

L5: PAUSED/UNKNOWN and incident reason, no accepted terminal SUCCESS. A minimal redacted checkpoint is persisted under localdata/runtime/gar_snapshots, with digests but no raw original request, tool results, paths, or secrets.

The shared engine is source/agent_core/goal_anchored_recovery.py. V9 integration is source/WebAgent/protocol_loop.py, used by WebDirect and RemoteAgent and by WebCopilot when the shared V9 worker is enabled. The Goal1 base defaults WEBGPT_COPILOT to shared V9. A deliberate TRI_ONE_WEBCOPILOT_V9_V1=0 opts out into legacy smart_agent.py, outside this GAR integration. LocalAgent complete convergence requires the separate 2B/2C architecture.

## Offline tests to execute and record for an exact frozen SHA

- source/WebAgent/tests/validate_gar_shared_runtime.py
- source/WebAgent/tests/validate_gar_protocol_loop_it.py
- source/WebAgent/tests/validate_protocol_recovery_readback.py
- source/WebAgent/tests/validate_tri_one_2a_task_result.py
- source/WebAgent/tests/validate_tri_one_2a_matrix.py
- source/WebAgent/tests/validate_tri_one_5a_answer_only.py
- source/WebAgent/tests/validate_tri_one_5b_shared_worker.py
- source/agent_core/protocol_manifest.py require_protocol_manifest validation

Windows CI definition: .github/workflows/gar-runtime-offline.yml. A workflow file is NOT test evidence. Preserve real exit codes, source SHA, stdout/stderr hashes, and immutable attempts.

## Gates remaining before complete GAR acceptance

1. Source candidate is not PASS until real offline UT/IT and regression evidence are recorded.
2. True fresh-context L4 needs a supported, verified adapter with session fencing, original task owner/route, snapshot integrity, and no duplicate side effects. Unsupported means explicit PAUSED; no fake success.
3. The DOM/Composer stale-readback root cause is a distinct transport issue; this implementation does not remove it.
4. Cross request/task/epoch continuation budget persistence and ownership need separate verified migration, not an unguarded automatic resume.
5. Real WebDirect/WebCopilot and RemoteAgent E2E require verified loaded Source SHA and actual original-interface delivery. No new Live was run here.

Minimal user-side report, without raw logs: case; original request; actual ingress (WebDirect/WebCopilot); interruption stage; reason code; action_executed; final observed state; whether resubmitted; loaded Worker SHA if attested. Existing sanitized helper: source/WebAgent/tests/goal1_live_triage.py.

No main merge, no force-push, no active Worker replacement, no old task replay.
Reference: doc/smartagent_goal_anchored_recovery_design.md.
