# Git51 — ACL ON Native Turbo auto-enrollment (scoped, fail-closed)

## Intent

Once ACL ON is committed and an explicit Turbo root exists in the shared policy, SmartAgent automatically attempts native AppContainer enrollment. Workspace Manager Apply triggers the same native enrollment after persisting the requested root.

**Not unrestricted Turbo OS Full-Access.** Git50's native broker supports a constrained set of per-action commands; reporting global Turbo Effective ON would be false without continuous, independent attestation and support for all mutation types.

## Safe conditions

- Windows ACL mode is explicitly `on`, matched to this installation.
- Exactly one Turbo root is requested, strictly within exactly one registered writable workspace.
- The Root **must not already exist**: importing an existing project would require preserving its per-entry DACLs, integrity labels, hardlink/reparse conditions and rollback proof.
- Native activation writes a protected journal, deploys a protected broker code copy and verifies a real AppContainer identity, a Job Object, successful in-root write and out-of-root kernel denial.
- An existing matching enrollment is reused only if root, installation and policy revision still match. Stale journals are **not** silently overwritten.
- Every native action still performs independent token, scope, and kernel-denial checks. It cannot fall back to the host executor.
- `ACLstatus.bat off` revokes AppContainer grants before committing OFF. It does not delete user output.

## Operator-visible outcomes

- `ACL ON PASS` means normal ACL postconditions passed; when Turbo is configured, native auto-enrollment is additionally attempted.
- `TURBO_AUTOSTART_EXISTING_ROOT_IMPORT_UNSUPPORTED` means the existing root is untouched; the requested Turbo preference remains unfulfilled.
- `Turbo OS Native Broker: ENROLLED (per-action attestation required)` is advisory, not production Full Access.
- `Turbo Effective: OFF` and `Turbo OS Full-Access: NOT ENABLED` stay truthful until a separately tested, unrestricted broker exists.

## Known limitations to finish full scope

1. Transactional existing populated-root import, per-entry DACL/integrity-label backup and complete rollback, rejecting reparse/hardlink escapes.
2. All typed file operations, recursive safe delete, chunked writes, subprocess/child restrictions and network/registry boundaries.
3. Production token/service identity and continuous native attestation, then reliable ON/OFF status publication.
4. Live testing of this installation's existing `E:\AIspace\SmartAgent_GoalDriven_IT_GH_v1` directory without modifying prior files.

## Verification

Windows CI `git51-acl-real-deployment.yml` checks ACL ON/OFF and native fresh-root auto enrollment. `validate_git50_turbo_activation.ps1` also verifies actual native command execution, kernel-denied out-of-root access, revocation, and rejection of a populated existing root.

Do NOT set `turbo_effective=ON` or bypass `SANDBOX_UNAVAILABLE` as a substitute for these postconditions.

## Existing Root Scheme B — NTFS preflight and protected snapshot (phase 1)

This phase is **not a production existing-root import**. No AppContainer
grant is applied to existing files; no full-access status is asserted.

- `install_smart_agent/turbo_existing_root_preflight.ps1` is read-only,
  enumerates the bounded NTFS tree, records stable volume/file IDs, detects
  hardlinks and reparse components, and checks whether owner, group, DACL and
  SACL/mandatory label information can be captured.
- Ordinary preflight output suppresses sensitive SDDL. An explicitly privileged
  caller can opt into `-IncludeSensitiveInventory` for an in-memory snapshot.
- `install_smart_agent/turbo_existing_root_snapshot.ps1` requires an elevated
  administrator and an installation-bound ACL ON mode plus a matching Turbo
  policy. It writes a **CreateNew** immutable JSON snapshot into the
  admin/SYSTEM-only `localdata/secure/windows_security/turbo_existing_root_snapshots`.
  Snapshot status is `PREPARED_READ_ONLY`, and
  `authorization_to_mutate=false`. Original user data and ACLs are untouched.
- CI validates the snapshot matches original per-file SDDL and file hashes,
  rejection on policy mismatch, no follow of junctions/hardlinks, and no
  unauthorized grant.
- Native OS CI separately verifies that a low-privilege AppContainer may
  **read** an explicitly RX-authorized disposable sibling file but the OS
  **denies writing it**; a different sibling with no RX grant is unreadable.
  This is a scoped fixture, **not a production claim that all external paths
  are readable**.

### Required before existing root Full-Access becomes safe

1. Privileged transaction executor: a durable per-entry mutation journal,
   exclusive workspace operation freeze, file-ID/SDDL revalidation, stop on
   tree changes, deterministic crash recovery and PowerShell error propagation.
2. Native AppContainer ACE/integrity deployment on the existing root's
   individual protected/legacy entries, followed by real kernel
   inside/outside probes. Carefully handle DACL inheritance boundaries.
3. Revocation based on exact managed ACE/label changes and immutable snapshot,
   without deleting user-created files or replacing unrelated ACL updates.
4. Restricted production Executor action-to-Broker routing for all permitted
   commands/child processes; no host-identity fallback, re-attestation
   per operation, supported recursive operations and negative breakout tests.
5. Only when ALL of those tests pass should a truthful `Turbo Effective: ON`
   and `Full-Access: ENABLED` be reported. Otherwise stay fail-closed.

Do not copy these scripts into the workspace or assume their presence alone
grants elevated or AppContainer permissions.

## Scheme B P2 — Tested NTFS ACL transaction and cross-process recovery

The newest experiment, `install_smart_agent/turbo_existing_root_transaction_rehearsal.ps1`, is **strictly CI-only**. It requires the GitHub Runner environment, a unique disposable fixture directory beneath `RUNNER_TEMP`, a fixture marker, an elevated Windows identity, and a protected earlier snapshot. No ordinary operator or SmartAgent runtime workflow may invoke it for a real Workspace.

Windows integration validation in `install_smart_agent/tests/validate_turbo_b_acl_transaction_ci.ps1` proves:

1. An existing 3-node NTFS tree is re-enumerated and compared to its immutable earlier full SDDL and volume/file IDs before any ACE is added. Each node is granted a test AppContainer SID, **child-first**, then restored **parent-first**. No original data bytes or final SDDL change.
2. An injected failure after the first Set-Acl is caught; the pre-mutation journal and in-memory rollback restore the original DACL, owner/group and observed security descriptor.
3. A separate PowerShell process forcibly exits after the first Set-Acl. The parent reads a flushed journal written before that mutation, starts a new process-level recovery action and verifies exact SDDL restoration and no leftover package ACE. A stale revision is independently rejected before changing the tree.
4. Windows PowerShell 5.1 Set-Acl alone can introduce the inherited-control flag `D:AI`; the CI-only native `SetFileSecurityW` DACL restore path is verified to recover the original observed control flags.

The journal is a **recovery rehearsal**, not a finished permanent production transaction mechanism. In particular, there is not yet an OS-enforced workspace writer freeze, race-proof per-handle change tracking, validated integrity-label mutation/restoration across existing files, or a production crash/startup recovery coordinator. Child process isolation and complete Tool/Executor routing remain separate milestones.

Do not relax `TURBO_AUTOSTART_EXISTING_ROOT_IMPORT_UNSUPPORTED`, set `Turbo Effective: ON`, or run the CI transaction script against a real user directory because of these results. It will intentionally refuse.


## Scheme B P3 — Production candidate-only live check (no existing-root mutation)

This is the **handoff checkpoint** after the administrator has already observed
\`TURBO_B_EXISTING_ROOT_PREFLIGHT_PASS entries=68\` on their real Windows NTFS
existing project. **No production AppContainer ACE, integrity-label change,
existing-root import, policy switch, or Full-Access enablement is performed.**

The new, narrowly scoped entry point is
\`install_smart_agent/turbo_existing_root_prepare.ps1\`, invoked in a trusted
elevated Windows PowerShell terminal. It supports:

- \`-Action Prepare\`: requires the matching installation ACL mode to be ON,
  verifies a registered writable Workspace and strictly nested, existing
  candidate Root; refuses overlap with Read-only/denied paths; runs the existing
  NTFS preflight including hardlink/reparse and full SDDL capture; writes an
  immutable, admin/SYSTEM-only \`PREPARED_READ_ONLY\` candidate snapshot; and
  independently verifies it before returning PASS.
- \`-Action Verify -SnapshotId <32 lowercase hexadecimal chars>\`: loads only
  a snapshot stored inside this installation's protected snapshot directory,
  checks schema, install/workspace/root identity, \`candidate_only=true\`,
  \`authorization_to_mutate=false\`, Policy Revision, file paths, entry count,
  stable file IDs, full SDDL and directory/file types. Changes to tree
  membership, security descriptors or policy revision require new preparation.
- Status \`P3_BROKER_BLOCKER\` indicates whether a separately enrolled native
  AppContainer currently exists (possibly pointing to a *different* Root).
  This is an ADVISORY next-stage blocker: Prepare still succeeds because it
  never changes existing grants or records.
- A separate production root that is not yet requested by Edit Workspace MAY
  be snapshotted as a **candidate**, without editing the active Turbo policy.
  The normal Edit Workspace existing-root refusal remains in force.

### Operator check (PowerShell, elevated)

First update the Git51 installation to the approved commit. Do not switch
versions while an ACL operation is running; verify the local Git SHA separately.

\`\`\`powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\smartagent\install_smart_agent\turbo_existing_root_prepare.ps1" -Action Prepare -ProjectRoot "C:\smartagent" -Workspace "E:\AIspace" -TurboRoot "E:\AIspace\SmartAgent_GoalDriven_IT_GH_v1"
\`\`\`

Expected on the real machine, if the 68 entries have not changed:

\`\`\`text
TURBO_B_EXISTING_ROOT_PREFLIGHT_PASS entries=68
TURBO_B_PROTECTED_EXISTING_ROOT_SNAPSHOT_PASS entries=68
TURBO_B_P3_VERIFY_PASS entries=68
P3_SNAPSHOT_ID=<new 32-hex ID>
P3_SNAPSHOT_SHA256=<64-hex SHA256>
P3_POLICY_REVISION=<live revision>
P3_BROKER_BLOCKER=NONE|OTHER_TURBO_ROOT_ACTIVE_REQUIRES_VERIFIED_REVOCATION|...
P3_IMPORT_MUTATION=DISABLED
P3_FULL_ACCESS=NOT_ENABLED
TURBO_B_P3_PREPARE_PASS
P3_LIVE_CHECKPOINT=EXISTING_ROOT_SNAPSHOT_VERIFIED_NO_MUTATION
\`\`\`

Verify independently (paste the emitted P3_SNAPSHOT_ID):

\`\`\`powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\smartagent\install_smart_agent\turbo_existing_root_prepare.ps1" -Action Verify -ProjectRoot "C:\smartagent" -Workspace "E:\AIspace" -TurboRoot "E:\AIspace\SmartAgent_GoalDriven_IT_GH_v1" -SnapshotId "<new 32-hex ID>"
\`\`\`

If the live tree or ACL changed in between, Verify MUST fail closed. The
snapshot deliberately stores **ACL/File-ID metadata**, not file-content hashes;
this check does not detect content-only file changes. If the preparer sees a
previous AppContainer enrollment on another root, do **not** delete the
journal, existing directory or AppContainer profile manually.

Windows CI uses a populated disposable NTFS tree to test successful Prepare
while Turbo requested is OFF, independent Verify, new-file invalidation,
policy-revision invalidation, root-overlap refusal, unchanged SHA256 file data
and SDDL, and a separately requested different Turbo Root.

### NOT YET SUPPORTED

Protected read-only snapshot is not a backup of user-file contents. Production
\`Existing Root Import\` and a fully transactional ACL/mandatory-integrity
mutator with Startup/ACL OFF recovery are still absent. The old CI-only
transaction rehearsal cannot safely be run against user data. Do not assert
\`Turbo Effective: ON\` until full isolation and action routing pass.
