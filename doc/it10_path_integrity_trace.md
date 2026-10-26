# Opt-in IT10 Path Integrity Trace

This diagnostic hashes the actual values at the Sender, Telegram adapter,
Gateway, Task Store, Worker, protocol request, Composer, rendered user turn,
Planner response, request context, expanded action, fact tool, terminal and
local-outbox boundaries. It does not repair paths, grant authorization, execute
extra tools, recover writers, or create acceptance receipts.

All of these conditions are required:

```text
SMARTAGENT_IT10_PATH_TRACE=1
SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED=YES
SMARTAGENT_IT10_PATH_TRACE_RUN_ID=<fresh 8-80 character identifier>
SMARTAGENT_IT10_PATH_TRACE_ROOT=<existing absolute private directory>
```

The request must contain both exact diagnostic marker lines:

```text
NLIT_CASE_ID=IT10
NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E
```

Leave the environment flag off in normal operation. Configure the private
directory's ACL before acknowledging it. The logger does not change ACLs.
Use a fresh run ID for each diagnostic session. A directory inside the release
package is permitted only under its ignored `localdata` directory. Private
trace filenames and `it10_path_trace_private` directories are also ignored.
Never stage raw traces or reports containing private conversation data.

Each process appends its own `it10-path-<trace>-<pid>.jsonl` file. The trace ID
joins records created before a Request/Task ID exists to later records with
those IDs; empty early identity fields mean not yet assigned, not a fabricated
identity. The Composer helper carries this diagnostic context explicitly.

Raw UTF-8 hashes and lengths, comparison hashes, and literal path codepoints
are distinct fields. Comparison hashes normalize whitespace and are not raw
byte-integrity proofs. Only opted-in test request fragments are retained, up
to 8,000 characters with an explicit truncation flag. Full Planner responses
and injected toolkits are hashed, not stored. Selected Telegram entity data
contains only type, offset and length; user objects, URLs and credentials are
excluded. Delivery retries for foreign Request/Task IDs are not recorded.

Trace I/O errors are best effort and never change execution or admission.
Missing stages must be reported as missing; they are not a PASS. A Renderer
observation must not be substituted for the original Telegram message text.

Offline tests:

```text
PYTHONPATH=source python -B source/WebAgent/tests/validate_it10_path_integrity.py
```

These tests use temporary stores and memory-only synthetic updates, not a real
Planner. A separate Live test must use the shipped `send_and_wait()` path and
retain actual Worker provenance, Runtime results, terminal receipt and outbox.
Do not clear a quarantined writer to run that test. A local simulation does not
verify the real Telegram client, clipboard or Telegram API input path.


## A/B/C evidence hardening

For **LOCAL_TEST** only, the trusted Gateway stores a short-lived, validated
diagnostic handoff in the local Task Store when the sender has opted in.
The Supervisor WorkerLauncher validates the request-bound trace ID, private
directory and freshness before passing the four IT10 trace variables to the
new Worker. The Composer helper inherits that environment and restores the
trace context. No user-supplied path or message content can activate a handoff.
For **real Telegram**, configure the four variables in the Supervisor process
**before startup**; the normal Worker inherits the Supervisor environment.

The Worker writes a reason-only `it10_trace_gate` Runtime Journal event. The
Supervisor writes `IT10_TRACE_WORKER_DISPATCH`; both indicate whether the
diagnostic was enabled. Absence of a Worker trace still means NOT_VERIFIED.
Trace evidence remains local and ignored by Git. No automatic retry or
path repair is introduced.

The Composer Writer/Pre-submit Journal now saves an independent Adapter
`composer_validation` object with `matched`, `matched_view`, expected
and observed lengths, and SHA-256 values. A separate
`composer_strict_validation` debug event records the send gate. Neither
contains raw prompt text. The normal verification method still decides
whether submission is safe.

The Supervisor and Worker Runtime Logs now include
`source_load_attestation` with module disk hashes, **live code-object**
fingerprints, and an explicitly labelled disk-only Git HEAD. These records
are captured at dispatch and immediately before Planner execution. They
support future provenance audits but do not retroactively attest historical
processes or authorize Runtime operations.

`config/debug_config.json` is deliberately .gitignored. The committed
`config/debug_config.defaults.json` defines
`composer_verification.unsafe_force_pass=false` as the shipped security
default; it is **not** an automatic overwrite of local settings. On deployment
Codex must update only the existing private local key to false, preserving
all other keys, and verify the actual reader returns false.

Offline regressions, before any new live Telegram/local task:

```text
PYTHONPATH=source python -B source/WebAgent/tests/validate_it10_path_integrity.py
PYTHONPATH=source python -B source/WebAgent/tests/validate_it10_evidence_gates.py
PYTHONPATH=source python -B source/WebAgent/tests/validate_unsafe_composer_verification_bypass.py
```

A local simulation covers LOCAL_TEST only. Real Telegram input requires a
separate human-triggered task and its own new Request/Task IDs.
