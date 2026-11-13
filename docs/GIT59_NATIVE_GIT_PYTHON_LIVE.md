# Git59 — Native AppContainer Git/Python child-token Live

Git59 extends the Git58 accepted baseline. It adds an authenticated
Telegram command /git59_native_exec_live which expands into a normal,
read-only natural-language requirement with exactly six Runtime run_command
steps. Nothing is sent through Telegram manual Host CMD as an Agent action.

## Two separate executable launches

The first launch is the existing AppContainer PowerShell invocation of an
exact, pinned executable version command. stdout/stderr is command output,
never an OS token attestation. For the exact read-only invocation of pinned
git.exe --version or python.exe --version, Git59 additionally performs a
second, direct CreateProcessW launch of that same executable using the
already protected Native Broker's LaunchWithTrustedPath function.

That native C# function verifies TokenIsAppContainer, the expected package
SID, internetClient capability and JOB_KILL_ON_CLOSE before resuming the
direct child, returning the process PID and exit code. This proves the
directly launched executable's OS token, but not the identity of every
grandchild that Git or Python might spawn. The two launches are explicitly
reported; a parent-only PowerShell receipt never proves child identity.

Only two exact commands are eligible for the additional probe, each with
the currently enrolled and SHA256 verified executable path. No global
injection flag, Host CMD fallback or extra policy authorization is added.

## Operator deployment

Stop existing RemoteAgent, Telegram Listener and WebAgent Workers BEFORE
switching branches. In ordinary Windows CMD (C:\smartagent is an example):

    cd /d C:\smartagent
    git status --short
    git fetch origin
    git switch --detach origin/git59/native-executable-child-token-e2e-20261113
    git rev-parse HEAD
    set "PYTHONPATH=source" && python -c "from agent_core.protocol_manifest import write_protocol_manifest,require_protocol_manifest; write_protocol_manifest('.'); r=require_protocol_manifest('.'); print('Verification:',r.get('hash_verification')); print('Mismatches:',r.get('hash_mismatches'))"

Require Verification: PASS, Mismatches: [].

The new protected PowerShell Broker source must then be deliberately
re-enrolled by the human operator in an **elevated Windows PowerShell 5.1**
(Administrator). Do not launch this admin setup from an Agent ToolCall:

    $root = 'C:\smartagent'
    $sha = (git -C $root rev-parse HEAD).Trim()
    & "$root\install_smart_agent\turbo_native_toolchain_prepare.ps1" -ProjectRoot $root -ExpectedCommit $sha

This protected setup checks the requested Git commit, ACL ON, Turbo ON,
active OS activation, policy revision, existing protected files, trusted
executables and absence of reparse-point redirection. It updates the
protected Broker files and executable SHA256 pins and grants the AppContainer
package read/execute access to the local private .venv. It does not grant
write/delete rights outside Turbo Root.

If it rejects with a typed GIT52 or GIT59 reason, stop and inspect the
precondition. Do not fall back to Host CMD execution to make a Live test PASS.

Restart RemoteAgent/Telegram Listener/Workers from the normal installed
launcher after those setup steps. The active authorized workspace must be
bound to the unique OS Turbo Root and Broker.

## One-message Telegram Live

Send exactly this text in the previously authorized private chat:

    /git59_native_exec_live

The Receiver creates the immutable source-grounded
DIAGNOSTIC_EXECUTABLE_RESOLUTION Goal and asks the actual GPT Planner to
issue six independent, read-only run_command commands (operation INSPECT):

    $env:PATH
    Test-Path -LiteralPath 'C:\smartagent\.venv\git\cmd\git.exe'
    Test-Path -LiteralPath 'C:\smartagent\.venv\Scripts\python.exe'
    Get-Command git, python -ErrorAction SilentlyContinue
    & 'C:\smartagent\.venv\git\cmd\git.exe' --version
    & 'C:\smartagent\.venv\Scripts\python.exe' --version

Paths above assume C:\smartagent; the Receiver dynamically uses the actual
installed root. No local files are created or deleted by these test commands.

## Acceptance

Inspect actual MIN-EV Runtime receipts and the frozen Goal State Table:
D01 confirms per-action Native Sandbox OS identity and scope denial.
D02 confirms six independent read-only commands with no extra ToolCalls.
D03-D06 record PATH, Test-Path and Get-Command observations.
D07 Git and D08 Python must BOTH report:

    reason_code = NATIVE_CHILD_EXECUTION_VERIFIED
    actual.native_executable_identity_verified = true
    actual.native_executable_probe_name = git / python
    actual.native_executable_probe_pid = positive integer
    actual.native_executable_probe_exit_code = 0
    actual.native_executable_direct_launch_count = 1
    actual.native_executable_probe_is_second_process = true
    actual.exit_code = 0
    actual.stdout contains the expected version text

Each associated sandbox_evidence must bind to its real task/action and
WIN32_APPCONTAINER, OS Token, Job Object and kernel outside-denial proof.
D09 checks the accuracy and uncertainty statements in the final report.

If a direct-child token cannot be verified, D07/D08 instead show
NATIVE_CHILD_TOKEN_UNVERIFIED_EXPLICIT. A nonzero process exit is
NATIVE_CHILD_EXECUTION_NONZERO_OBSERVED. An absent/mismatched version text is
NATIVE_CHILD_VERSION_OUTPUT_UNVERIFIED.

Important: Frozen Goal PASS means the requested DIAGNOSTIC REPORT is
truthful and complete; even a report of UNVERIFIED can legitimately pass
as a diagnosis. Git/Python positive execution verification requires BOTH
D07 AND D08 to be NATIVE_CHILD_EXECUTION_VERIFIED. Do not infer success
from a green terminal alone.

GitHub Windows CI compiles the native C# launcher and parses the protected
Broker PowerShell source. It also checks synthetic OS receipt tampering,
Telegram Goal compilation and inherited real filesystem Runtime regressions.
CI does NOT attest the user's Windows AppContainer; only this actual
Telegram Live run can produce that per-machine evidence.
