# ACL ON failure diagnosis (in-place update, baseline 6b5629)

This release adds diagnostic files without replacing `ACLstatus.bat`, `Edit_workspace.bat`, `install_smart_agent/acl_status.ps1`, or the existing ON/OFF authorization paths.

## A. Read-only check after a failed ACL ON

From the SmartAgent installation folder, double-click `check_acl_on.bat`, or run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_smart_agent\diagnose_acl_on.ps1 -ProjectRoot .
```

The report examines Python/manifest preflight, ACL mode, provisioning profile, machine-wide scheduled task, Executor self-test, current heartbeat, machine authorization, and final `mode=on` commit. It prints `earliest_unmet_postcondition`.

Stages: `PRECHECK`, `SECURITY_PROFILE`, `EXECUTOR_TASK`, `EXECUTOR_SELF_TEST`, `MACHINE_AUTHORIZATION`, `EXECUTOR_FINAL_READY`, `ACL_MODE_COMMIT`.

This scan is **read-only**. It cannot reconstruct the original thrown error from a past run and it never grants a complete Windows OS ACL attestation. A stale heartbeat after a failed launch is not by itself proof of the original failure. Inference = current unmet postcondition, not necessarily the exact prior failure line.

## B. Capture the *next* actual ACL ON failure

Use `run_acl_on_diagnostic.bat` instead of `ACLstatus.bat on` **only when intentionally retrying ACL ON**.
It requires administrator elevation and then requires typing the exact confirmation `RUN-ACL-ON`. It executes the original `acl_status.ps1` ON implementation, not a reimplemented state changer.

Results are saved under `%LOCALAPPDATA%\SmartAgent\acl_on_diagnostics\`:

- `acl_on_<timestamp>_<id>.txt`: full PowerShell transcript.
- `acl_on_<timestamp>_<id>_error.json`: original caught Exception and stack.
- `acl_on_<timestamp>_<id>_snapshot.json`: structured, read-only evidence at the end.

The diagnostic wrapper never automatically runs ACL OFF, retries a failed mutation, deletes journals, or changes security outside the original ON procedure.

Exit codes for the read-only postmortem: `1` = unmet postcondition; `2` = not proven, including an ON state that still needs independent OS verification.
Exit codes for the ON wrapper: `0` = original ON returned and wrote mode on; `1` = exception or missing on commit; `3` = user declined; `4` = launcher elevation failed.

## GitHub Windows tests

`install_smart_agent/tests/validate_acl_on_diagnosis.ps1` creates a disposable fixture and tests missing profile, missing executor state, machine authorization mismatch, manifest absence, final commit absence, and read-only behavior. CI never invokes actual ACL ON. These fixture results cannot certify the user's actual PC.

Note: `check_acl_on.bat` does not require administrative mutation privileges. Reading the protected machine authorization or Task Scheduler may still require elevated read permissions. Keep original diagnostic logs private.
