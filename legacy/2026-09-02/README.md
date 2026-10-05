# SmartAgent

SmartAgent is a Windows-first local automation agent that uses the ChatGPT web
interface for planning and decision-making. It validates structured
`smartagent_tool` messages, performs approved operations on the local computer,
returns the real tool results to ChatGPT, and continues until a final natural
language response is produced.

The public package provides three entry points:

| Interface | Best for | Launcher |
| --- | --- | --- |
| LocalAgent | Starting tasks from a local CMD window | `launch_smart_agent.bat` |
| WebAgent Direct | Typing requests directly in a selected ChatGPT conversation | `launch_webcopilot_chatgpt.bat` |
| RemoteAgent | Receiving work through a remote conversation or Telegram | `launch_remote_agent.bat` |

> [!WARNING]
> SmartAgent can execute commands and modify files. Use it only on computers,
> workspaces, and ChatGPT conversations that you trust. Review the requested
> paths and operations before allowing local execution.

## Features

### ChatGPT planning with local execution

ChatGPT interprets the request, plans the work, and selects tools. The local
runtime validates the protocol and performs the actual operation. Supported
capabilities include:

- Listing, searching, reading, creating, and updating files and directories.
- Running bounded commands and returning stdout, stderr, and exit status.
- Uploading local files to the active ChatGPT conversation.
- Downloading fresh files, images, and other artifacts produced by ChatGPT.
- Handling large text through chunked transfer operations.
- Project scanning, source bundles, delta bundles, and verification workflows.

### Validated `smartagent_tool` protocol

Tool messages are never treated as commands without validation. The protocol
includes:

- Run and turn identifiers.
- A fresh local nonce for every turn.
- Alternating Web ACK and Local ACK verification.
- Unique action identifiers for exactly-once execution.
- Result identifiers that must be acknowledged by the following turn.
- Strict `final_response` and `turn_commit` ordering.
- Rejection of malformed JSON, unknown tools, replayed acknowledgements, and
  unsupported inline scripts.

### Persistent ChatGPT browser runtime

- Uses Playwright Chromium with a persistent login profile.
- Can reuse an existing CDP browser session.
- Selects conversations by conversation ID.
- Observes generation, attachment, and UI-idle state before accepting results.
- Uses a shared send lock and rate governor to reduce duplicate or overly
  frequent ChatGPT submissions.

### LocalAgent

LocalAgent is the CMD-oriented workflow. It supports workspace/conversation
binding, interactive startup configuration, multi-turn planning, local tool
execution, checkpoints, task status, project synchronization, and validation.

### WebAgent Direct

WebAgent Direct is separated from LocalAgent and RemoteAgent orchestration:

1. Paste a normal `https://chatgpt.com/c/...` conversation URL into CMD.
2. WebAgent opens or reuses that exact conversation tab.
3. It bootstraps or attaches the WebAgent protocol.
4. After CMD displays `READY`, type requests directly in the ChatGPT composer.
5. WebAgent executes approved local tools and returns results to the same
   conversation.

WebAgent owns its run IDs, turns, nonces, results, ACK chain, action ledger, and
final response loop. It does not create a LocalAgent worker or RemoteAgent task.

Each request exposes a request ID, round number, current attempt, and previous
ACK ID in the conversation. A missing or invalid ACK permits at most one repair
of the same request and round; WebAgent never turns ACK recovery into a new
local task or repeats an uncommitted local action.

Attachment submission is conservative. WebAgent waits while an upload ring is
visible, requires the ring-free state to remain stable, and revalidates all
requested filenames immediately before Send. It does not treat the Remove
button as completion, does not interrupt an unchanged upload after 30 seconds,
and does not automatically remove or re-upload a timed-out attachment. Explicit
ChatGPT upload errors stop the turn; the passive wait has a 600-second hard
limit.

### RemoteAgent

RemoteAgent provides remote ingress and result delivery, including:

- Telegram bot configuration and QR pairing.
- Conversation/workspace route binding.
- Durable task state, claim, heartbeat, retry, pause, cancellation, and result
  delivery.
- Independent receiver lifecycle through `launch_remote_agent.bat`.
- Canonical conversation-page reuse so sequential requests do not open a
  second tab for the same ChatGPT conversation.
- Cross-process queue refresh so work accepted after Agent 0 startup is
  dispatched without restarting the receiver.

For an offline transport simulation that still exercises the production task
queue, worker, WebGPT protocol, and result path, keep RemoteAgent running and
use:

```text
send_remoteagent_test.bat --request "list the workspace files"
```

Remote transport and automated recovery features are advanced functionality and
remain under active development.

## System requirements

- 64-bit Windows 10 or Windows 11.
- Python 3.11 or newer, 64-bit.
- Internet access.
- A ChatGPT account that can use the web interface.
- A desktop environment capable of running Playwright Chromium.
- Optional: Ollama for local or Ollama Cloud model workflows.
- Optional: a Telegram Bot Token for Telegram RemoteAgent ingress.

Login, password, 2FA, and CAPTCHA steps must always be completed manually by the
user. SmartAgent does not automate account authentication.

## Download and installation

Clone the repository:

```powershell
git clone https://github.com/b43120000/smartagent.git
cd smartagent
```

Alternatively, download the repository ZIP from GitHub and extract it to a
writable local directory.

Run:

```text
install_smart_agent.bat
```

The installer first performs a read-only environment check and reports each
item as `PASS`, `MISSING`, `BROKEN`, or `SKIPPED`. If every required item passes,
no installation is performed. If repair is required, review the list and press
Enter to continue.

The installer can:

1. Locate or install a compatible 64-bit Python runtime.
2. Create the project-local `.venv`.
3. Install the locked Python dependencies.
4. Install or repair Playwright Chromium.
5. Install and start Ollama when it is enabled.
6. Run post-install verification.

Check the environment without installing anything:

```text
install_smart_agent.bat -ValidateOnly
```

Install for WebGPT-only use without requiring Ollama:

```text
install_smart_agent.bat -SkipOllama
```

## Using LocalAgent

Configure a workspace and ChatGPT conversation:

```text
Edit_workspace.bat
```

Then start LocalAgent:

```text
launch_smart_agent.bat
```

Enter a natural language request in CMD, for example:

```text
List the Python files in this workspace and summarize their purpose.
```

The release launcher resolves `.venv`, `smart_agent.py`, `agent_core`,
`RemoteAgent`, and runtime state relative to the downloaded repository. It does
not depend on a parent development checkout.

## Using WebAgent Direct

Start:

```text
launch_webcopilot_chatgpt.bat
```

Then:

1. Paste a ChatGPT conversation URL containing `/c/...`.
2. Press Enter.
3. Wait while the browser opens and the protocol is sent.
4. Prefer waiting until CMD displays `READY`. A request entered during protocol
   startup is queued and processed after readiness instead of being lost.
5. Type the request directly in the ChatGPT conversation.

Example:

```text
webcopilot list the files under C:\path\to\project
```

Notes:

- Shared `/share/...` links are not supported; use a normal conversation URL.
- Do not switch that browser tab to another conversation while the controller
  is running.
- Do not run multiple automated controllers against the same conversation.
- Press `Ctrl+C` in CMD to stop the controller. The browser login profile is
  preserved.
- CMD prints a heartbeat while a browser turn is running. Detailed structured
  diagnostics are written to `WebAgent\state\webagent_runtime.jsonl`; this
  runtime file is ignored by Git.

## Using RemoteAgent

Configure RemoteAgent workspace and transport settings:

```text
Edit_Remoteworkspace.bat
```

Pair Telegram when required:

```text
launch_remote_agent.bat telegram-pair
```

Start the receiver:

```text
launch_remote_agent.bat
```

Keep the CMD window open after it displays `WAITING_SIGNAL`.

## Release verification

Verify that the downloaded tree is complete and does not import code from a
parent checkout:

```powershell
python verify_release.py
```

Run the offline WebAgent startup/session test:

```powershell
python -m WebAgent.tests.validate_startup_protocol_flow
```

Verify that human input is queued during startup and automated bootstrap sends
are not captured as user requests:

```powershell
python -m WebAgent.tests.validate_startup_input_queue
```

Run the offline `list_directory` protocol integration test:

```powershell
python -m WebAgent.tests.validate_standalone_loop
```

These deterministic checks do not replace a live ChatGPT, browser, Telegram, or
network test.

## Repository structure

```text
agent_core/                 Shared protocol, browser, tool, task, and recovery code
RemoteAgent/                Remote transports, receiver, scheduling, and workers
WebAgent/                   Standalone WebAgent Direct controller and protocol loop
install_smart_agent/        Environment checker, installer, and requirements
install_smart_agent.bat     Main installation entry point
launch_smart_agent.bat      LocalAgent launcher
launch_remote_agent.bat     RemoteAgent launcher
launch_webcopilot_chatgpt.bat
                             WebAgent Direct launcher
verify_release.py           Standalone release-boundary verification
```

## Security and privacy

- Do not commit `.agents`, `.venv`, browser profiles, logs, tokens, cookies,
  workspace paths, or personal conversation URLs.
- Keep the repository in a writable directory owned by the current user.
- Treat `run_command`, file writes, and artifact downloads as real local side
  effects.
- Use a dedicated workspace and conversation when evaluating the project.
- Review remote requests before enabling unattended RemoteAgent workflows.

## Known limitations

- ChatGPT web DOM changes can require browser selector updates.
- ChatGPT consumer accounts can be rate-limited.
- Conversation history is not a transactional message queue.
- Browser, account, Telegram, and network behavior cannot be fully validated by
  offline tests.
- Remote transport, self-repair, and meta-recovery components are still under
  active development.

## Project status

SmartAgent is under active development. Start with LocalAgent or WebAgent Direct
in a dedicated test workspace before enabling remote or unattended workflows.

No open-source license is included yet. Until a license is added, copyright law
reserves reuse and redistribution rights to the repository owner.
