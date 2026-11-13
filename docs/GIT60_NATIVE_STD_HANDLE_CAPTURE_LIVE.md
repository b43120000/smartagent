# Git60 — Native Git/Python STD Handle repair and child output capture

## Git59 Live evidence motivating this repair

User task TASK-208FD6033AF74029E3B0 completed six independent Runtime ToolCalls in WIN32_APPCONTAINER, with OS token and kernel-denial evidence. Git direct executable PID 25344 exited 3221225480 (0xC0000008 = STATUS_INVALID_HANDLE). Python PID 22040 exited 0 but there was no version stdout from the PowerShell parent. Frozen diagnostic Goal PASS correctly meant the report was truthful, not that the two executables were verified.

The actual invalid handle has not been traced. Git59's direct C# CreateProcessW did not give valid STDIN, STDOUT or STDERR with STARTF_USESTDHANDLES; this is a likely but unproven cause.

## Git60 bounded solution

For exact, pinned Git/Python --version only, the C# Native Broker launches a direct OS-AppContainer executable with valid stdin/stdout/stderr handles. Three disposable capture files are created INSIDE the authorized Turbo working directory. SetHandleInformation enables inheritance only for those handles; PROC_THREAD_ATTRIBUTE_HANDLE_LIST explicitly restricts inheritance. Existing Win32 OS Token, package SID, internetClient capability, Job Object and outside-root denial checks remain required. No host shell execution or privileged host handles are passed.

The broker captures at most 32768 bytes per direct stream, validates lengths, and cleans up temporary files. The Python Runtime validates capture completeness and provenance. Frozen Goal conditions D07/D08 now require direct-child version stdout/stderr, separate direct-child exit code 0 and verified OS token. Shell output alone cannot establish positive native executable execution.

Generic commands and Native Broker security policy remain unchanged.

## Operator deploy and test

Stop all active RemoteAgent, Telegram Listener and Worker processes. In Windows CMD (manual Host CMD is permissible for setup, not for Agent execution):

    cd /d C:\smartagent
    git status --short
    git fetch origin
    git switch --detach origin/git60/appcontainer-stdio-handle-capture-20261114
    git rev-parse HEAD
    set "PYTHONPATH=source" && python -c "from agent_core.protocol_manifest import write_protocol_manifest,require_protocol_manifest; write_protocol_manifest('.'); r=require_protocol_manifest('.'); print('Verification:',r.get('hash_verification')); print('Mismatches:',r.get('hash_mismatches'))"

Expected: Verification PASS, Mismatches [].

Then elevated Windows PowerShell 5.1, as a human operator and NEVER as a model Agent ToolCall:

    $root = 'C:\smartagent'
    $sha = (git -C $root rev-parse HEAD).Trim()
    & "$root\install_smart_agent\turbo_native_toolchain_prepare.ps1" -ProjectRoot $root -ExpectedCommit $sha

Expected: OPERATOR_PROVISIONED, git_pinned=true, python_pinned=true.
The setup re-copies protected code and re-enrolls exact SHA256 pins. Use an already-approved PowerShell execution policy; do not bypass Group Policy or widen ACLs.

Restart RemoteAgent using its normal launcher.

In authorized private Telegram chat, send the SAME six-step Git59 Live test command:

    /git59_native_exec_live

## Positive acceptance

Require D01 NATIVE_SANDBOX_ATTESTED and D02 SIX_DISTINCT_READ_ONLY_COMMANDS. Git D07 and Python D08 must both show NATIVE_CHILD_EXECUTION_VERIFIED with native_executable_identity_verified=true, native_executable_probe_exit_code=0, native_executable_probe_capture_complete=true, native_executable_probe_output_source=OS_DIRECT_CHILD_PINNED_FILE_HANDLE, and version content in native_executable_probe_stdout. Only then can the separate terminal verdict be GIT59_NATIVE_CHILD_EXECUTION_PASS.

The overall frozen diagnostic Goal can legitimately PASS while true Git/Python execution remains UNVERIFIED. If Git still exits 0xC0000008, inspect direct-child stderr, per-action OS evidence, and protected broker reason. Never fall back to Telegram Host CMD to prove a native AppContainer action.

GitHub Actions compiles protected C# and PowerShell and exercises synthetic evidence tamper tests, not the user's real OS AppContainer. A user-local Telegram Live receipt is required to prove success.
