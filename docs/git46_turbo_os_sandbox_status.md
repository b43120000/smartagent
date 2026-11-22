# Git46 — Turbo OS Sandbox: implementation and security gap record

**Base:** branch `git45/turbo-admission-evidence-finish-repair-20261110`, commit `090d2d9961c339a6f2432431fab66f082b6572d4`.

**Development branch:** `git46/turbo-os-sandbox-fail-closed-20261110`.
**Delivery class:** SAFE PARTIAL IMPLEMENTATION. Not a Windows OS Sandbox PASS.

## Verified code architecture

* `workspace_access_policy.json` remains the only editable workspace/Turbo-root policy file. `turbo_requested` is an explicit boolean there. An old Git45 file with `turbo_roots` but no explicit request migrates to OFF.
* `acl_mode` is independently obtained from the existing authoritative ACL security state. ACL OFF cannot persist a new Turbo ON request. ACL OFF transition clears Turbo roots and requests before publishing `mode=off`. Any workspace policy rewrite also invalidates the request.
* `turbo_sandbox.resolve_turbo_state()` exposes separate ACL, requested, sandbox, effective and reason fields, and refuses to treat a JSON or environment `READY` claim as OS authority.
* `pokeclaw_executor_loop`, `restricted_executor_client` and `restricted_executor_service` reject requested Turbo actions before the ordinary Git45 executor. This avoids silent fallback after admission or from direct service queue clients.
* Each rejected PokeClaw action writes a `TURBO_SANDBOX_BLOCKED` blackboard event with explicit `executed=false`, `os_execution_result=NOT_EXECUTED`, `os_access_denied=false`. `collect.bat` copies privacy-filtered metadata into `executor_sandbox_evidence.json` in RCA.
* Telegram Workspace Manager and Turbo text-control replies show requested and effective states separately.

## Deliberately UNIMPLEMENTED

There is **no runnable OS-enforced Turbo Broker yet**. `sandbox_backend=UNAVAILABLE_NO_OS_BROKER`; `sandbox_status=NOT_READY` unless policy resolution itself fails; `turbo_effective=OFF` for every input. No fake READY status, no unverified lease, no privileged child-process fallback.

Windows-specific acceptance is **NOT RUN / NOT PASSED**, including:

1. Dedicated Sandbox identity creation and immutable token/Package SID evidence.
2. Per-root NTFS ACL provision/attestation, root deletion protection, recursive existing DACLs and low integrity write label.
3. Native Win32 AppContainer/LPAC process launch, Python/PowerShell/Git/compiler availability, DLL resolution and child inheritance.
4. File-API negative tests, distinction between Windows ERROR_ACCESS_DENIED (5) and preflight software rejection, parent/other-project/other-volume denial.
5. Reparse-point/hardlink/open-handle/TOCTOU/cross-volume escapes and file-system broker abuse.
6. IPC, registry, network, services, devices and process-identity policy.
7. Reboot/revocation race, policy revision lease binding, Windows OS regression fixture and real-machine Live.
8. Finish/evidence contract integration with a *working* Turbo broker's actual ToolResult.

The existing restricted executor has workspace-wide access. It must never be reused as the future Turbo Sandbox identity.

## Windows OS backend acceptance design (not implemented)

Microsoft documents Win32 AppContainer launch through
`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` and its package-SID/DACL
two-principal access check. The implementation candidate is a **separate**
sandbox identity / lowbox token, package-specific SID and root-scoped NTFS
ACL; process creation must reject unavailable/invalid sandbox attributes.
All child processes must remain in the same OS-enforced boundary.

For Git clone, the network capability and required TLS/CA dependencies
need explicit validation, not implied permission. A newer
`Experimental_CreateProcessInSandbox` API exists on Windows 11, but is
experimental, subject to change and requires OS capability probing.

References:
- https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer
- https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox

## Test taxonomy

* **Unit / software contract:** `python -m pytest source/WebAgent/tests/test_git46_turbo_sandbox_contract.py -q`. Validates requested/effective logic, ACL combinations, attestation failure and no fallback. A unit PASS is not Windows OS PASS.
* **Existing regression:** Git34/42 Finish tests are included in the Git46 workflow. Git45 tests that expect unknown-command auto-confirm must be migrated to the new security contract and **must not be reported PASS as-is**.
* **Windows OS Sandbox:** No backend, no provisioned disposable identity/fixture; NOT RUN and NOT PASSED.
* **Live:** No real Windows instance was accessible from this implementation session; NOT RUN and NOT PASSED.

`git46-turbo-sandbox-contract.yml` names contract jobs explicitly. A CI job turning green says **contract PASS only**. True Windows OS tests require a separate fixture-backed, fail-hard job and a non-software OS access-denied observation.

## Operational recommendation

Do not enable full-access autonomous actions in production on this Git46 branch. The only safe expected Live behavior for a requested Turbo root is **BLOCKED / SANDBOX_UNAVAILABLE**. To continue ordinary Git45 operating mode, explicitly disable Turbo. Do not run any destructive sandbox probes against an actual E: project.
