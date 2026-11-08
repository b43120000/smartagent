# Minimal Core v10 — Runtime/Planner Responsibility Split

Status: first integrated refactor on branch refactor/pokeclaw-core-loop-20261108.
Baseline: cde1a46e121675932d32d0d5d7d7718cd2d52c14.

## Primary execution path

User request -> Runtime request/scope setup -> one compact model JSON decision ->
canonical schema and scope admission -> existing WebAgentToolContext.execute ->
actual ToolResult and Runtime action ledger -> bounded observation ->
next model decision, or blocked/finish.

The model emits ONLY one complete JSON object:

- {"type":"tool_call","name":"list_directory","arguments":{"path":"."}}
- {"type":"finish","summary":"observed result"}
- {"type":"blocked","reason":"verified blocker or missing input"}

The model does not author action IDs, progress ledgers, evidence IDs or transport
ACKs. The Runtime generates action IDs, preserves request/epoch ownership,
records original/canonical/dispatched action receipts and command outcomes, and
prevents an uncertain mutation from being automatically replayed.

v10 bootstrap is small and primary. Legacy v9 is retained for controlled
rollback through SMARTAGENT_CORE_LOOP=legacy_v9 (set before launching). New
request normal turns use protocol_expected=None and the browser UI-idle +
fresh-response completion logic. Final rendering does not issue an extra model
request in Minimal Core.

## Existing subsystems reused

- agent_core.smartagent_protocol.validate_tool_envelope
- agent_core.tool_capabilities.get_allowed_tools
- WebAgentToolContext.preview_tool_scope / execute
- agent_core.action_dispatcher -> agent_core.tools.execute_tool
- SecurityContext, command-operation checks, approval manifests and request write ceiling
- WebAgentProtocolLoop._execute_action for tool provenance and exactly-once local action-ID cache
- agent_core.tool_result_contract.decode_result
- Existing TaskContinuationStore and per-task action ledger

New local receipt journal:
localdata/runtime/minimal_core/TASK-*.jsonl

The journal contains hashes, IDs, operation classification, status and result
references. Full tool output remains in the Runtime action/result ledgers,
not the condensed planner prompt.

## Command operations

Use canonical run_command with command and operation. The existing executor
uses PowerShell 5.1. Exact known read-only commands such as git status --short
and cmd /c dir can use GIT_INSPECT/INSPECT. Python scripts, tests or commands
that may change state require success_criteria and nonempty verify checks,
plus every normal security and working-directory gate.

A command exit code alone does not prove a broad user goal. Single-command
read-only requests can finish after one observed successful command; broader
mutations remain UNKNOWN unless a proper goal-level verification path exists.

## Deliberate fail-closed properties

- Unknown/unregistered tools never dispatch.
- JSON fragments embedded in prose are never executable.
- Rejected arguments and forged Runtime fields are never executed.
- Duplicate attempted mutating command/action in one task is blocked.
- A Browser/DOM transport exception is not an automatic resend.
- A model-only finish with no narrow verifiable goal receipt remains PAUSED
  with outcome UNKNOWN rather than being claimed as successful.
- Existing file-transfer provider limits and approval guards remain enforced.
- Legacy v9 complex contracts remain available for rollback, not as another
  per-tool fast path.

## Offline validation

PYTHONPATH=source python -m pytest source/WebAgent/tests/test_minimal_core_v10.py -v

CI: .github/workflows/minimal-core-v10.yml

## Windows Live validation checklist

1. Launch on an isolated workspace and newly bootstrapped v10 conversation.
2. Request one-level list_directory of a known authorized directory.
   Target: model tool_call -> Runtime SUCCEEDED -> model finish (two model replies).
3. Read_file of one small known file, verify actual content.
4. Run git status --short inside a test Git repo.
5. Run cmd /c dir in an authorized working directory.
6. Run python -m pytest on a test project with explicit verification.
7. Create_directory in a disposable test workspace; check existence and
   ensure duplicate invocation does not mutate again.
8. Test malformed JSON, invalid scope, ambiguous execution and model-only
   finish; verify no fake success and no uncontrolled retries.

This is not a claim of Windows Live PASS. Source compilation, isolated
pytest and a real local list_directory dispatcher regression are distinct
from a real logged-in ChatGPT browser, Telegram and Windows execution test.

## Follow-up coverage gaps

- Broader multi-action goal completion still needs independently verified
  semantic acceptance (the current narrow verifier does not fabricate it).
- Long-running process resumption, artifact/image workflows and complex
  plan-transaction mutations require targeted Live regression coverage.
- Continuation after process interruption needs durable reconciliation of
  attempted/UNKNOWN mutation fingerprints across process lifetimes.
- Initial request bootstrap still invokes some existing runtime-local
  environment/scope preparation; no new Planner turns are required for this.


## v10 Decision Format Recovery (2026-11-08)

The prior Minimal Core parser applied whole-response JSON decoding. Real
ChatGPT DOM replies can wrap one JSON object in explanatory prose or markdown.
The primary transport now:

1. Records a bounded SHA256/size/structure-only response diagnostic.
2. Extracts outermost balanced JSON objects (not nested argument objects).
3. Accepts exactly one valid decision. Rejects arrays, multiple decisions,
   mixed valid/invalid objects, duplicate keys and legacy v9 tool fences.
4. Normalizes only unambiguous PokeClaw tool/type/argument aliases, then
   performs canonical Schema and Authorization checks.
5. Corrects only one exact user-provided unescaped absolute Windows 'path' or
   'working_directory' string by doubling backslashes; never guesses the
   target and never rewrites command/Python/Git/CMD bodies.
6. On failure, returns a precise, bounded JSON-repair instruction for one
   retry, including safe error reason and line/column when applicable.
7. On acceptance, invokes existing SmartAgent Tool Executor. Prose-only model
   replies are not silently treated as a successful Finish.

Runtime MinimalCoreJournal stores decision_rejected/decision_accepted events
including response hash, candidate counts, error codes, location and a
syntax-only masked preview (no raw model response).
collect.bat exports only bounded request-scoped scalar parser diagnostics in
minimal_core_decision_diagnostics.json; raw response text and masked local
journal previews are not uploaded.

The design deliberately differs from PokeClaw's permissive prose Finish
fallback: a model claim cannot replace executed ToolResult or verification.

Regression:
  PYTHONPATH=source python -m pytest source/WebAgent/tests/test_minimal_core_v10.py

Windows collector integration:
  PYTHONPATH=source python -m pytest source/WebAgent/tests/test_minimal_core_collector_windows.py

Offline/CI PASS does not establish a ChatGPT Web DOM / Telegram Live PASS.


## Tri-One Unified Runtime Terminal (2026-11-08)

The Runtime (not Telegram Worker, WebDirect, or Planner prose) now owns the
terminal status, outcome and accepted receipt. Shared implementation:
source/agent_core/tri_one_runtime_terminal.py

State flow:

- Ingress: trusted task/request/epoch, original goal and source reply route
- Action: Planner outputs one JSON decision; Runtime admits and executes
- Evidence: actual request-scoped ToolResult is added to Runtime ledger
- Goal evaluation: strictly bounded, deterministic proof from real ToolResult
- Terminal: V1-compatible accepted receipt (PASS) or nonterminal PAUSED
- Egress: Tri-One TaskResult V1 + shared render_task_reply; origin-only delivery

First deliberately bounded auto-finish contract: a SINGLE explicit Windows
directory-creation goal. Runtime requires a canonical create_directory result,
SUCCEEDED/DIRECTORY_CREATED, executed=true, same requested/resolved target,
directory_created/observed_exists/observed_is_directory all true and a confirmed
observation. This task completes after one model tool_call and no extra Finish
turn. It cannot auto-complete a compound goal (e.g. mkdir + write README).
Later successful directory inspection does not negate the existing proof;
a contradictory observation requires reconciliation.

On an accepted terminal: the receipt includes request_id, task_id, task_epoch,
receipt_id, evidence_refs, PASS verdict, COMPLETED and SUCCESS. This is the
trusted source for Tri-One TaskResult V1. The tool action ID is an identity
reference, not a model-authored evidence claim.

On an unverified model Finish: outcome UNKNOWN, state PAUSED, no accepted
terminal receipt. The shared Telegram / Local Test / WebCopilot worker uses
queue.pause_running + TASK_PAUSED and keeps progress, rather than queue.complete
and deleting evidence. WebDirect and its Local Test signal report the same
PAUSED status. Delivery success is tracked independently from goal success.

Tri-One TaskResult V1 is generated for v10 common worker by default unless
explicitly disabled in the environment; v9 legacy opt-in flags remain
available. The previously missing source metadata adapter is provided,
without treating Git HEAD as verified loaded-source provenance. Source SHA256
records in config/protocol_manifest.json are aligned with the branch.

Known boundary: the standalone interactive source/smart_agent.py legacy CLI
does not run the new Minimal Core, and its unverified chat text must not be
silently promoted into a Runtime-success receipt. The shared v10 worker,
WebDirect and Tri-One Local Test are covered; migrating the standalone legacy
CLI's execution engine itself remains separate work.

Live test priorities:
1. Reload/restart the current v10 process and use a fresh ChatGPT conversation.
2. On a DISPOSABLE, authorized Windows path that does not already exist, ask:
   create the exact folder. Expect one model tool_call, SUCCEEDED and PASS,
   one Runtime receipt, TASK_COMPLETED/SUCCESS with identical TaskResult V1.
3. Repeat on an already-present directory; Runtime must not overwrite and
   must not falsely claim create_directory executed.
4. Request create folder AND write README. The folder receipt alone MUST NOT
   complete this compound goal.
5. Force a model-only Finish with no tool proof: expect PAUSED/UNKNOWN,
   TASK_PAUSED, preserved progress, no fake accepted receipt.
6. Run the same terminal mapping through Telegram, Local Test, WebCopilot and
   WebDirect; verify the source route and delivery receipts remain separate.

Continuous integration:
- Linux v10 unit regressions and preexisting Tri-One V1 / 5B checks
- Windows v10 filesystem, commands, actual RCA collector, Tri-One tests
- Strict Linux + Windows protocol manifest hash verification

Passing CI is not equivalent to an actual Windows user-machine/DOM live PASS.
