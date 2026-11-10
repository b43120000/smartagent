# Git49 — ACL ON + Turbo ON: OS Broker foundation and required next boundary

Base: Git48 commit 884c2c02c430501bb38a82465fcf52939ff3e68f.

## Current tested state (P0/P1)

- Shared admission: source/agent_core/turbo_broker_admission.py
- Host command interception: source/agent_core/bounded_process.py
- Typed mutator interception: source/agent_core/tools.py
- Immutable, non-authorizing per-action scope contract:
  source/agent_core/turbo_broker_scope.py
- Windows AppContainer proof: existing
  install_smart_agent/turbo_appcontainer_probe.cs,
  install_smart_agent/turbo_win32_file_probe.cs and the
  install_smart_agent/turbo_appcontainer_disposable_probe.ps1 disposable
  fixture; the Git49 Actions workflow runs that real Windows OS test.
- CI: .github/workflows/git49-turbo-os-broker.yml
- Tests: source/WebAgent/tests/test_git49_turbo_broker_admission.py
  and source/WebAgent/tests/test_git49_turbo_broker_scope.py.

## Execution state matrix

| ACL | Turbo requested | Actual mode | Current result |
| --- | --- | --- | --- |
| OFF | OFF | Software Guard | Normal guard and approvals |
| OFF | ON (stale) | Software Guard | Turbo suppressed, normal guard and approvals |
| ON | OFF | ACL Restricted Executor | Existing security controls |
| ON | ON | OS broker required | BLOCKED until production OS authority verified |

Successful disposable Windows AppContainer tests prove actual kernel-level
denials **in the disposable fixture**, including a real AppContainer token,
a kill-on-close Job Object, direct Win32 file API and child processes.
They do not establish authorization on a real user Turbo Root.

## What must exist to enable production

P2 — Protected broker identity and lease lifecycle:
- A privileged OS-owned broker service (not model-writable process data).
- Atomic binding to the active installation, Task/Action ID, exact Workspace,
  Turbo Root, revision and active ACL state; refuse stale leases.
- Ephemeral AppContainer SID lifecycle, token validation *before* execution,
  Job Object, cancellation, timeout and process-tree kill.
- Crash-safe per-root NTFS DACL + integrity-label provisioning with exact
  restoration, and explicit ACL OFF revocation **before** mode transition.
- Entire selected existing root subtree and metadata checked for reparse
  paths, hardlinks, protected child DACLs, and TOCTOU changes. Never weaken
  parent/sibling or unrelated project DACLs.

P3 — A broker-owned execution path for script/binary operations:
- Intercept and route *all* mutating Runtime tools through the protected
  broker, retaining normal Task Result, evidence and approval semantics.
- Kernel negative probes against parent, sibling, unrelated drive, reparse
  escapes and spawned children; no software-only denial accepted as proof.
- Dependencies supplied as separately protected RX-only roots: Python,
  PowerShell, CMD, compiler and Git. Native Git for Windows hit NUL device
  access denied in Git47 and remains unverified for full operations.
- Network/credentials/IPC/service/registry restrictions and explicit tests.

P4 — Live authorization:
- Persistent revocation record and startup recovery must be proven.
- Windows production Live tests must verify active ACL ON + Turbo ON
  and immediate, irreversible OS-level permission removal on ACL OFF.
- Only an **independent authenticated OS attestation** may make
  turbo_effective=ON. JSON from a task or a test artifact must not.

## Fail-closed behavior

Git49 does not implement an authenticated production OS broker. It
**never** promotes sandbox_status=READY or turbo_effective=ON from a
Planner value or CI fixture. Instead, mutators return
TURBO_OS_BROKER_BLOCKED with executed=false and privacy-safe
sandbox_evidence. Read-only diagnostic tools remain available.

The scope request stores policy revision, Task/Action, root and cwd,
rejects traversal, reparse paths and stale revisions. Its SHA256 field
detects accidental changes but is **not** a signature, secret, capability
or permission grant.

## Safe Windows status check

    cd /d C:\Users\user\Desktop\remoteagent\smartagent
    ACLstatus.bat status

Expected under ACL ON + requested Turbo ON during Git49 P1:
Turbo Requested: ON
Turbo Effective: OFF
Sandbox Status: NOT_READY

This is a deliberate blocked state, not production Turbo functionality.
