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
