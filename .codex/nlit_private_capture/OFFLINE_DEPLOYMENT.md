# SA-NLIT-20261024 — Offline dual-axis observer deployment

**Owner of analysis/source and observer implementation: ChatGPT.** Codex only stages exact artifacts outside the product installation, runs tests/launch and preserves evidence. Earlier `CODEX_ADDED` wording in round-002 delivery was incorrect: GitHub observer source commits were authored through ChatGPT's GitHub tools. No historical IT07–IT23 request should be replayed.

## Immutable version axes

- **Product/source full SHA:** `309305b2eeeb79aed0925dc4dfee45af5193f8b5`. The offline detached test installation MUST remain at that exact Git HEAD, tracked clean, with its original protocol/update manifests. Already-reported four focused regressions, Windows manifest 192 files, `hash_verification=PASS` belong ONLY to this axis, not observer/live testing.
- **Observer/artifact version:** Git branch `fix/nlit-evidence-20261024-r1`, published artifact commit and **per-file UTF-8 SHA-256** in `artifact_manifest.json` alongside the observer. Codex fetches each artifact through GitHub connector at the exact version; do not `git checkout` the observer SHA in the product installation and do not call the detached product HEAD the observer version. The observer root is OUTSIDE the installation and workspace. Its files are not included in the source Manifest.
- **Validation version:** Record GitHub Actions exact head SHA and run ID for offline/unit tests separately from `product_sha` and `observer_sha`. It is not a live Planner delivery attestation.

Because `github.com` DNS is unavailable to the Windows test host, **do not fetch/pull/clone from that host**. Stage complete UTF-8 text bytes obtained with the GitHub connector into an ACL-controlled external observer directory. Verify EVERY byte SHA-256 in `artifact_manifest.json`; any mismatch is BLOCKED. A Git blob SHA is not interchangeable with a SHA-256 or a product Commit SHA. Preserve received source hash, byte lengths, extraction method and local staging report privately.

## 1. Local prerequisites (never mutate a running Agent)

Provision these distinct directories, with no symlink/reparse-point overlap:

- `TEST_INSTALL`: fresh detached `309305b2...` product, Python environment installed, Git tracked-clean, no active worker
- `LIVE_INSTALL`: existing in-use product (READ-ONLY for this run)
- `TEST_WORKSPACE`: new independent authorized workspace, empty `.agents/chunked_writes`
- `LIVE_WORKSPACE`: active workspace (READ-ONLY; never construct ChunkedWriteManager against it)
- `OBSERVER_DIR`: external exact `.codex/nlit_private_capture` artifact files copied from GitHub
- `PRIVATE_DIR`: private trace root, outside Git, installation, workspace, cloud-sync and other agents' directories; inheritance disabled, accessible only to current OS identity, SYSTEM and necessary admin
- `TEST_APPDATA`: separate browser profile root, distinct from `LIVE_APPDATA`; create `TEST_APPDATA` and a private `Temp` directory in this isolated scope

Example ACL preparation (Codex executes only on its newly created private folder; never run against live data):

```powershell
$Private = '<PRIVATE_DIR>'
New-Item -ItemType Directory -Force -Path $Private | Out-Null
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $Private /inheritance:r
icacls $Private /grant:r "*${userSid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"
```

Check the resulting ACL locally. Do not post directory, nonce, credentials, Prompt, response, draft, browser profile or CDP address to GitHub.

Set a **fresh per-run secret nonce** only in the local PowerShell session:

```powershell
$bytes = New-Object byte[] 24
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$env:SMARTAGENT_NLIT_CAPTURE_NONCE = -join ($bytes | ForEach-Object { $_.ToString('x2') })
```

## 2. Offline preflight — read-only, NO browser

The following is a concrete command template; replace placeholders with Codex's real **local** directories. The script refuses overlapping roots, dirty/wrong Git HEAD, artifact SHA mismatch or missing worker import hooks.

```powershell
$obs = '<OBSERVER_DIR>'
$argsNlit = @{
  TestInstall='<TEST_INSTALL>'
  ProductionInstall='<LIVE_INSTALL>'
  TestWorkspace='<TEST_WORKSPACE>'
  ProductionWorkspace='<LIVE_WORKSPACE>'
  ObserverDir=$obs
  PrivateDir='<PRIVATE_DIR>'
  TestAppData='<TEST_APPDATA>'
  ProductionAppData='<LIVE_APPDATA>'
  GitExe='<PATH_TO_INSTALLED_GIT_EXE>'
}
& (Join-Path $obs 'Invoke-NlitIsolated.ps1') @argsNlit -Mode Preflight
if ($LASTEXITCODE -ne 0) { throw 'NLIT_PREFLIGHT_FAILED' }
```

Expected `NLIT_OFFLINE_PREFLIGHT_PASS`. In addition, run offline tests in isolated product Python:

```powershell
$env:PYTHONPATH=$obs
& '<TEST_INSTALL>\.venv\Scripts\python.exe' -m unittest discover -s $obs -p 'test_*.py' -v
if ($LASTEXITCODE -ne 0) { throw 'OBSERVER_UNIT_TEST_FAILED' }
```

The preflight child intentionally sets `SMARTAGENT_REMOTE_WORKER=1` and imports **real** Product `WebAgentProtocolLoop`, `WebAgentBrowserClient`, `WebLLMScraper` *without* starting Browser, to assert four expected hooks, method signatures and opt-in import path. A fake-only test is not enough.

## 3. Prepare independent browser — never reuse the Agent's logged-in page

The product's `browser_preferences` chooses port `12800 + instance` for Chrome and `12810 + instance` for Edge (instances 1–5); its profile root is under **APPDATA**, not necessarily the Git installation. Choose an unused instance with a port different from the live installation; adjust **only TEST_INSTALL's** browser preference with the existing official method, after setting a distinct `APPDATA`:

```powershell
$env:APPDATA = '<TEST_APPDATA>'
$env:PYTHONPATH = '<TEST_INSTALL>\source'
& '<TEST_INSTALL>\.venv\Scripts\python.exe' -c "from agent_core.browser_preferences import save_browser_preferences; print(save_browser_preferences('edge',5))"
```

The example `edge,5` uses port 12815; **do not use** if that port/profile belongs to the live Agent or another owner. Verify separate browser process, independent profile and logged-in account manually. The launch gate's `/json/version` endpoint is only liveness evidence; it does not certify profile identity or logged-in status. If no verified isolated browser is available: **BLOCKED**, no supervisor launch and no test request. Never attach to Codex/ChatGPT's existing work browser or clear an unrelated draft.

## 4. Gated formal launch and NEW request

Only after **offline preflight PASS** and **private browser process/profile/login verified**, use two fresh local PowerShell terminals; nonce and parameter dictionary stay LOCAL, never GitHub. Every process inherits separate `APPDATA`, `PYTHONPATH`, worker opt-in flags and state paths.

Terminal A, **foreground isolated supervisor**:

```powershell
& (Join-Path $obs 'Invoke-NlitIsolated.ps1') @argsNlit -Mode StartSupervisor -PrivateBrowserVerified
```

Confirm the *isolated* launcher reports `WAITING_SIGNAL` and its supervisor is RUNNING. Do NOT start the live installation, and do NOT attach to a live CDP port.

Terminal B, only after A is ready and a private read-only task text file has been saved inside `PRIVATE_DIR` (e.g. `case.txt`):

```powershell
& (Join-Path $obs 'Invoke-NlitIsolated.ps1') @argsNlit -Mode RunCase -PrivateBrowserVerified -CaseFile '<PRIVATE_DIR>\case.txt' -NewCaseId 'NLIT-NEW-CAPTURE-20261024-R3-001'
```

This invokes `RemoteAgent.local_telegram_sender.send_and_wait`, which uses the REAL local Telegram-shaped receiver, Gateway, queue, supervisor, worker, WebAgent Planner and Runtime. The helper inserts an opt-in token from an environment variable (not process argv), with a unique case label; it cannot re-run historical IDs. **Do not** run arbitrary external prompts or transact with the active workspace. For IT19, this must be a separately authorized new isolated case and requires verifying disjoint `.agents/chunked_writes` physical roots and no foreign OPEN writer records first; no direct `ChunkedWriteManager` test against live roots.

`sitecustomize.py` activates only in the new remote worker when both `SMARTAGENT_REMOTE_WORKER=1` and `SMARTAGENT_NLIT_CAPTURE_ENABLED=1`; it does not alter the normal Planner decision or the existing submit/composer gates.

## 5. Private verifier

After the new test completes, validate only the local private corpus:

```powershell
& '<TEST_INSTALL>\.venv\Scripts\python.exe' (Join-Path $obs 'verify_capture.py') --private-root '<PRIVATE_DIR>'
```

Expect at least one `post_tool_rounds_correlated` with `MODEL_RESPONSE_CORRELATED`, real `USER_TURN_CONFIRMED_BY_PRODUCTION_SCRAPER`, matching `RESULT_ID` inside an exact final Composer payload and matching response hash. **Do not call this a Predicate PASS**; it only proves the new request's transport capture if all gates match. If no private authenticated browser or no post-tool round, report `PLANNER_DELIVERY_NOT_OBSERVED` and retain local logs.

## 6. Codex handoff (metadata ONLY)

Codex sends the next GitHub evidence file (new round and Commit; never overwrite old): `product_sha`, `observer_artifact_commit`, individual artifact SHA-256 verification status, `CI_test_sha` and run ID if available, baseline regression results, `preflight_status`, whether separate browser profile/login confirmed, `private_capture_round_count`, `post_tool_rounds_correlated`, `IT19_isolation_preflight`, `new_case_ids`, Runtime terminal receipt/admission and any precise blocker reason. Exact original Prompt, response, source local paths, nonce, browser URLs/CDP/profile and drafts remain **LOCAL ONLY**.

**Current acceptance:** prior 17/17 Planner inputs remain `PLANNER_DELIVERY_NOT_OBSERVED`; no retrospective PASS. If test browser absent, distinguish `BLOCKED_PRIVATE_BROWSER_NOT_READY` from GitHub permission failures. Neither observer unit tests nor existing baseline regression markers substitute for a real local end-to-end test.
