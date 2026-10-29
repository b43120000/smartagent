# GAR Shared Runtime — candidate implementation handoff

Status: L0–L4 SOURCE CANDIDATE; UT/IT NOT YET VERIFIED; Live NOT RUN; NOT FORMAL PASS.
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

L4: Implemented explicit, capability-gated ExistingConversationFreshAdapter in source/WebAgent/fresh_context.py. When an operator has prepared a DIFFERENT existing ChatGPT conversation tab in the SAME BrowserContext and configured SMARTAGENT_GAR_FRESH_CONTEXT_URL, GAR checks the original page lease, source generation idle, distinct target conversation ID/provider, exactly one target tab, no other worker owner, target idle and a matching new SessionProtocol bootstrap. Only then Runtime validates FreshContextProof (same request/task/epoch/owner) and fences the old Planner before using the new page. The original TaskResult, Action Ledger, user scope and original-interface reply destination remain with the original worker. A missing/invalid URL or failed bootstrap safely pauses L5, never creates a tab, resends the original task or accepts an old ACK. This is a SOURCE CANDIDATE pending tests, not verified Live.

L5: PAUSED/UNKNOWN and incident reason, no accepted terminal SUCCESS. A minimal redacted checkpoint is persisted under localdata/runtime/gar_snapshots, with digests but no raw original request, tool results, paths, or secrets.

The shared engine is source/agent_core/goal_anchored_recovery.py; its capability-gated Browser Adapter is source/WebAgent/fresh_context.py; V9 source wiring is in source/WebAgent/protocol_loop.py, WebAgent/controller.py and RemoteAgent/telegram_webagent_worker.py. WebDirect acquires the common execution-page lease for an explicitly configured L4. RemoteAgent already holds that lease. The Goal1 base defaults WEBGPT_COPILOT to shared V9. A deliberate TRI_ONE_WEBCOPILOT_V9_V1=0 opts out into legacy smart_agent.py, outside this GAR integration. LocalAgent full convergence remains the separate 2B/2C architecture.

## RCA: first-round GAR_PENDING_OR_BLOCKED_OPERATION (2026-10-29)

A no-side-effect M365/RAII failure-injection Live produced `GAR safely paused: GAR_PENDING_OR_BLOCKED_OPERATION` at round=1. The redacted checkpoint showed `pending_verification_requirement=UNKNOWN`, `blockers=[]`, and both `rejected_rounds=0` and `local_rejected_total=0`. Source trace: the Runtime creates `pending_verification_requirement` when a V9 `COMPLETE/SUCCESS` is rejected for `terminal_success_verification_missing`. This can happen with **zero executed tools**; GAR had incorrectly classified all such requirements as potentially in-flight operations.

Candidate correction: `is_unverified_nonexecuted_terminal_requirement` checks an exact, restrictive zero-action/no-evidence shape, including no Action/Result/Condition ledger, no evidence refs, no blocking action ID or reason code, `current_verification_status=UNVERIFIED`, and a canonical `run_command VERIFY` prerequisite. Only this specific case becomes an advisory `UNVERIFIED_TERMINAL_NO_EXECUTION` checkpoint precondition rather than an unsafe pending operation. The V9 terminal acceptance and verification requirement remain intact, so this change **does not** accept the original unsupported SUCCESS or execute the suggested command.

Any `OPEN_WRITE_TRANSACTION`, unknown outcome, existing action history, referenced evidence, transport ambiguity, or noncanonical requirement remains a hard L5 safety blocker. The negative and positive regression cases are in `validate_gar_shared_runtime.py` and `validate_gar_protocol_loop_it.py`. No new Live run or offline PASS proof was obtained here.

**Important:** The original deliberate-protocol-violation prompt contains test instructions and may not qualify for 5A Answer-only admission; an unsupported terminal claim is still rejected. This classification fix merely lets legitimate GAR recovery continue rather than confusing that rejected claim with an in-flight side effect.

## Bounded Repair → GAR unified decision ownership (2026-10-29 candidate)

Observed Live failure: after an intentionally malformed V9 test, the original Bounded Repair raised `bounded_repair_exhausted_without_sufficient_new_evidence` before GAR L4 could be attempted. Source RCA: `WebAgentProtocolLoop._admit_bounded_repair` raised on `EXHAUSTED` before its subsequent `_gar_rejection` call. Two repeated-rejection branches could also accidentally count one turn more than once.

With GAR enabled, Bounded Repair is now the local **observation/repair budget**, not an independent terminal decision maker. It reports **one** `gar_local_repair_handoff` on first local exhaustion; GAR owns all subsequent L2/L3/L4 decisions and L5 safe pause. GAR records each rejected Planner turn exactly once and checks exact synchronization against BoundedRepairBudget's `rejected_turns_total`, so a duplicated event or lost counter fails closed with `GAR_LOCAL_BUDGET_DESYNC`.

The original local thresholds remain unchanged: 5 occurrences of the same reason or 6 rejected turns without new evidence (whichever comes first); **global hard limits remain active**. GAR now uses a 10-rejection shared ceiling, bounded further by the original 16-total maximum and existing 600-second soft repair deadline. On identical fresh protocol rejections with no tool effects, the expected staged sequence is four local repairs, then L2 (fifth rejection), L3 (sixth), L4 request (seventh); the eighth and subsequent decisions remain bound by the same counters. L4 does not reset the budget.

When GAR is **OFF**, the original repeated-rejection and Bounded Repair interruption behavior remains unchanged. Transport stale readback and unknown/OPEN action outcomes are separate hard safety gates; they do **not** automatically escalate into L4.

Negative and positive fake-Planner regression cases are in `validate_gar_shared_runtime.py` and `validate_gar_protocol_loop_it.py`. These tests are **committed but execution results are not available**.

## L4 operator configuration (only after safe idle and exact Source checkout)

In the same browser profile as the original WebCopilot conversation, prepare a SECOND existing ChatGPT conversation and open its full `https://chatgpt.com/c/<distinct-id>` URL in a separate tab. Use a conversation you explicitly designate for recovery; do not use a second Agent or another person's page. The original conversation remains the task and delivery destination. No new tab is created automatically.

```powershell
$env:SMARTAGENT_GAR_V1 = "1"
$env:SMARTAGENT_GAR_FRESH_CONTEXT_URL = "https://chatgpt.com/c/REPLACE-WITH-EXISTING-SECOND-CONVERSATION-ID"
# Optional, independent Goal1 flags only if their own acceptance gates permit:
# $env:TRI_ONE_TASK_RESULT_V1 = "1"
# $env:TRI_ONE_TEXT_ONLY_V1 = "1"
```

These are process environment variables to set BEFORE starting a new, idle Worker. No live task may be restarted or replayed merely to apply them. L4 is attempted only after repeated FRESH Planner protocol rejections with a verified safe checkpoint; L0 stale readback still pauses because no independently fresh response was obtained.

## Offline tests to execute and record for an exact frozen SHA

- source/WebAgent/tests/validate_gar_shared_runtime.py
- source/WebAgent/tests/validate_gar_protocol_loop_it.py (including fresh V9 terminal and old Planner fencing)
- source/WebAgent/tests/validate_gar_l4_fresh_context.py (mock BrowserContext, owner, bootstrap and identity negatives)
- source/WebAgent/tests/validate_protocol_recovery_readback.py
- source/WebAgent/tests/validate_tri_one_2a_task_result.py
- source/WebAgent/tests/validate_tri_one_2a_matrix.py
- source/WebAgent/tests/validate_tri_one_5a_answer_only.py
- source/WebAgent/tests/validate_tri_one_5b_shared_worker.py
- source/agent_core/protocol_manifest.py require_protocol_manifest validation

Windows CI definition: .github/workflows/gar-runtime-offline.yml. A workflow file is NOT test evidence. Preserve real exit codes, source SHA, stdout/stderr hashes, and immutable attempts.

## Gates remaining before complete GAR acceptance

1. Source candidate is not PASS until real offline UT/IT and regression evidence are recorded.
2. L4 Source adapter and Session Fencing have now been implemented, but NO real browser/session bootstrap acceptance has been observed in the closed environment. A prepared independent conversation URL is mandatory; missing support means explicit L5 PAUSED, no fake success. A previously attempted stale L0 response is not a fresh Planner reply and L4 is not invoked to bypass its unknown transport result.
3. The DOM/Composer stale-readback root cause is a distinct transport issue; this implementation does not remove it.
4. Cross request/task/epoch continuation budget persistence and ownership need separate verified migration, not an unguarded automatic resume.
5. Real WebDirect/WebCopilot and RemoteAgent E2E require verified loaded Source SHA and actual original-interface delivery. No new Live was run here.

Minimal user-side report, without raw logs: case; original request; actual ingress (WebDirect/WebCopilot); interruption stage; reason code; action_executed; final observed state; whether resubmitted; loaded Worker SHA if attested. Existing sanitized helper: source/WebAgent/tests/goal1_live_triage.py.

No main merge, no force-push, no active Worker replacement, no old task replay.
Reference: doc/smartagent_goal_anchored_recovery_design.md.
