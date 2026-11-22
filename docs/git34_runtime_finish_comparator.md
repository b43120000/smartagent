# Git34 — Runtime Finish Comparator / Tri-One Binary Final Acceptance

Based on Git33 commit 4bdc38545b99e49b7b4c08caddd21369a1e13237.

## Architecture

Model ToolCall → Runtime ToolResult → Executor Blackboard → Model Finish with
expected_runtime → Runtime Blackboard/Action Ledger readback → exact typed
field comparison → independent original-request Goal proof → Runtime Terminal
Receipt → Tri-One TaskResult → Telegram/Local Test/WebCopilot/Web Direct.

Goal Driver and Prerequisite decisions stay DISABLED. No Tool Admission, path
scope, mutation replay, or execution authorization has been relaxed.

## Finish transport

The model must submit a claim for each executed ToolResult, keyed by the
Runtime-provided MIN-EV reference. Claims are not execution authorization:

~~~json
{
  "type": "finish",
  "summary": "資料夾已存在",
  "expected_runtime": [
    {
      "evidence_ref": "MIN-EV-...",
      "fields": {
        "status": "SUCCEEDED",
        "data.observed_exists": true,
        "data.observed_is_directory": true,
        "data.observation_status": "CONFIRMED"
      }
    }
  ]
}
~~~

Comparator recomputes all evidence from Runtime raw ToolResult and verifies
result SHA256, request/task/epoch, Action binding and Blackboard readback.
Model omission, inconsistent fields, stale/foreign evidence, unknown execution,
admission rejection and unsupported Goal coverage are FAIL, never PASS.

A field match alone is not Goal PASS. The independent Goal verifier uses the
original request and a bounded contract. Initial supported contracts include:
single path fact query (observe_fact, or typed inspect_directory), single
directory existence/absence assertion, existing strict mkdir/conditional
directory contracts, and explicit single-command expected-zero/nonzero exit or
exit-code reporting. General multi-step / open-ended Goals fail closed with
GOAL_COVERAGE_NOT_PROVEN until a validated coverage adapter is registered.
This is not a claim that every arbitrary natural language task is verified.

## Separate statuses

- Tool execution: SUCCEEDED / FAILED / REJECTED / UNKNOWN (unchanged).
- Observed facts: typed booleans, values and observation status.
- Model finish expectation: expected_runtime (untrusted).
- Goal acceptance: final_verdict PASS / FAIL (Runtime-owned).
- Legacy task outcome SUCCESS / FAILED / PARTIAL / UNKNOWN remains for
  compatibility and in-flight state. A final TaskResult exposes final_verdict
  as PASS or FAIL. PAUSED is nonterminal and has no final verdict.

A run_command returning exit_code=1 is a tool failure but can still provide
a successful, verified expected-failure goal when the original user request
explicitly asks to confirm it. A negative file.exists=false is a successful
observation when the query/absence goal is satisfied.

Tri-One Terminal Receipt contains comparison.reason_code,
comparison.mismatches, comparison.tool_results, with evidence_verdict PASS|FAIL.
All transports consume the same receipt; Telegram only formats it, it never
recalculates acceptance.

## Tests

Positive and negative directory existence queries; explicit absence assertion;
model/Runtime field mismatch; model evidence omission; matching UNKNOWN cannot
yield PASS; unproven multi-step goal fails closed; expected nonzero shell exit
with Tool FAILED and Goal PASS; Tri-One TaskResult mapping; legacy regressions.

Run: PYTHONPATH=source python -m pytest
source/WebAgent/tests/test_git34_runtime_finish_comparator.py -q
