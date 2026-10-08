# WebAgent Direct

WebAgent Direct is structurally separate from the LocalAgent/RemoteAgent
architecture.  It does not import `smart_agent.py` or `web_copilot.py`, create a
`SmartAgent`, start `host_supervisor`, enqueue RemoteAgent tasks, or use Agent-1
workers.

Its own protocol loop owns run IDs, turns, local nonces, result IDs, alternating
Web ACKs, exactly-once action IDs, attachment scheduling, and final response.
It reuses only shared infrastructure from `agent_core`:

- `web_runtime` for the authenticated ChatGPT browser/CDP and artifacts;
- `session_protocol` and `smartagent_protocol` for wire validation;
- `tools` for local computer operations.

All WebAgent code, state, tests, and documentation live in this directory.  The
only root-level file is `launch_webcopilot_chatgpt.bat`.

## Run

1. Run `launch_webcopilot_chatgpt.bat` from the repository root.
2. Paste a normal ChatGPT `https://chatgpt.com/c/...` conversation URL into CMD
   and press Enter.
3. WebAgent opens that exact conversation in the persistent browser, sends the
   WebAgent/smartagent_tool bootstrap or session-attach protocol, and prints the
   send state in CMD. The shared send lock is rate-limit infrastructure only;
   it does not start LocalAgent or Agent1.
4. Prefer waiting for `READY`. Human input submitted during bootstrap is queued
   and processed after the protocol becomes ready.
5. Submit natural language in the ChatGPT composer.

Example:

`webcopilot list 出這裡有哪些檔案 C:\Users\ExampleUser\Desktop\picture`

Every request displays its request ID and round/ACK chain. ACK repair is limited
to one retransmission of the same logical round. Attachment upload waits are
passive: a visible upload ring keeps the turn waiting, disappearance must remain
stable, and WebAgent never removes or reselects a stalled attachment. The hard
wait limit is 600 seconds.

The controller writes structured diagnostics to
`WebAgent\state\webagent_runtime.jsonl`. Runtime state is excluded from Git.

## Offline integration test

```text
.venv\Scripts\python.exe -m WebAgent.tests.validate_startup_protocol_flow
.venv\Scripts\python.exe -m WebAgent.tests.validate_startup_input_queue
.venv\Scripts\python.exe -m WebAgent.tests.validate_standalone_loop
```

The startup test verifies observable busy-lock/rate-delay handling plus protocol
bootstrap and session attach. The startup-input test verifies capture queue
ownership. The standalone-loop test exercises the real shared parser/ACK
validator and `list_directory` executor. It also asserts that neither
`smart_agent` nor `web_copilot` is imported.
