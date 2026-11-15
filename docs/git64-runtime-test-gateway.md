# Git64 — Runtime Test Gateway (Planner-free)

This is a **local Python API and CLI** that submits a bounded test Action to
SmartAgent's **existing** `agent_core.tools.execute_tool` Runtime, without
asking a Planner, Telegram, or WebCopilot to construct the Action.

It is **not** an HTTP endpoint, a privileged executor, or an alternate
AppContainer broker. It cannot configure ACL, Turbo Root, Windows SIDs,
authorization profiles, or broker activation.

## Local Live test — only after installing this branch

From Windows CMD, in the SmartAgent checkout (`C:\smartagent`):

```bat
cd /d C:\smartagent
set "PYTHONPATH=C:\smartagent\source"
C:\smartagent\.venv\Scripts\python.exe -m agent_core.runtime_test_gateway --case source\WebAgent\tests\fixtures\git64_runtime_native_liveness.json --live
```

The example runs `Start-Sleep -Seconds 12` using `run_command`,
`operation=TEST`, `timeout=30`, with the exact actual CWD:
`E:\AIspace\SmartAgent_Turbo_Live_01`.

**No `--live` means no execution.** Every mutation must first pass
`BROKER_REQUIRED` for its **actual Action target**, and the Runtime
is rechecked and refuses ordinary host/restricted-executor fallback if the
ACL/Turbo policy changes before dispatch. The test gateway does not
set an ACL/Turbo flag in a fake profile; it reads the installed policy.

A console-only **JSON summary** reports the actual Runtime status, exit code,
Native identity, stage/substage/HRESULT, CWD/PSDrive/provider observations
and last RUNNING evidence. Original stdout/stderr, arbitrary commands, raw
paths and secrets are not copied into the JSON summary. The full Runtime
ToolResult is used in memory to assert expected fields and compute a SHA256
digest. The Gateway does **not** create the Telegram Executor Blackboard, nor
does it invent a `MIN-EV-...` reference.

You can import `run_case(case, live_authorized=True)` from
`agent_core.runtime_test_gateway` as a synchronous Python API. Each
fixture has a `schema`, an operator-configured `workspace`, and
1–12 ordered `actions`, each with `action` and `expect` dictionaries.
Every Action runs through the shared Runtime. On an unexpected outcome,
execution stops; failed mutations are **never automatically retried**.
Expected values are limited to safe, typed Runtime status/evidence fields.

## CI scope

GitHub Actions `.github/workflows/git64-runtime-test-gateway.yml`
runs Python UT on Linux and Windows and parses the Native PowerShell Broker
script on a Windows runner. UT uses mocked policy/OS receipts; these are
**adapter and dispatch contract tests, not real Windows sandbox attestation**.

Real ACL ON + Turbo ON, SID, native Job Object, E: filesystem rights,
PowerShell Provider initialization, and kernel process liveness require the
registered **Windows Live machine** (or a separately provisioned equivalent
Windows self-hosted runner). A GitHub-hosted Windows runner does not have
the operator's protected `E:\AIspace` Turbo enrollment.

The Gateway validates Runtime behavior without Planner decisions. It does
not itself prove the complete Planner→Runtime task/terminal/evidence flow,
which remains a separate end-to-end test.
