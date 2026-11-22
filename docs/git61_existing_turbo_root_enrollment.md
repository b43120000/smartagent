# Git61 — Reuse an Existing Turbo Root (Native AppContainer)

Base: `4c0e81de568b99d419c28c30bf51068af2558857`
Branch: `git61`

## What changes

Windows `Edit_workspace.bat` -> Security -> **Add turbo mode** now accepts an existing
directory that is a strict child of an authorized writable Workspace.
It does NOT create a new replacement directory, rename an existing project,
replace any user file, or automatically approve arbitrary commands.

The **Apply** step distinguishes these cases:

1. **Fresh, non-existing child** — keep the original `turbo_auto_activate.ps1`
   workflow and native fresh-root enrollment.
2. **Existing directory, no active Broker** — require a locally elevated
   Workspace Manager, exact typed confirmation of the target path, then invoke
   `turbo_existing_root_activate.ps1`. The script independently checks
   the current ACL mode, workspace/Root policy identity, and absence of active
   or pending Broker transitions. It snapshots original NTFS security via
   the existing candidate-only snapshot writer, re-verifies every File ID and
   SDDL, and invokes the P3-D3 importer with an exact operator confirmation.
3. **Existing active Broker** — do not re-run Configure Security, do not rewrite
   Policy revisions, and do not re-enroll or overwrite the journal.
   Switching to another Root still uses the separately reviewed
   `turbo_existing_root_switch.ps1` workflow; Manager Apply fails closed.
4. **`turbomode=...` sent from Telegram/WebGPT** — records the requested
   policy only. **No** NTFS changes and **no** direct importer launch.
   A local elevated operator must finish the enrollment.
5. **ACL OFF** — no Turbo native grant; ACL OFF semantics are unchanged.

Existing-root enrollment adds a Package SID to existing per-entry NTFS DACLs
and adjusts mandatory integrity labels. These **security metadata changes are
intentional** and necessary for AppContainer operations. Original file contents
and directory layout are preserved, not the live ACL values. Protected snapshots
and the existing exact-SDDL/IL revoker restore original rights on Revoke/Recover.

## Safety rules

- Root must exist and be a directory; strict descendant of exactly one
  authorized writable Workspace, never the Workspace itself, installation
  directory, read-only/denied area, symlink, junction, reparse tree or
  hardlink escape.
- P3-D3 protected snapshot includes File IDs and security descriptors;
  any concurrent change fails closed.
- A fresh Import and any existing-Broker switch cannot race: they share the
  same global installation mutex and abort on pending protected journals.
- Exact path confirmation is required twice: from the Manager operator and
  by the elevated native importer. A saved policy cannot satisfy this.
- An interrupted import/switch remains BLOCKED until authenticated Recovery.
  The Manager does not blindly remove protected journals or fall back to host
  execution. The native Broker re-attests OS isolation per action.
- Multiple registered Turbo roots do NOT mean multiple active AppContainers.
  Native execution currently supports one active root per installation.

## Verification

Git61 Windows CI:

- Parse Python Workspace Manager and every modified native PowerShell script.
- Validate wrong confirmation and missing authorization are rejected before
  any AppContainer activation.
- Enroll a populated disposable project, confirm original content SHA256 and
  native `ACTIVE_PROVISIONED` journal.
- Reject duplicate enrollment while an active Broker exists.
- Revoke and assert the exact original per-entry SDDL and file hash are
  restored and that the original root still exists.
- Run existing D3 importer/revocation and Broker-switch regressions.

### Operational note

`TURBO_GIT61_EXISTING_ROOT_NATIVE_ENROLLED_PASS` denotes native
enrollment, **not** global Turbo Effective ON or a particular child command
being verified. Each real `run_command` still requires per-action
`sandbox_identity_verified`, `kernel_outside_read_denied`,
exit code and Runtime ToolResult evidence.

No Windows live verification is implied by a green parser test.
