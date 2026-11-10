# Git47 — WebCopilot readable Tri-One output + Windows disposable AppContainer probe

**Base branch / SHA:** `git46/turbo-os-sandbox-fail-closed-20261110` at
`929b555b83dcac3d49271e0118eadc3c943297ce`.
**Branch:** `git47/webcopilot-readable-result-and-turbo-os-20261110`.
Neither Git46 nor main was changed.

## P0: WebCopilot natural-language presentation

The durable Tri-One `TASK_RESULT_V1` remains the source of truth.
`webcopilot_result_view.py` binds result to Task/Request/Epoch,
source-interface identity, exact conversation and validated
`terminal_acceptance`, using `validate_task_result`. It unwraps
recognizable JSON result envelopes into bounded Chinese prose (including
`summary`, `answer_text`, `response`) with the actual completion
state and evidence count. It never manufactures success from worker state.

`webcopilot_result_panel.py` displays a WebCopilot-owned shadow-DOM card
without modifying the model conversation. Data is inserted using
`textContent` only, not arbitrary HTML. A close button is available.
`web_copilot.py` polls the event store after TaskState transitions
because terminal state and canonical egress can be persisted separately.
If canonical metadata is absent, it explicitly reports unverified legacy
output and keeps a notice fallback if the panel cannot render.

### Tests

`source/WebAgent/tests/test_git47_webcopilot_result_panel.py`
covers verified result, cross-conversation rejection, paused/no-evidence,
non-exposure of unrelated command/secret fields, and structured JSON
readable summaries. Existing Tri-One egress, Git46 contract and
WebCopilot startup regressions are in the CI matrix.

**Real browser Live:** NOT RUN. Unit/CI outcomes do not prove rendered
behavior in a connected ChatGPT page.

## P1: native Windows OS sandbox discovery (isolated fixture only)

`install_smart_agent/turbo_appcontainer_probe.cs` uses the native
`CreateProcessW` with Security Capabilities and an AppContainer package
SID. Child token `TokenIsAppContainer` and `TokenAppContainerSid`
are verified before `ResumeThread`. The child belongs to a kill-on-close
Windows Job. The launcher supplies an explicit Unicode environment
allowlist (no controller token/key inheritance). This is not a production
broker, and its host-side C# entry point is not exposed as a general
untrusted request processor.

`install_smart_agent/turbo_appcontainer_disposable_probe.ps1` creates a
GUID-named test fixture exclusively under the CI temp directory. It sets
package-SID DACL rights allowing inner-root reads/writes and children
deletions, but denies deletion of the root object, and labels test root
Low Integrity. No real E: directory, other project, system DACL or
existing user work directory is modified.

The in-container `turbo_win32_file_probe.cs` calls Win32 File APIs and
records `Marshal.GetLastWin32Error`. Positives and negative OS tests
cannot pass from a software `REJECTED` result.

### Observed Windows CI evidence (at least run 42751676118)

- Root native read/write: `ERROR_SUCCESS=0`.
- Parent directory and sibling project direct Win32 read/write:
  `ERROR_ACCESS_DENIED=5`.
- Native child process in-root read succeeds; sibling read gets error 5.
- In-root `DeleteFileW` succeeds.
- Empty protected Turbo root `RemoveDirectoryW` gets error 5.
- Hard link to external sibling data read gets error 5.
- Junction to sibling data read gets error 5 (later fixture versions).
- AppContainer child does not inherit an explicit controller secret
  environment variable (this is an environment probe, not an OS file
  denial).
- CMD and Windows PowerShell executed in AppContainer and created
  disposable in-root marker files.
- Host Python had `0xC0000135` (`STATUS_DLL_NOT_FOUND`) during
  the first tool-probe runs; Git returned 128, not capability PASS.
  A copied read/execute Python dependency fixture is under evaluation;
  report actual later CI evidence, not assumptions.

### Still NOT DONE

**Production Turbo remains strictly `NOT_READY`, Effective=OFF.**
The Git47 AppContainer probe is not a replacement for a request-scoped
authenticated OS Sandbox Broker, hardened provisioner and tool lease.
At minimum, still require:

- Runnable Python/Git/compiler dependency provisioning with package SID
  read/execute without widening other ACLs.
- Full negative suite across volumes, symlinks, junction/hardlink races,
  open handles, broker services, IPC/registry/network/device boundaries.
- Scoped production broker with protected policy revision, package SID,
  ACL attestation, process-token evidence, safe result transport and
  immutable user request/Task IDs.
- Immediate revocation / kill-on-ACL-OFF, policy version/Workspace
  change, service restart and machine reboot.
- Integration of actual OS process exit, stdout/stderr, postconditions,
  three-in-one Terminal, Blackboard and Live RCA.
- True live tests in a disposable local Windows instance.

Until all prerequisite checks pass, **never mark `SANDBOX_READY`** and
never silently fall back to the existing unrestricted or Git45 executor.

## How to run Git47 CI tests locally

```bat
set PYTHONPATH=source
python -B -m pytest source\WebAgent\tests\test_git47_webcopilot_result_panel.py -q
python -B -m pytest source\WebAgent\tests\test_tri_one_egress_v10.py -q
python -B -m pytest source\WebAgent\tests\test_git46_turbo_sandbox_contract.py -q
```

For disposable Windows native testing only, run
`install_smart_agent\turbo_appcontainer_disposable_probe.ps1` from
Windows PowerShell with `-FixtureParent` set to a disposable temporary
directory. Do not point it at real projects. This never enables
production Turbo.
