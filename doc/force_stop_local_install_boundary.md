# ForceStop local-install boundary / missing historical workspace

## Incident
A locally recorded PAUSED task, TASK-519592C38B0CB1DDCE21, refers to
E:\SmartAgent_GoalDriven_IT_GH_v1, which no longer exists. Previously,
force_stop_cleanup._plan() raised force_stop_workspace_unavailable and returned
work_cleanup.success=false, so ForceStop ended with code 2 and prevented a
reconnect.

## New contract
ForceStop invoked from a SmartAgent checkout may touch only that checkout's
installed resources and process identities. Incomplete TaskRecords stored
in THIS installation's durable queue are still archived and cancelled,
regardless of their obsolete/external project workspace. Task workspace is
not evidence of Agent-install ownership; it does not authorize accessing
that project.

- Never scan/archive/modify chunked-write folders, approvals or other
  workspace data outside the checkout. No external workspace filesystem
  probing in ForceStop planning.
- A deleted workspace is treated as unavailable-for-writer-inspection, NOT
  as a reason to prevent local TaskRecord cancellation.
- Workspace directories genuinely inside the checkout keep full writer
  integrity checks, staged archive and abort semantics. Unknown unowned
  writers there still block cleanup as before.
- Records with explicit foreign install_root are skipped unchanged, even
  if an old shared state file includes them.
- A historical worker_pid reused by an unrelated process is not proof of
  ownership. Only an inventoried Agent with exact checkout provenance can
  block cleanup; it cannot authorize killing an unrelated PID.
- CLI process selection now requires the executable under checkout,
  explicit matching --root, or rooted Agent source/script. A foreign
  --root overrides incidental interpreter or workspace path matches.
- Kill exact proven Agent PIDs with taskkill /PID /F, NOT /T. No blanket
  descendant traversal and no generic foreign launcher ancestor selection.
- After archive/cancel, remote_clean_start(local_checkout_only=True)
  clears only checkout-local Runtime leases; no extra task abandonment,
  metadata-PID killing, or external workspace SecurityApprovalLedger access.
- No deletion, moving, install cleanup, or rollback is performed on E:\ or
  on any other SmartAgent installation.

## Tests
Native sandbox scenarios include deleted E: PAUSED task, external existing
workspace not inspected, two separate checkouts, mixed-store foreign
install_root, old reused PID, a foreign Agent whose --workspace names
the local checkout, no recursive taskkill, and unchanged local writer
fail-closed behavior. CI runs Python 3.13 on Windows and Ubuntu and checks
the protocol manifest SHA-256 values.

## Live acceptance
Switch to the CI PASS commit; run ForceStop from the checkout containing
force_stop_all_agents.bat. Confirm exit=0, local missing-workspace TaskRecord
archived and CANCELLED, and other installed SmartAgent checkout files and
processes untouched. A full reinstall is not necessary. An independent
live check remains necessary: unit tests are not actual Windows process stops.
