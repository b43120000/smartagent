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


## Git47 extended regression evidence and architecture decision (2026-11-10)

Windows GitHub Actions run `42778342146` finished SUCCESS on all three
jobs (Windows WebCopilot/Git46 contracts, Ubuntu WebCopilot/Git46 contracts,
Windows disposable AppContainer fixture). Additional strict Windows fixture
cases verified:

- Other-volume *disposable* read/write denials: actual Win32 error 5.
- CMD absolute-path out-of-root read: OS-denied stderr and no success path.
- Windows PowerShell absolute-path read/write: UnauthorizedAccessException,
  observed inside AppContainer; no external sentinel mutation.
- Python 3.13 copied to a read/execute-only dependency subtree: start PASS,
  file `open()` out-of-root PermissionError PASS, subprocess out-of-root
  PermissionError PASS, in-root overwrite PASS, in-root recursive delete PASS,
  and outside absolute-path write blocked without modifying a sibling sentinel.
- Native child process token inheritance, root-object delete denial,
  cross-project hardlink and Junction probes, and non-inheritance of a host
  secret environment variable PASS.

The native `NUL` device probe returned OS Win32 error **5**, not successful
access. Git for Windows 2.56 (using the new `ucrt64` layout) exits **128**
because it tries to open `/dev/null` for read and write. This is an
AppContainer device/object namespace boundary, *not* a root NTFS DACL issue.
It must **not** be fixed by granting wider file ACLs or disabling AppContainer.

The pure-Python Git-format implementation **Dulwich** was added as a
controlled alternative test in the same disposable fixture; this is not
identical to arbitrary native `git.exe` execution. Test output is
`NOT_VERIFIED` unless actual clone/postcondition PASS is recorded. No
GitHub network clone, TLS, remote Git feature compatibility or network
capability has been attested.

The CI writes
`SMARTAGENT_GIT47_WINDOWS_DISPOSABLE_SANDBOX_EVIDENCE_V1` as
`git47_sandbox_ci_evidence.json`, retaining a per-probe action ID,
scope verdict, Win32 result, OS-denied boolean and evidence reference.
It does not contain raw commands, source secrets, or test fixture paths.
The GitHub workflow publishes this as a test-only artifact when available.
An artifact with `file_boundary_verdict=PASS` **does not mean**
`production_turbo_effective=ON`.

`ACLstatus.bat status` is now a read-only shared resolver display, and
`ACLstatus.bat on/off` displays the requested/effective reason after
a successful transition. A missing Python interpreter reports an unavailable
status instead of guessing enabled mode.

### Important remaining production guarantees

The production `turbo_sandbox.resolve_turbo_state()` remains
`sandbox_status=NOT_READY`, `turbo_effective=OFF`: there is **no
authenticated production AppContainer Broker** and therefore no safe path
from Planner actions to a real Turbo process yet. There is no validated
process lease bound to workspace policy revision, or ACL-OFF job
revocation; no production root provisioning/attestation or removal
workflow; no network policy for GitHub clones; no C++ compiler/IPC
attestation; no OS proof of all reparse/hardlink/open-handle races.
AppContainer cannot be marked fully usable merely because Python and
PowerShell work in disposable CI.

**Release verdict:** WebCopilot result UI = UNIT/CI PASS, Live NOT RUN.
Native Windows AppContainer file boundary = disposable fixture CI PASS.
Git for Windows = NOT VERIFIED (NUL OS denied).
Production Turbo Full-Access = BLOCKED / NOT IMPLEMENTED.

### Confirmed later test results

The later Windows CI run `42778811440` confirmed
`python_dulwich_local_git_clone` **PASS**: a Git-format repository was
seeded entirely inside the disposable fixture, cloned inside AppContainer by
Python 3.13 plus read/execute-only Dulwich, and verified by file contents.
This is a real local Git-compatible clone, not a simulation. Native Git for
Windows remains unverified due kernel-denied NUL device.

Run `42778925043` at source SHA `03906770b2977b4b1cb5b490f1442f5570fbaaa6`
completed all three Git47 CI jobs with `success`.
Its Windows job also published `git47-windows-os-sandbox-fixture-evidence`
as a sanitized typed JSON artifact.

Strictly separate:
- Disposable OS file boundary PASS; Python and Dulwich local clone PASS.
- Native Windows Git.exe capability FAIL/UNVERIFIED; GitHub HTTPS clone NOT RUN.
- Production Broker/ACL revocation/lease and live WebCopilot DOM NOT RUN.
- Production Turbo Effective is still OFF; this test must never promote READY.

