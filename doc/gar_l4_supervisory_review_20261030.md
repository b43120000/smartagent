# GAR v1 L4 — WebGPT2 recovery review, WebGPT1 continues

Base SHA: `75febba681949d2541f51f2423420dc2b2a35867`
Branch: `feat/gar-v1-l4-supervisor-webgpt1-20261030`.
This document describes implementation intent, **not** a Live PASS.

## Runtime behavior

1. WebGPT1 owns the original task, scope, Task/Request/Epoch, progress, V9 ACK chain, action ledger and outbox. Existing local Bounded Repair, L2 progress rebase and L3 replan are retained.
2. On safe L4 PREPARE (repeated fresh V9 rejections **or Runtime-observed semantic stagnation** such as repeated accepted no-action Progress / exhausted authorization guard, no OPEN/UNKNOWN side effects, cumulative budget intact), Runtime synchronously pauses the WebGPT1 round; no original task replay. Semantic L4 is a distinct, one-shot trigger: it does **not** fabricate V9 rejection counts or reset Bounded Repair. The browser adapter attaches the uniquely identified WebGPT2 conversation in the **same** Chrome BrowserContext, checks ownership and idle state, and completes a separate read-only `gar_l4_supervisor` protocol bootstrap.
3. Runtime passes WebGPT2 a bounded Goal/Progress summary, including the previously completed plan steps, current/blocked step, failure reason, checkpoint digest, verification preconditions and budget. Raw tool results and Action Ledger are **not** transferred as authority. WebGPT2 returns exactly one JSON advisory with challenge nonce, completed steps, blocked step, failure analysis, revised blocked step and continuation plan.
4. Runtime validates nonce/schema, disallows tool transport, verifies unchanged checkpoint, and releases the WebGPT2 tab owner. WebGPT1 remains the execution Planner. The advice is attached to its next V9 prompt under `GAR_L4_WEBGPT2_ADVISORY_NOT_EVIDENCE`; it is explicitly **not** proof of execution, terminal SUCCESS, permission or updated plan. The original Progress, Tool and Terminal admission gates remain authoritative.
5. Invalid/ambiguous response, unknown submit, missing/double-owned target, wrong identity, or exhausted budget → safe L5 PAUSED with checkpoint. There is at most **one** WebGPT2 review, at most two subsequent original-planner format repairs, and no second bootstrap or action replay.

## Windows startup

Use `launch_gar_l4_supervisor.bat` in the checkout root. Set `GAR_WEBGPT2_URL` (full **existing** second ChatGPT conversation URL); optionally set `GAR_WEBGPT1_URL`. If WebGPT1 is blank, the existing `Edit_workspace.bat` startup preference is used. The BAT sets `SMARTAGENT_GAR_V1=1`, `SMARTAGENT_GAR_L4_SUPERVISOR_V1=1`, `SMARTAGENT_GAR_FRESH_CONTEXT_URL`, `SMARTAGENT_GAR_A0_DIAGNOSTIC=1`, `TRI_ONE_TASK_RESULT_V1=1`, `TRI_ONE_TEXT_ONLY_V1=1`, and **D1 injection OFF**. It delegates to existing `launch_webcopilot.bat`; existing browser startup opens/reuses WebGPT1 then opens/reuses one WebGPT2 tab in the same context. It does not start a new browser profile or interfere with Edge1.

The supervisor is the default L4 mode when GAR is enabled. `SMARTAGENT_GAR_L4_SUPERVISOR_V1=0` is retained only for explicit legacy takeover testing; do not use it in the new L4 release.

## Tests / acceptance

From root, with `PYTHONPATH=source`:

```bat
python -B source/WebAgent/tests/validate_gar_l4_supervision.py
python -B source/WebAgent/tests/validate_gar_l4_fresh_context.py
python -B source/WebAgent/tests/validate_gar_protocol_loop_it.py
python -B source/WebAgent/tests/validate_gar_shared_runtime.py
run_gar_regression.bat
```

The new offline IT asserts the original WebGPT1 handles the final V9 result **after** exactly one WebGPT2 review; no WebGPT2 tool planner call, no action replay, non-evidence advisory, and fail-closed malformed review. Browser Live and RemoteAgent LOCAL_TEST still require the user's real Windows Chrome3 runtime. Never record a fake Live PASS.

Recommended events: `gar_l4_webgpt1_paused_for_review`, `gar_l4_webgpt2_advice_received`, `gar_l4_webgpt1_resumed`, `gar_l4_supervisor_review_failed`.
