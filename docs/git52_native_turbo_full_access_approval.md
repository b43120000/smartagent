# Git52 — Scoped Full-Access Command Confirmation for Native Broker

## Scope

Base: \`bbf8fc26e66d9a7a6cf622c32e7efe2875298b5f\`.

This change eliminates the *secondary UNKNOWN-command human-approval popup*
only for Runtime-admitted commands whose Turbo OS Broker gate returns
\`BROKER_REQUIRED\` with ACL ON and Turbo requested ON, an active protected
broker enrollment, a validated policy revision, a working directory inside
the enrolled Turbo Root (or the approved workspace parent that the native
client canonically remaps to that Root), no reparse-path traversal, and the
existing hard command-security rules satisfied.

The change does **not**:
- set global \`turbo_effective=ON\` or \`global_production_full_access_attested=true\`
- bypass normal action/argument/success-criteria admission
- make unverified \`UNKNOWN\` commands \`PROVEN_READ\`
- allow outside-root filesystem mutations or sandbox escape
- weaken protected-broker activation checks, runtime SHA256 verification,
  Windows AppContainer identity checks, Job Object checks or the kernel
  outside-access negative proof
- execute user commands on Telegram Host CMD or the ordinary host Executor
- change ACL OFF or Turbo OFF behavior

## Four-mode behavior

| ACL | Turbo requested | Approval behavior |
| --- | --- | --- |
| OFF | OFF/ON | Ordinary software guard + current unknown approval |
| ON | OFF | Ordinary executor and current unknown approval |
| ON | ON; broker unavailable | BLOCKED, no host fallback |
| ON | ON; broker enrolled, scoped command | No secondary UNKNOWN consent; the Native Broker must still reattest each command |
| ON | ON; broker enrolled, outside cwd or hard-banned | BLOCKED before process execution |

The authorization check is fail-closed and records a \`TURBO_DECISION\`
and, where applicable, \`TURBO_AUTO_ADMITTED\` audit event. The term "auto"
refers to user-confirmation UX only; it is **not** an OS-granted privilege.

## Windows validation

Run the Git52 pytest file plus Git32/Git49/Git50 regression suites in an
isolated testing worktree, not from the deployed project state.

For a Native OS Live Test, use the *normal SmartAgent Runtime* with the
enrolled Turbo Root. Perform distinct \`run_command\` actions: create a new
temporary file, verify exact UTF-8 bytes, delete it, and independently
verify nonexistence. Confirm no Telegram unknown-risk approval appeared.

For every command, require actual Runtime Evidence:
\`sandbox_backend=WIN32_APPCONTAINER\`,
\`sandbox_identity_verified=true\`,
\`kernel_outside_read_denied=true\`, and an appropriate
\`command_exit_code\`. Also test that a write target outside the Turbo Root
is denied by OS enforcement without modifying the outside file. Do not
claim production-wide attestation from a successful one-off test.
