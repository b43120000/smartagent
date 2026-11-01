# PR-0 Task Requirement Assessment V1

Implementation target: main. Parent: SmartAgent Prerequisite Resolution v3
(PR-A through PR-D remain separate future stages).

## Responsibilities

PR-0 classifies a trusted original request once per V9 request. It does
not execute tools, request permissions, grant authorization, satisfy a
prerequisite, or manufacture a terminal receipt. The structured assessment
is Runtime-owned; the Planner can see it but cannot set it.

SMARTAGENT_TASK_REQUIREMENT_V1 fields:

- route: ANSWER_ONLY, RESOURCE_REQUIRED or AMBIGUOUS
- resource_sources: NONE, INLINE, LOCAL, GITHUB, EXTERNAL (multiple permitted)
- required_operations: READ, WRITE, EXECUTE, VERIFY, DELIVER
- required_capabilities: e.g. LOCAL_WORKSPACE, GITHUB_REPOSITORY_ACCESS,
  EXTERNAL_ACCESS, COMMAND_EXECUTION, EXTERNAL_DELIVERY
- restrictions, reason, original intent_sha256, decision_owner=RUNTIME_POLICY
- grants_authorization=false; verification_complete=false

No remote GitHub capability is implicitly installed simply because PR-0
detects a GitHub resource. Git push/write still requires established
executor capabilities, explicit permissions and action-level evidence.

## Routing

ANSWER_ONLY: only a candidate for the existing 5A zero-operational-action
V9 response; success requires all original Progress, Action Ledger,
turn_commit, Terminal Receipt and TaskResult gates. No fake PASS.

RESOURCE_REQUIRED: deliver the source/operation hints to canonical V9
planning and, when PR-A through PR-D exist, build prerequisite requirements
for the next legal operation; unknown capability is not permission.

AMBIGUOUS: no Answer-only admission and no implicit tool permissions.
Normal Planner and fail-closed policies remain authoritative.

The current assessment is intentionally bounded and deterministic for the
tested English/Traditional-Chinese intent forms. It is not a guarantee
of perfect arbitrary-language semantic classification. Do not accept a
model-proposed ANSWER_ONLY claim without independent Runtime scope gates.

## Feature and compatibility

The feature registry contains pr0_task_requirement / SMARTAGENT_PR0_V1.
Default ON, eligible roles: local_agent, remote_agent, web_direct. The
shared V9 runtime currently wires the assessment in RemoteAgent and
WebDirect. LocalAgent still needs its separate Tri-One Phase 2 Planner
integration; an ON config is not proof of that end-to-end capability.
An explicit zero switches the 5A gate to the previous legacy classifier.
PR-0 never overrides the independent 5A feature flags.

## Validation

Windows PowerShell repository root:

    $env:PYTHONPATH = (Resolve-Path .\source).Path
    & .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_pr0_task_requirement.py
    & .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_tri_one_5a_protocol_it.py
    & .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_tri_one_5a_answer_only.py

The above tests use a scripted Planner and isolated fixtures; even true
V9-loop tests are offline IT, not real WebGPT/Telegram Live. The Windows
Main GitHub Actions workflow is a mandatory offline regression.

A real Live gate must record the loaded Worker source SHA, corresponding
request_id/task_id/epoch, task_requirement_assessed event, route and
sources, accepted V9 terminal for Answer-only, zero executed tools,
and a delivered result on the originating interface. Negative Local/
GitHub/mixed requests must not create false Answer-only receipts.
