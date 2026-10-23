# SA-NLIT-20261024 round 002 — Offline dual-axis deployment & safe NL-IT protocol

Status: **RUNBOOK WRITTEN; actual Windows execution by Codex NOT YET OBSERVED**. This document is Codex-readable and authoritative for **new** targeted requests. Never replay IT07–IT23 historical run IDs.

## 0. Evidence and version facts

- Baseline product tree commit **`309305b2eeeb79aed0925dc4dfee45af5193f8b5`** exists in the user's local Git object store. The user/Codex reported a separate tracked-clean detached checkout, independent Python 3.13.16, Git 2.56.0, four focused regression scripts with exit 0 and assertions, `192`-file protocol manifest `hash_verification=PASS`; these are **baseline tests only**, not observer, live UI or NL-IT acceptance.
- GitHub fetch from local Windows failed DNS (`Could not resolve host github.com`); do not claim a GitHub permission problem or alter manifests to hide inability to fetch.
- Observer files are **external instrumentation**, not a second product HEAD. Pin them to exact source Commit `7e19ee5181712f38f8cf3aef6375e510bef15046` and its child artifact manifest Commit `5c541bf467cad53c401aff2cdf25421b5a624f79`. New docs/CI commits do not change the five observer files unless explicitly checked.
- Original 17 requests still have `PLANNER_DELIVERY_NOT_OBSERVED`; the 207 selected producer events, 15 receipts, and original task snapshots do not retroactively prove delivery.
- The running legacy process SHA remains independently **UNVERIFIED**. Do not claim it has switched to the test product or observer.

## 1. Install GitHub-fetched observer without `git fetch`

Codex must use its **GitHub connector** to fetch each source from Commit **`7e19ee5181712f38f8cf3aef6375e510bef15046`** and `artifact_manifest.json` from Commit **`5c541bf467cad53c401aff2cdf25421b5a624f79`**. These are the only required GitHub source artifacts; no command needs `github.com` DNS from the Windows package.

Repository paths:
```text
.codex/nlit_private_capture/private_capture.py
.codex/nlit_private_capture/sitecustomize.py
.codex/nlit_private_capture/test_private_capture.py
.codex/nlit_private_capture/verify_capture.py
.codex/nlit_private_capture/preflight.ps1
.codex/nlit_private_capture/artifact_manifest.json
```

Write those exact UTF-8 bytes, **without BOM or newline conversion**, into a dedicated **ObserverDir** OUTSIDE the product checkouts, the original workspace and the dedicated test workspace. Never paste source into production `source`. The original GitHub `blob_sha` is a Git object digest, *not* SHA-256; independently verify every downloaded file with the pinned `sha256` and `bytes` in `artifact_manifest.json`. If an importer normalizes line endings or appends a newline, recopy the original bytes or report `ARTIFACT_SHA256_MISMATCH`. Do not force hashing success.

Do not modify the tracked product source or `config/protocol_manifest.json`. No product manifest regeneration is needed: the product remains at baseline SHA; observer has its own independent source Commit and SHA-256 artifact identity. The GitHub documentation and CI workflow are also not installed as product code.

## 2. Isolated directories, ACL and browser identity

Codex maintains **five distinct, canonical, non-symlinked roots**, all local/private paths and never published to GitHub:

- `ProductionInstall`: legacy in-use installation; leave untouched.
- `ProductionWorkspace`: legacy writer state (contains or may contain open transactions); do not open `ChunkedWriteManager` there.
- `TestInstall`: the detached, tracked-clean checkout at exactly `309305b2...` with its own `.venv`, localdata, security profile, state, supervisor queue and launcher.
- `TestWorkspace`: a **fresh empty** authorized workspace, distinct from both installs/workspaces, with no preexisting `.agents` before the first test supervisor run. Its test write store must resolve to **TestWorkspace/.agents/chunked_writes**, never the legacy store.
- `ObserverDir`: fetched observer code only. A **sixth distinct root**, `PrivateCaptureRoot`, holds raw transcript bytes and must be outside all above. Use a user-only ACL. If its ACL grants read access to Everyone, Authenticated Users or BUILTIN Users, preflight blocks.

Make sure isolated `TestInstall/config/browser_session.json` selects a browser instance that differs from production. Actual port mapping in baseline `browser_preferences.py`: Chrome `12800 + instance`, Edge `12810 + instance`, 1–5. Example: production Chrome 1 vs test Edge 5 (provided both are available). Check the test port is not already LISTENING. Use the **existing product browser-preferences configuration mechanism** only in the isolated test install; verify tracked-clean and Manifest afterward. If a browser config change makes tracked files dirty or changes a hashed product file, **BLOCK** and report rather than pretending product SHA still describes the running tree.

Before live tests, Codex must privately inspect process ownership: no shared `host_supervisor`, Worker process, live browser profile, CDP browser instance/tab, writer path, runtime queue or lock root. Path disjointness alone does not prove process isolation. In particular, do **not** start the new worker on the original supervisor by changing only `local_telegram_sender --workspace`. For IT19, ensure the test profile is a new dedicated browser, not an active legacy page.

## 3. Preflight commands — read only

PowerShell template; substitute **local private variables in Codex only**. Do not copy filled-in paths to GitHub, the ChatGPT transcript, or sanitized reports.

```powershell
$ProdInstall = '<LEGACY_INSTALL>'
$ProdWorkspace = '<LEGACY_AUTHORIZED_WORKSPACE>'
$TestInstall = '<DEDICATED_BASELINE_INSTALL>'
$TestWorkspace = '<FRESH_AUTHORIZED_TEST_WORKSPACE>'
$ObserverDir = '<GITHUB_FETCHED_OBSERVER_DIRECTORY>'
$PrivateRoot = '<PRIVATE_ACL_CAPTURE_DIRECTORY>'
$GitExe = '<INSTALLED_GIT_EXE>'
$Py = Join-Path $TestInstall '.venv\Scripts\python.exe'

& (Join-Path $ObserverDir 'preflight.ps1') `
  -ProductionInstall $ProdInstall -ProductionWorkspace $ProdWorkspace `
  -TestInstall $TestInstall -TestWorkspace $TestWorkspace `
  -ObserverDir $ObserverDir -PrivateCaptureRoot $PrivateRoot `
  -ArtifactManifest (Join-Path $ObserverDir 'artifact_manifest.json') `
  -GitExe $GitExe -Phase BeforeStart
if ($LASTEXITCODE -ne 0) { throw 'NLIT preflight failed' }

# Collector unit tests (fake boundary, NO browser or actual task):
$env:PYTHONPATH = $ObserverDir
& $Py -m unittest discover -s $ObserverDir -p test_private_capture.py -v
if ($LASTEXITCODE -ne 0) { throw 'NLIT collector offline unit tests failed' }
```

Expected first marker: `NLIT_PREFLIGHT_PASS phase=BeforeStart baseline=309305b2...`. A source script may exit with `NLIT_PREFLIGHT_BLOCKED:<code>`; that is a **safe block**, not permission to disable a gate. Record preflight exit code, local stdout/stderr, timestamp, product SHA, observer manifest SHA in a private evidence directory; publish only status/allowlisted digests.

### Hook installation self-test (no browser sends)

In a new temporary Python process using the **test** Python executable, set only that process's environment and import existing classes:

```powershell
$env:PYTHONPATH = "$ObserverDir;$(Join-Path $TestInstall 'source')"
$env:SMARTAGENT_REMOTE_WORKER='1'
$env:SMARTAGENT_NLIT_CAPTURE_ENABLED='1'
$env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED='YES'
$env:SMARTAGENT_NLIT_CAPTURE_ROOT=$PrivateRoot
$env:SMARTAGENT_NLIT_CAPTURE_NONCE=([guid]::NewGuid().ToString('N'))
& $Py -c "from WebAgent.browser_client import WebAgentBrowserClient; from WebAgent.protocol_loop import WebAgentProtocolLoop; from agent_core.web_runtime import WebLLMScraper; import private_capture as p; assert p.patch_available_modules(); print('NLIT_OBSERVER_HOOK_IMPORT_OK')"
if ($LASTEXITCODE -ne 0) { throw 'Observer hook import failed' }
Remove-Item Env:SMARTAGENT_REMOTE_WORKER -ErrorAction SilentlyContinue
```

This import-only test proves hook importability, **not** actual Planner message submission. It must NOT initialize a real `ChunkedWriteManager`, send a browser prompt or mutate a production draft.

## 4. Dedicated supervisor + real gateway/worker transport

After the read-only preflight and process/port preflight pass, open a **new terminal** for the dedicated TestInstall supervisor. In that terminal, set the observer variables *before launching*; Python's `sitecustomize.py` will activate only in spawned worker processes with `SMARTAGENT_REMOTE_WORKER=1`. It must be importable from `PYTHONPATH`. Normal product launch BAT prepends `TestInstall/source` and retains the inherited observer path.

```powershell
$env:SMARTAGENT_NLIT_CAPTURE_ENABLED='1'
$env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED='YES'  # only AFTER preflight ACL check
$env:SMARTAGENT_NLIT_CAPTURE_ROOT=$PrivateRoot
$env:SMARTAGENT_NLIT_CAPTURE_NONCE=([guid]::NewGuid().ToString('N'))
$env:PYTHONPATH = "$ObserverDir;$env:PYTHONPATH"
& (Join-Path $TestInstall 'launch_remote_agent.bat')
```

Do **not** start the legacy supervisor or the original active browser, and do not disable composer verification, the single-submit gate, terminal-evidence checks or request ownership. Preserve the nonce in the dedicated local session only; do not publish it. Verify that the new test supervisor heartbeat and test browser are owned by the test installation, with a browser port/profile distinct from legacy. Then repeat the preflight **`-Phase AfterStart`** (same arguments, no port-free assertion in that phase). This must return PASS before sending a new task.

### First new opt-in read-only case, not an IT07–IT23 replay

Use a new `NLIT-NEW-R2-CAPTURE-...` case ID. The exact first line is the local activation token:

```powershell
$RequestText = "NLIT_TRACE_TOKEN=$($env:SMARTAGENT_NLIT_CAPTURE_NONCE)`nNLIT-NEW-R2-CAPTURE-001: In the separately authorized test workspace, perform a single read-only list_directory through the normal Planner/Runtime loop; use its real result in the next Planner decision. Do not write, clear drafts, retry actions or invent SUCCESS. Report actual evidence and accepted terminal only."
Push-Location $TestInstall
try {
  $env:PYTHONPATH = "$(Join-Path $TestInstall 'source');$ObserverDir"
  & $Py -m RemoteAgent.local_telegram_sender `
      --workspace $TestWorkspace --request $RequestText --timeout 900
} finally { Pop-Location }
```

The LOCAL_TEST sender sends through actual Telegram-shaped receiver → Gateway → independent supervisor queue → worker → real browser Planner → Runtime. The special token is **only an observer selection predicate**, never an instruction to bypass production validity rules. The script captures the final composer-ready model input after transport transformations, actual non-destructive submit stages, a scope-confirmed fresh user turn, and the correlated assistant reply from the real browser. No original 17 case IDs are replayed. **If any stage is missing, treat delivery as NOT_OBSERVED**; do not invoke a mock Planner or reconstruct a prompt from the Runtime ledger.

After the new request, run:

```powershell
& $Py (Join-Path $ObserverDir 'verify_capture.py') --private-root $PrivateRoot
if ($LASTEXITCODE -ne 0) { throw 'No fully correlated next-Planner capture' }
```

The verifier publishes **aggregate-only** fields such as total rounds, post-tool rounds, correlated rounds and missing gate counts; private prompt/response bytes remain in PrivateCaptureRoot. For a post-tool Planner turn, it additionally requires matching RESULT_ID and the `[WEBAGENT_TOOL_RESULTS]` envelope in the **actual captured composer-input bytes**. The exact round identity, private binary hashes and Runtime evidence must agree before reporting targeted E2E evidence complete. A source code prompt reconstruction or `tool_completed` event is insufficient.

## 5. IT19 fresh isolated transaction case — formal route only

IT19 historical **remains UNSENT/BLOCKED**, regardless of new tests. Only after every independent isolation gate passes may Codex send a **new case ID** such as `NLIT-NEW-R2-WRITER-001`, under the dedicated TestWorkspace. It must explicitly authorize only a **new test-only file** within the fresh workspace and request a small begin_file_write → write_file_chunk → commit_file_write transaction via the normal natural-language Planner/Runtime path; use canonical tools and verify test-only output. Do not construct a ChunkedWriteManager directly from a test harness. Never operate on the legacy production transaction, even for 'just a check'. Full request provenance and local before/after test writer manifests must be preserved privately.

Required proof: `real_test_workspace`, `test_writer_state_root` resolved under it; both disjoint from legacy paths and with no link aliases, `no_shared_supervisor_queue`, `no_shared_profile_or_cdp`, test transaction only touches the isolated manifest/staging/destination, production unchanged. On failed isolation, no action is dispatched and the verdict remains `IT19_BLOCKED_ISOLATION_UNPROVEN`.

## 6. Verification boundaries and reporting

After the targeted run, Codex publishes one **minimized** new round-002 status/handoff (or round-003 if a new official round is started), using only bool/status, SHA-256, file byte sizes, event types and non-sensitive error reasons. **Never upload** complete prompts, assistant answers, drafts, browser URLs, CDP endpoints, user local paths, security tokens or locally private logs. No old test is retroactively marked PASS.

Two-axis declaration required:

```json
{
  "product_head_sha": "309305b2eeeb79aed0925dc4dfee45af5193f8b5",
  "observer_source_commit": "7e19ee5181712f38f8cf3aef6375e510bef15046",
  "source_manifest_verification": "PASS",
  "offline_observer_byte_hashes_verified": true,
  "loaded_process_product_sha": "UNVERIFIED_UNTIL_ATTESTED",
  "targeted_new_case_status": "NOT_RUN",
  "historical_17_planner_delivery": "NOT_OBSERVED",
  "it19_historical_status": "BLOCKED",
  "never_claimed_product_HEAD_equals_observer_commit": true
}
```

If no private user-turn and response evidence is obtained, report the exact missing stage and fail closed. A locally confirmed `NLIT_OBSERVER_HOOK_IMPORT_OK` does **not** imply live capture success.

### Codex low-credit safety handoff

At approximately 5% remaining Codex quota, stop starting new work, do not interrupt active writer/browser mid-submit, record exact local test stage, file hash/exit codes and pending private state, safely release idle test resources and publish only a minimized checkpoint. Resume **once** at the next replenishment window according to the user's latest instruction. Do not rerun old cases or lose proof by deleting private trace directories.

### Stop conditions

`BLOCK` if: missing or dirty product source, manifest mismatch, unverified observer SHA-256/bytes, shared path, reparse point, ACL broad reader, shared/used test CDP profile/port, unverified supervisor identity, existing active foreign writer, private collector inactive, unconfirmed composer/draft, or incomplete Planner submit/user-turn correlation. Do **not** work around any stop condition by disabling safety checks.
