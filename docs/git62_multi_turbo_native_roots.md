# Git62 — independent Native Turbo Roots

Base commit: 3db801b63aacf90fae092bec0455bcd12666e61c
Implementation branch: git62

## Operator intent

The shared Edit Workspace / Telegram WebGPT / Workspace registry can nominate
several distinct Turbo Roots, each below precisely one writable Workspace.
For example:

- W1 E:\AIspace; T1 E:\AIspace\SmartAgent_Turbo_Live_01
- W2 C:\Users\user\Desktop; T2 C:\Users\user\Desktop\remoteagent

One natural-language action bound to a T1 working directory is executed by
the T1 AppContainer; an action bound to T2 is executed by the T2 AppContainer.
The command input MUST specify or be unambiguously bound to an actual directory
inside the selected Root. With 2+ roots, a bare Workspace parent or absent
working directory MUST NOT mean "choose the first Root."

The intended security model is **one distinct AppContainer Package SID per Root**,
not one process capable of writing every configured Root. Each Root uses a
protected activation and (for populated existing directories) import journal:
secure/windows_security/turbo_root_activations/
  activation-<sha256(root path uppercase)>.json
  import-<sha256(root path uppercase)>.json

These indexes are not authorization tokens. All requests continue to verify
ACL mode ON, a currently registered root, revision, Package SID, protected
Broker source hash, kernel outside-root and cross-root access denial, and the
actual spawned process token on every operation.

## Operator workflow

1. Open Edit Workspace with administrator privileges and select Security /
   Workspace Access.
2. Keep every parent Writable Workspace registered, add each desired existing
   Turbo directory with **Add turbo mode**.
3. Select Apply and type APPLY to explicitly authorize NTFS metadata changes.
   The Manager calls the elevated existing-root snapshot/import flow separately
   for each Root. No existing project directory or contents are replaced.
4. Read each Root's Native enrollment status and perform a scoped Runtime
   command specifying working_directory INSIDE that Root.
5. To remove an OS-enrolled Root, use Edit Workspace Apply, which revokes the
   selected SID before removing the Root from the policy. An unprivileged
   Telegram setting is never a substitute for OS revocation.

For Telegram WebGPT/Workspace updates:
- turbomode=C:\Users\user\Desktop\remoteagent : add one registered root.
- turbomode=E:\AIspace\SmartAgent_Turbo_Live_01;C:\Users\user\Desktop\remoteagent :
  supply the exact list for the selected Workspace (every path must be under
  that single Workspace; use separate forms for separate Workspaces).
- turbomode=-C:\Users\user\Desktop\remoteagent : remove from the registry
  ONLY if no protected Native activation for this Root is still present.
- turbomode=off : unregister the selected Workspace's non-enrolled roots.
Registration alone does NOT grant OS access. An administrator must complete
Native activation in Edit Workspace before runtime dispatch becomes possible.

## Critical safety invariants

- No overlapping or nested Turbo Roots.
- A path must match exactly one registered writable Workspace and Root.
- No SID/moniker is ever selected from the Planner's text.
- Root-scoped Native tool execution has no host/ordinary-Executor fallback.
- Unknown, stale or partial Journal: BLOCKED, not success.
- A protected import journal commits before an independently readable
  activation receipt marks existing_import_committed=true.
- Shared Workspace ancestor traverse ACEs are revoked only for the target SID,
  without erasing another active AppContainer.
- ACL OFF revokes every independent Root; if any revocation fails, Native OS
  rights must not be presented as fully revoked.
- Status requested=ON and globally effective=OFF is not an execution verdict.
  Only per-action native token and kernel negative probe receipts establish
  executed/verified status.

## CI acceptance

Git62 workflow runs isolated Python routing/manager/WebGPT tests and an
elevated disposable Windows real OS integration. Windows must enroll TWO
existing populated Roots with distinct Package SIDs, run a protected Broker
command inside EACH Root, reject cross-root write, revoke A while preserving
B's operation, revoke ALL, and verify original file hashes and NTFS SDDL.

This document describes intended behavior. PASS must be taken from matching
GitHub Actions Windows job evidence, not inferred from source code or
the existence of an activation file.
