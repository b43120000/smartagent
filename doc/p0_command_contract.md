# P0 command contract

## Operation-specific admission

INSPECT and GIT_INSPECT may execute without an extra success_criteria/verify contract. Their stdout/exit code are observations, not automatic proof of BUILD or mutation success. Existing operation/command checks and command security remain in force.

BUILD, MUTATE, TEST, VERIFY, GIT_MUTATE, PROCESS, TRANSFER and GENERAL still require the verification contract. GENERAL never becomes terminal-eligible merely because it exits successfully.

## Working directory

run_command optionally accepts working_directory, an existing absolute directory within the writable policy and current request scope. The resolved directory is checked before approval/process creation. Omitting it uses the saved workspace only if that default stays within the request scope; it is not silently widened or guessed from Set-Location. For an authorized existing file, its containing directory is the command context (not a new write-policy grant).

Main command, verification commands and relative file_exists/file_contains paths use the same directory. Relative verification file targets remain subject to path authorization, including ADS and traversal checks. Output includes the actual directory and whether it came from the explicit field or workspace default. Capture/log/approval ownership stays with the original runtime workspace; executable approval is resolved using the actual command directory.

## Minimal field repair

Runtime retains an unexecuted rejected run_command for the current request/task epoch. It reports COMMAND_FIELD_REPAIR with the action ID, required fields and canonical sample types. The planner can send report_progress, then:

```json
{"tool":"run_command","action_id":"BUILD-001","verify":[{"action":"file_exists","path":"C:\\project\\app\\build\\outputs\\apk\\debug\\app-debug.apk"}]}
```

and turn_commit with the correct object count. Runtime restores command/operation/directory/dependencies, then repeats normal schema, scope, operation, approval, dependency and admission checks. It never executes the repair object independently.

Changing command/operation/directory/dependencies requires a new action ID and explicit replaces_action_id referring to the never-executed rejected action. No verification history is inherited. Executed/UNKNOWN actions cannot be field-patched or blindly replayed. Pending repair state is cleared on a new request; it is not crash-resume authorization.

## Verification

validate_p0_command_field_repair covers shared cwd (including a real read-only PowerShell smoke test), unauthorized/relative/file/ADS directory rejection, default-scope widening rejection, observation vs GENERAL contracts, three-round partial repair with exactly one execution, and cross-request/changed-identity/executed-target rejection. Related command/admission/progress/blocker suites are also run. No live ChatGPT/Telegram IT or Agent restart is part of this P0 implementation turn.
