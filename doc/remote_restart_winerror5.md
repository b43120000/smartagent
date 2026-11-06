# RemoteAgent restart helper WinError 5 (Windows Job Object)

## Symptom
Telegram control reply:
`RemoteAgent control failed: PermissionError: [WinError 5] Access is denied`

If the command is `重啟連線`, the likely failure is before ForceStop:
`HostSupervisor._request_full_remote_restart()` calls
`RemoteAgent.remote_restart.spawn_restart_helper()`, which launches a
detached helper with `CREATE_BREAKAWAY_FROM_JOB`. Windows can refuse this
flag when the parent process belongs to a Job Object lacking the
`JOB_OBJECT_LIMIT_BREAKAWAY_OK` privilege.

## Narrow compatibility recovery
On Windows only, after the initial helper spawn raises
`PermissionError` with `winerror == 5`:
- emit `RESTART_HELPER_BREAKAWAY_DENIED` into this installation's restart log,
- retry the *identical helper command and working directory* once, with
  `CREATE_BREAKAWAY_FROM_JOB` removed,
- retain `DETACHED_PROCESS` and `CREATE_NEW_PROCESS_GROUP`,
- log the fallback PID if the second process starts,
- otherwise propagate the original class of exception.

This is not elevation, privilege bypass, a ForceStop scope change, or an
override of Windows Job Object restrictions. When a parent Job Object is
configured to kill children on shutdown, its restrictions may still prevent
the fallback helper from surviving the host's exit; live restart validation
is therefore still required. The existing restart helper remains responsible
for the current-install-root ForceStop and post-restart readiness check.

## Evidence to check
Inspect the local restart log under this installation's runtime log path
(`remote_restart_log_path`) for `RESTART_HELPER_BREAKAWAY_DENIED`,
`RESTART_HELPER_BREAKAWAY_FALLBACK_STARTED`, and later
`RESTART_HELPER_START` / `NEW_RUNTIME_READY`.
The latter two entries prove the helper executed and reconnected, not merely
that the fallback Popen call returned a PID.

## Regression
Windows and Linux CI mock the Windows flags, test one-and-only-one retry for
WinError 5 and fail-closed behavior for unrelated errors; also run all
ForceStop local-root isolation tests. No live host processes are terminated
during CI.
