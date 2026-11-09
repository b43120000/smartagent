# Git36 — One Finish with an upstream Evidence Requirement

Parent Git35: 45fbeeda34f14ecadbd88834bb323b808daf6dbd.

## One pipeline

Original User Request → upstream Runtime-owned Evidence Requirement
→ one PokeClaw Executor (model chooses tool calls or Finish)
→ one Model Finish (summary, optional expected_runtime and goal_checks)
→ one Runtime Finish Comparator → Blackboard Terminal Receipt
→ one Tri-One TaskResult → all ingress/egress transports.

The upstream requirement is a **minimum evidence boundary, NOT an Executor
route**. It does not authorize tool operations, hardcode a task plan, or turn
the legacy Goal Driver / Prerequisite back on. An eligible text request may
finish without tools, or use tools if genuinely necessary. Tasks requiring
outside state cannot be satisfied by text-only assertions. Mixed tasks
include actual ToolResults and their explanatory text in one Finish.

## Requirement recorded before Executor Loop

The task-scoped Blackboard now persists:

- request_sha256, original requirement classification and reason;
- resource sources / required operations;
- answer_only_eligible, tool_evidence_required;
- text_delivery_allowed and external_state_verification_required;
- authorizes_operation=false.

The requirement is immutable after the Blackboard run begins, bound to
task identity and request SHA256 and checked at Finish. Actual Tool Admission,
path authorization and Write Ceiling remain separate.

## One Model Finish

Text candidate:

~~~json
{
  "type": "finish",
  "summary": "這裡是我寫的雨天笑話……",
  "expected_runtime": []
}
~~~

Tool / mixed candidate:

~~~json
{
  "type": "finish",
  "summary": "資料夾存在，已提供文字說明。",
  "expected_runtime": [
    {"evidence_ref": "MIN-EV-...", "fields": {
      "status": "SUCCEEDED",
      "data.observed_exists": true
    }}
  ],
  "goal_checks": [
    {"criterion": "確認存在", "evidence_ref": "MIN-EV-...",
     "field": "data.observed_exists", "operator": "eq",
     "expected": true}
  ]
}
~~~

Model writes the fields from the original task and actual ToolResults; there
is no tool-name-specific required list. The Runtime validates scalar types,
event/ledger/request identities and all evidence claims.

## Zero-Tool Finish is not an evidence failure

No ToolResult is valid for a request that the upstream requirement permits
to finish with text. This condition must also pass the shared
validate_runtime_answer_only checker, reused by legacy
accept_runtime_answer_only. It rejects empty/oversized answers, unsupported
claims of executed operations, externally sourced or mixed requests, attached
context, active action/recovery state, and original-goal mismatch.

For such a Finish, the Comparator emits PASS with:
- completion_kind=ANSWER_ONLY;
- coverage_basis=RUNTIME_TEXT_ADMISSION;
- evidence_refs=[];
- answer_sha256 bound to the persisted Blackboard summary digest;
- tool_results=[].

This PASS means a valid text response was admitted, NOT that the joke is
subjectively funny or that outside factual claims have been verified.

When the original task requires tool execution, external state or artifacts,
a no-tool Finish is FAIL with TOOL_EVIDENCE_REQUIRED. Attempted but rejected
tools are also not treated as a valid zero-tool response.

No fake ToolResults are synthesized.

## Tri-One

The same Runtime-owned comparator receipt maps to the same
task_state/outcome/final_verdict and PASS/FAIL display for Telegram,
WebCopilot, Web Direct and Local Test. For a positive text-only admission,
the TaskResult contract now accepts ANSWER_ONLY with a comparator-issued
evidence_verdict=PASS, and keeps the legacy v10 answer-only receipt behavior
for compatibility. The answer's SHA256, goal SHA256 and Blackboard terminal
are checked before the Runtime terminal is projected.

A completed text answer is delivered as text, without appending the internal
Blackboard debug trailer. Blackboard, comparator and receipt remain available
for audit and RCA.

## CI

Tests exercise real prompting flow with no ToolResult for a rain joke,
translation, greetings and writing; text-only and Telegram/Web Direct mapping;
negative tool requirements, mixed tool + text, fake claims, false execution
assertions; plus Git33/34/35 and earlier Tri-One regressions. Runs on
Windows and Ubuntu with Python 3.13.
