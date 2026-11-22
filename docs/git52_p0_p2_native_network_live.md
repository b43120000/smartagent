# Git52 P0-P2 Native AppContainer Git/Python and internetClient

## Purpose and observed original blockers

Git52 base: 3de1d59df0a418b6687a566bf8f24c4cdfeb2bd4.

The ACL-OFF comparison demonstrated that the normal Executor could launch the
private Git/Python binaries and run Git HTTPS, while Native AppContainer child
processes returned nonzero even for version queries. The earlier Native Broker
did NOT include private Git/Python PATH directories and passed zero capability
SIDs to Windows. The exact version-query OS error has not been established from
the old RCA; this work addresses those independent configuration defects.

## Scope

P0 - Explicit operator enrollment, never Planner self-escalation:

- Elevated operator enrollment requires an exact Git HEAD SHA, an active
  ACL-ON/Turbo-ON protected activation, and the same global Broker mutex.
- Only two fixed project-local paths are enrolled:
  C:\smartagent\.venv\Scripts\python.exe and
  C:\smartagent\.venv\git\cmd\git.exe (under the chosen ProjectRoot).
- Enroll SHA256 in protected security_profile.json; recheck exact hashes
  before EVERY native action; do NOT inherit host PATH or credential variables.
- Operator grants AppContainer package SID RX (not write) on the dedicated
  private .venv and project root traversal; no arbitrary other-project writes.
- Under the protected mutex, refresh protected Broker PowerShell/C# copies and
  activation broker_source_sha256 in the protected journal. Existing Turbo
  Root, workspace contents and snapshots remain untouched.
- Any mismatch or missing prerequisite fails closed; in-progress changes to
  protected files are restored from captured byte snapshots where possible.

P1 - Native outbound internetClient:

- CreateProcessW SECURITY_CAPABILITIES contains exactly one enabled capability
  SID: S-1-15-3-1 (internetClient).
- After CreateProcessW and BEFORE ResumeThread, inspect the actual child token
  (TokenIsAppContainer, TokenAppContainerSid, TokenCapabilities).
- Require enabled internetClient; reject unintended privateNetworkClientServer
  or internetClientServer and every token mismatch. Preserve job kill-on-close.
- internetClient is outbound public Internet access, NOT a GitHub domain
  allowlist or an egress firewall. Windows firewall / proxy / TLS can still fail.

P2 - Independent Runtime proof and classified error:

- Per action, native process token internetClient and AppContainer SID attested.
- Existing outside sentinel read denial must remain. Additional WRITE-open
  negative test opens the sentinel for FileAccess.Write WITHOUT writing bytes;
  a successful open fails closed before the Planner's command runs.
- Keep all existing Turbo Root, policy revision, task/action binding, read-only
  evidence vs OS receipt distinction, and no ordinary Executor fallback.
- Expose internet_client_token_verified, kernel_outside_write_denied,
  native_toolchain_status, native_trusted_tools and native_error_class in the
  bounded, redacted RCA export.

## Updating and performing one-time enrollment

1. Obtain the final commit SHA for this branch. In a normal operator terminal,
   fetch and switch the project's local checkout to that exact SHA.
2. Activate ACL ON + Turbo ON through the existing operator-controlled flow.
   An ACTIVE_PROVISIONED Native activation is required. Do NOT reinitialize or
   erase an existing populated Turbo Root.
3. From an **elevated operator PowerShell** (NOT a SmartAgent Runtime command),
   invoke:

       powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "C:\smartagent\install_smart_agent\turbo_native_toolchain_prepare.ps1" -ProjectRoot "C:\smartagent" -ExpectedCommit "<FINAL_SHA>"

   The operator script fails if the actual HEAD differs, source Broker files
   are modified, the activation is stale, the security profile cannot be
   validated, or Git/Python private binaries are missing. It does not execute
   Planner code; its host-side git rev-parse/status are explicit operator
   setup checks, never a Runtime Host CMD fallback.

4. Restart SmartAgent to pick up updated Python runtime and protected Broker.

## Native Live request

Only normal SmartAgent Runtime actions; ACL ON, Turbo ON, active workspace
E:\AIspace, Turbo Root E:\AIspace\SmartAgent_Turbo_Test.

Run separately and preserve every ToolResult:

- Get-Command git, python
- git --version
- python --version
- git ls-remote https://github.com/b43120000/test.git HEAD
- python -c "import urllib.request; r=urllib.request.urlopen('https://github.com/robots.txt', timeout=15); print(r.status, len(r.read(1024)))"

Use operation INSPECT for the bounded Python HTTPS observation. Keep separate
Admission and process-launch failure status. All tool calls must go through
the attested WIN32_APPCONTAINER Broker. Do not use Telegram Host CMD, ordinary
Executor or a browser fallback, and do not run Case D during this diagnostic.

Expected OS evidence for any executed Native command:

- sandbox_backend = WIN32_APPCONTAINER
- sandbox_identity_verified = true
- internet_client_token_verified = true
- internet_client_capability_sid = S-1-15-3-1
- kernel_outside_read_denied = true
- kernel_outside_write_denied = true
- native_toolchain_status = PINNED
- native_trusted_tools includes git and python
- command_exit_code and bounded stdout/stderr, plus native_error_class

After the test use collect.bat with its Task/Request IDs, and check
executor_run_commands.txt/json and executor_command_capture_summary.json.
Do not infer Case D PASS from basic network probes.

## Known boundaries

- The Windows virtualenv launcher may depend on a base Python installation
  elsewhere. P0 does NOT silently change any shared Python installation's
  ACL; if a base dependency is denied, collect stderr/Windows Error before
  authorizing a narrowly scoped read-only remediation.
- OS capability proves a network token, not a successful DNS/TLS/HTTPS request.
- Files already protected by an explicit non-inheriting ACL inside the private
  .venv may need separately operator-approved RX enrollment; do not grant
  wildcard write access or mutate unrelated projects.
- No real user Windows AppContainer Live outcome is claimed by CI. The CI
  compiles C# and syntax-checks PowerShell and tests fail-closed receipts only.
