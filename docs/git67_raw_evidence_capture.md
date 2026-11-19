# Git67 — Byte-exact IT10 evidence (sidecar only, no behavior repair)

## Observed boundaries

- 01b_telegram_http_body: untouched urllib HTTP response-body bytes, captured BEFORE UTF-8 and JSON decoding. Not phone sender bytes and not the TLS wire stream.
- 02_telegram_message_text / 03_receiver_normalized / 04_gateway_input / 06_worker_input / 07_loop_input / 08_user_request_block / 09_composer_input / 09_composer_primary / 09_composer_semantic / 10_submitted_* / 11_planner_response / 11_parser_input / 12_request_context / 13_field_refs_expanded: exact UTF-8 encoding of the Python string at each boundary. This is not original HTTP bytes and is labeled UTF8_FROM_PYTHON_STR.
- 12_request_context and 13_field_refs_expanded: observe the CTX pointer and resolved field value. A diagnostic raw_blob_ref always references a separately saved exact .bin file, not just an identifier.
- Phone-side sender bytes are UNOBSERVED. A Telegram mixed getUpdates batch is intentionally UNOBSERVED at the raw HTTP boundary, since it might include others' messages.

The official Runtime, Parser and ACL/Turbo semantics remain unchanged.

## Explicit local opt-in

Set all environment variables BEFORE starting Supervisor and Live Worker:

    SMARTAGENT_IT10_PATH_TRACE=1
    SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED=YES
    SMARTAGENT_IT10_PATH_TRACE_RAW=YES
    SMARTAGENT_IT10_PATH_TRACE_RUN_ID=GIT67-RAW-<fresh-run>
    SMARTAGENT_IT10_PATH_TRACE_ROOT=<existing absolute private directory>

The request MUST contain these two lines:

    NLIT_CASE_ID=IT10
    NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E

Restrict directory permissions before acknowledging private ACL.
Use a new run ID and ensure the raw directory cannot be read by other users.
Never upload raw secrets or assume that a private repository is safe for them.

## Collector

Run:

    collect.bat TASK-... RR-... --it10-raw

Manual mode uses .venv/Scripts/python.exe already installed in the checkout,
does not modify the Runtime, and adds these ZIP entries:

    it10_raw_evidence_index.json
    it10/trace/*.jsonl
    it10/raw/*.bin

Each raw_blob_ref in the JSONL maps to it10/raw/<raw_blob_ref> in the ZIP.
Every target is checked for byte count and SHA256, copied byte-for-byte;
missing, oversized or corrupt targets are reported as missing_refs, never PASS.
The collector selects traces ONLY by both task_id and request_id. Other
requests are excluded. Payload size is bounded. Missing boundary stages
are listed separately.

The default two-argument collect.bat invocation remains sanitized and the
automatic RCA uploader still uses this two-argument invocation, so automatic
uploads CANNOT accidentally include the raw mode's data.

Manual mode requires .venv and the same opt-in environment in the collector
console. PYTHON_MISSING, NOT_ARMED, PARTIAL or no matched trace are NOT PASS.

## Git Test handoff

Codex must locally inspect the full ZIP for personal information, credentials,
cookies and other secrets BEFORE any Git upload. If necessary, keep the exact
original ZIP offline and publish an explicitly labeled, redacted copy with
both source and redacted SHA256 values. Never mislabel transformed bytes as raw.

After review, Codex uploads the ZIP to b43120000/test and ALSO uploads
readable text companions: manifest.json, it10_raw_evidence_index.json,
a stage/digest matrix, execution log, and RCA notes. This allows ChatGPT to
read diagnostics via GitHub text interfaces even when ZIP binaries cannot be
opened. Report both the SmartAgent commit SHA and Git Test evidence commit SHA.

## Codex sidecar and Live verification steps

1. Inspect the exact Git67 local and remote SHA; reconcile without resetting or discarding the unpublished local commit.
2. Run offline S0-S7 sidecar and both Git67 regression suites.
3. Run one marked LOCAL_TEST end-to-end and one authorized Telegram Live test, preserving no-side-effect interception before Action dispatch.
4. Record HTTP-before-Decode where possible, each textual boundary, actual ProseMirror readback, actual model response and parser input.
5. Keep Synthetic and Live separate. Status UNOBSERVED is not PASS.
6. Run collect.bat TASK RR --it10-raw; verify pointer closure and ZIP hash.
7. Upload reviewed ZIP plus human-readable companions to Git Test and verify remote readback.

Regression commands:

    set PYTHONPATH=source
    python -B -m pytest -q source/WebAgent/tests/test_git67_sidecar_reproduction.py
    python -B -m pytest -q source/WebAgent/tests/test_git67_raw_evidence_collector.py
    python -B source/WebAgent/tests/validate_it10_path_integrity.py
    python -B source/WebAgent/tests/validate_it10_evidence_gates.py

Windows PowerShell 5.1 ZIP integration and real ChatGPT Live UI cannot be
validated on GitHub source inspection alone. Do not claim those PASS yet.

Do not fix the backslash-corruption origin or Continuation KeyError in this
round. Historical T5 already bad is NOT a known first corruption function.
