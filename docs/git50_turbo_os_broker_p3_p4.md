# Git50 — ACL ON / Turbo ON: native AppContainer Broker P3/P4

Parent: Git49 `7fd20d4370b0a7959ffff701515d573b5546cc04`.

## Implemented — *limited activation*, not universal Full Access

### Protected Windows execution path

`run_command` runs through the canonical `run_bounded_process` adapter
and then `turbo_os_broker_client.execute_turbo_command` when ALL of the
following hold:

- ACL mode is **ON** and exactly one Turbo Root is requested in the
  workspace policy;
- a separate **administrator-provisioned** AppContainer activation journal
  exists, matches the exact project, workspace, Turbo Root and policy revision;
- its broker script is in the protected
  `localdata/secure/windows_security/turbo_broker_code` subtree and
  its SHA-256 matches the protected activation journal;
- the native PowerShell broker reads the protected mode, workspace policy and
  activation state again under a shared Windows mutex;
- the broker derives and verifies the AppContainer package SID, launches a
  **real AppContainer process** with a kill-on-close Job Object, confirms
  kernel denial of a protected out-of-root sentinel, and rechecks policy
  before reporting the action outcome.

The controller never executes the user's PowerShell script as the host.
The sandbox child receives a fresh allowlisted environment, not host
credentials. The native broker reports real exit code and bounded output.
Captured stdout/stderr are **untrusted command output**, not authority.

The Runtime returns the attested process metadata as structured
`sandbox_evidence` in its Tool Result/Blackboard with Task/Action IDs.
Malformed, missing, expired or inconsistent receipts never trigger host
subprocess fallback.

### Supported typed operations

| Action | Enrolled Turbo root | Outside root / unavailable Broker |
| --- | --- | --- |
| `run_command` via PowerShell adapter | Native AppContainer, per-action OS attestation | REJECTED |
| `create_directory` | Exactly one new child directory | REJECTED |
| `write_file` | UTF-8 file up to 4096 characters / 8192-character script envelope, readback verified | REJECTED |
| `delete_path` on regular file | Native AppContainer, target absence verified | REJECTED |
| `delete_path` on **empty** directory | Native AppContainer, target absence verified | REJECTED |
| `delete_path` recursive | BLOCKED (not independently reparse/race attested) | BLOCKED |
| `begin_file_write`, `write_file_chunk`, `commit_file_write`, `abort_file_write` | BLOCKED until a native adapter exists | BLOCKED |
| Other host-side mutation tools | BLOCKED until a native adapter exists | BLOCKED |

The existing non-Turbo `ACL OFF` Software Guard and `ACL ON / Turbo OFF`
Restricted Executor are unchanged.

### Windows activation constraints

`install_smart_agent/turbo_os_activation.ps1` is an **operator-managed**
provision/revoke utility, requiring an elevated administrator identity.

The current safe activation profile supports **only a previously nonexistent
Turbo Root under an existing, registered, writable NTFS Workspace**. It
refuses an already-populated or existing directory. It does not recursively
apply new DACLs to user files or silently change an existing project.

The root SID grants are accompanied by an appcontainer profile, low-integrity
label, recorded traverse grants, external deny sentinel and protected
activation journal. `ACLstatus.bat off` starts a transition, calls the
revoker and **only** publishes ACL OFF after revocation succeeds. Failed
revocation leaves the mode invalid (fail closed). The user's files are not
deleted by deactivation.

### Independent tests

The `git50-turbo-command-broker.yml` workflow runs:

1. Real disposable Windows AppContainer token and filesystem negative-access
   tests, including native PowerShell command stdout/stderr and nonzero exit.
2. A real provisioned-root Windows test, invoking
   `run_bounded_process` from the SmartAgent Python Runtime, confirming
   native execution and task/action-linked OS evidence.
3. Real typed create/write/delete tests (each independently routed into the
   native AppContainer) and negative tests on sibling paths.
4. New-root revocation during `transitioning_off`, confirming SID ACL
   removal without deleting generated user files; an existing nonempty
   directory cannot be enrolled.
5. Python tests on Windows and Ubuntu for fail-closed broker admission,
   unsupported host subprocess, path-scope/provenance and Planner hints.

All production authorization remains **per-action**. The global
`turbo_effective` is intentionally not promoted solely because a journal
exists; the Planner instead sees `BROKER_ENROLLED` and
`per_action_os_attestation_required=true`.

## Still required for unrestricted existing-root Turbo Full Access

- An independent process/service identity and authentication channel for
  persistent production Broker attestation; the current protected one-shot
  invocation relies on ACL/installation state.
- Existing **nonempty** Turbo Root import and rollback with preservation
  of preexisting ACLs, integrity labels and reparse/hardlink protections.
- Native recursion-safe deletion; all typed chunking/project mutation tools.
- Tested external tool compatibility (notably native Git for Windows and its
  known AppContainer NUL device access issue), network/IPC/registry boundaries.
- Process cancellation, crash recovery under simultaneous ACL OFF and active
  child/grandchild processes, and Windows user-machine Live validation.
- Continued root-outside negative tests for unrelated volumes/locations,
  with no ACL weakening outside the user-selected Root.

**Do not describe this build as unrestricted ACL ON / Turbo ON production
Full Access.** It is a tested, limited OS-isolated execution path with
explicitly constrained activation and tool coverage.

## Safe repository checkout (no reliance on remote tracking configuration)

From the user's existing repo:

```bat
git status --short
git fetch origin git50/turbo-appcontainer-command-broker-20261110
git switch --detach FETCH_HEAD
git rev-parse HEAD
```

This only changes the checked-out Git revision; it does not modify local
Windows ACL or activate the OS Broker. Keep real user roots untouched
until supported activation and Live rollback are confirmed.
