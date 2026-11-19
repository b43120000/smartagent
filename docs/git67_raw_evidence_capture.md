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


## Git67 follow-up: Minimal Executor and submitted-turn observation

The 2026-11-19 Git Test baseline (commit
668b7421eaa40fded53179adb38f9a8bf0841651) proves that Telegram
input/Task Store/Worker were correct in one controlled Live request, but
previously missed the actual Minimal Executor planner/parser code path.

The observed Minimal Executor is source/WebAgent/pokeclaw_executor_loop.py,
NOT the legacy protocol_loop.py planner/parser. Added opt-in stages:

- 08_minimal_prompt: exact Python prompt sent into loop.planner
- 10_submitted_innerText / 10_submitted_textContent /
  10_submitted_innerHTML: the chosen actual submitted USER turn, selected
  by the unique IT10_SUBCASE_ID line; not a random history turn
- 10d_provider_response_return: extracted answer string returned from
  WebScraper._wait_for_response_complete; browser-derived, NOT server raw
- 11_minimal_planner_response: exact str returned by loop.planner
- 11_minimal_parser_input: exact str at Minimal parser boundary
- 12_minimal_decision / 12_minimal_parse_error: structured parsed
  decision or error, separately labelled as a derived JSON value
- 13_minimal_admission_input: original proposed action and its path
- 13_minimal_admission_output: unchanged admission decision and any
  actual admitted action; no authority granted by this trace
- 13_minimal_broker_gate: the actual OS broker admission outcome
- 13_minimal_pre_dispatch_action: final action immediately BEFORE the
  existing real Runtime dispatcher
- 15_minimal_tool_result_raw: the exact return value from the Runtime
  dispatch function (pre-decode; not an OS wire packet)
- 15_minimal_tool_result_decoded: decoded Runtime ToolResult semantics;
  do not mix this JSON serialization with the raw dispatcher output

The opt-in stage collector now detects MINIMAL_EXECUTOR vs
LEGACY_OR_UNKNOWN and provides counts, missing stages, missing raw blobs
(including a logged stage whose blob is empty or unavailable), and verified
pointer closure. A stage name alone never establishes byte-exact evidence. It always lists phone-side transport bytes and the raw model-service
server output as unobserved external boundaries: a browser DOM extraction is
not access to those upstream bytes.

### Required Telegram sender for local Codex Live tests

Use the existing local script without changing it:

    C:\Users\user\Desktop\remoteagent\smartagentSupport\TelegramBatchRunner\sendTGmessage.py

First inspect this exact Python script and its existing invocation interface;
do not guess command flags, recipient, account/session, or send format.
Use its established Telegram USER-session-to-SmartAgent-Bot send mechanism.
Do NOT replace it with a fabricated Bot API sender or simulated Telegram call.
Do not log Telegram API credentials, user-account session strings or cookies.

Record the source file SHA256, interpreter, actual invocation, exit code,
user-session send receipt/message ID, destination Bot identity (nonsecret),
parse_mode, and **sender message bytes immediately before the script passes
them to Telegram**. A sender-side input file can be independently hashed.
If the script does not expose a pre-send byte capture, record that gap and
use an independently hashed Golden file; do not assert phone-side bytes.

At least one real Telegram IT10 request must carry a fresh, unique
IT10_SUBCASE_ID=... marker and exactly the expected literal Windows path
with two backslashes in the Python string value:

    E:\AIspace\.case_d_backups

Send as plain text, without Markdown parse mode or escape processing.
Use the ordinary Runtime observe_fact only. Reject side-effecting proposed
actions in the test harness, without weakening the real ACL/Admission policy.

Collection for the real IDs:

    collect.bat TASK-... RR-... --it10-raw

Review the private ZIP for secrets before publication. Publish the reviewed
ZIP AND readable stage index/trace summary/manifest to b43120000/test under
a fresh evidence path. Preserve both source and publication hashes when
redacting bytes; do not describe a derivative as original raw HTTP.

Comparison decision rule: only an adjacent observed pair where the same
target path changes from correct to incorrect can identify a first mutation
boundary. Missing stages, absent submitted turn, renderer changes and model
response divergences are NEVER retroactively proof of the old Task A/B
historical root cause. Keep Historical Continuation KeyError separate.
