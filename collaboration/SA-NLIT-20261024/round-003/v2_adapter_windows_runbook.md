# SA-NLIT-20261024 round-003 — v2 adapter deployment and gate checklist

**Scope:** isolated Windows test package ONLY. This is a proposed executable runbook; **no Codex-local execution is claimed** by this document. Do not alter or merge `main`, original production install/workspace, historical case IDs, or any observer collector source. Use the already committed e5 v2 adapter; never deploy the older v1. Never run `Install-OfflineObserverPath.ps1` in this series.

## Immutable artifacts to obtain using GitHub connector

- Adapter: `.codex/nlit_private_capture/Enable-NlitIsolatedPython-v2.ps1`, SOURCE COMMIT `e5d8c82a84b8a4a6f66a75946ea54ac0bcb4a3b9`, Git blob `4fc49e324c95dca66de8b6e490b853f825e9b115`, SHA-256 `487dd90395a21ffda8556fe50595018db84d35e42b970de64bb2080fdee9cb8f`, **6660 bytes** (UTF-8 without BOM).
- Original five observer files: unchanged from `7e19ee5181712f38f8cf3aef6375e510bef15046`; preserve original `artifact_manifest.json` exactly (manifest source `5c541bf467cad53c401aff2cdf25421b5a624f79`).
- Separate adapter manifest: `.codex/nlit_private_capture/adapter_manifest.json` on THIS repair branch. It supersedes the old v1 pin; it does not supersede or mutate the original five-file artifact manifest.
- StrictMode fixture test: `.codex/nlit_private_capture/Test-NlitIsolatedPython-v2.ps1`. This is a synthetic, non-deployment test; its PASS is NOT a real import/IT PASS.

Use exact UTF-8 source bytes as fetched. Never paste or publish real private paths, browser identifiers, CDP address, profile IDs, login details, prompts, responses, or nonces to this repository.

## Gate 0 — independently verify local files

These variables must be assigned in the **private Codex Windows shell only**; values MUST NOT be committed to GitHub.

```powershell
$ProductionInstall = '<ORIGINAL_PRODUCTION_INSTALL>'
$ProductionWorkspace = '<ORIGINAL_PRODUCTION_WORKSPACE>'
$TestInstall = '<DETACHED_TEST_INSTALL_AT_309305B2>'
$TestWorkspace = '<FRESH_DEDICATED_TEST_WORKSPACE>'
$ObserverDir = '<SEPARATE_OBSERVER_DIRECTORY>'
$PrivateRoot = '<SEPARATE_RESTRICTED_PRIVATE_CAPTURE_DIRECTORY>'
$BackupDir = Join-Path $PrivateRoot 'python-pth-backup'
$Adapter = Join-Path $ObserverDir 'Enable-NlitIsolatedPython-v2.ps1'
$TestAdapter = Join-Path $ObserverDir 'Test-NlitIsolatedPython-v2.ps1'
$Py = Join-Path $TestInstall '.venv\Scripts\python.exe'
$GitExe = '<KNOWN_LOCAL_GIT_EXE>'
if (!(Test-Path -LiteralPath $BackupDir)) {
  New-Item -ItemType Directory -Path $BackupDir -ErrorAction Stop | Out-Null
}
if ((Get-FileHash -LiteralPath $Adapter -Algorithm SHA256).Hash.ToLowerInvariant() -ne '487dd90395a21ffda8556fe50595018db84d35e42b970de64bb2080fdee9cb8f') {
  throw 'NLIT_V2_ADAPTER_SHA256_MISMATCH'
}
if ((Get-Item -LiteralPath $Adapter).Length -ne 6660) { throw 'NLIT_V2_ADAPTER_BYTES_MISMATCH' }
if (!(Test-Path -LiteralPath $TestAdapter -PathType Leaf)) { throw 'NLIT_V2_TEST_SCRIPT_MISSING' }
# BeforeStart requires a fresh independent TestWorkspace and an unchanged five-file manifest.
& (Join-Path $ObserverDir 'preflight.ps1') -ProductionInstall $ProductionInstall -ProductionWorkspace $ProductionWorkspace -TestInstall $TestInstall -TestWorkspace $TestWorkspace -ObserverDir $ObserverDir -PrivateCaptureRoot $PrivateRoot -ArtifactManifest (Join-Path $ObserverDir 'artifact_manifest.json') -GitExe $GitExe -Phase BeforeStart
if (!$?) { throw 'NLIT_BEFORESTART_PREFLIGHT_FAILED' }
```

## Gate 1 — StrictMode and read-only Probe (must PASS before any Enable)

```powershell
& $TestAdapter -AdapterPath $Adapter
if (!$?) { throw 'NLIT_STRICTMODE_REGRESSION_FAILED' }
# Required markers:
# NLIT_STRICTMODE_FILEINFO_DIRECTORYINFO_PASS
# NLIT_STRICTMODE_OVERLAP_GUARD_PASS
# NLIT_STRICTMODE_IMPORT_SITE_GUARD_PASS
# NLIT_STRICTMODE_REGRESSION_PASS

& $Adapter -Mode Probe -TestInstall $TestInstall -ProductionInstall $ProductionInstall -ObserverDir $ObserverDir -BackupDir $BackupDir
if (!$?) { throw 'NLIT_V2_PROBE_FAILED' }
# Required: NLIT_PYTHON_PTH_PROBE_PASS.
# Probe may not create a backup/state, edit _pth or run Python.
```

## Gate 2 — controlled test-only Enable, Verify, and **fresh worker auto-hook**

Stop if an existing backup/state is discovered. **Do not blindly re-Enable.** Inspect private backup state and all dedicated test process ownership first. Only after the preceding gates pass:

```powershell
& $Adapter -Mode Enable -TestInstall $TestInstall -ProductionInstall $ProductionInstall -ObserverDir $ObserverDir -BackupDir $BackupDir
if (!$?) { throw 'NLIT_V2_ENABLE_FAILED' }
# Required: NLIT_PYTHON_PTH_ENABLE_PASS and actual bundled interpreter import.
& $Adapter -Mode Verify -TestInstall $TestInstall -ProductionInstall $ProductionInstall -ObserverDir $ObserverDir -BackupDir $BackupDir
if (!$?) { throw 'NLIT_V2_VERIFY_FAILED' }
# Required: NLIT_PYTHON_PTH_VERIFY_PASS.

$env:SMARTAGENT_REMOTE_WORKER = '1'
$env:SMARTAGENT_NLIT_CAPTURE_ENABLED = '1'
$env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED = 'YES'
$env:SMARTAGENT_NLIT_CAPTURE_ROOT = $PrivateRoot
$env:SMARTAGENT_NLIT_CAPTURE_NONCE = [guid]::NewGuid().ToString('N')
# Launch a NEW interpreter; never manually call patch_available_modules().
& $Py -c "import sys,sitecustomize,private_capture; from WebAgent.protocol_loop import WebAgentProtocolLoop; from WebAgent.browser_client import WebAgentBrowserClient; from agent_core.web_runtime import WebLLMScraper; assert sys.flags.isolated==1 and sys.flags.ignore_environment==1; assert all(hasattr(f,'__wrapped__') for f in (WebAgentProtocolLoop._run_impl,WebAgentBrowserClient.ask,WebLLMScraper._write_prompt_to_composer,WebLLMScraper._log_stage)); print('NLIT_ISOLATED_WORKER_AUTO_HOOK_PASS')"
if ($LASTEXITCODE -ne 0) { throw 'NLIT_ISOLATED_WORKER_AUTO_HOOK_FAILED' }
# Remove opt-in environment values from the shell if subsequently doing unrelated work.
```

This test proves only that wrappers auto-install in a fresh dedicated worker, NOT that the real Planner received the exact composed payload.

## Gate 3 — local isolation before new targeted IT

Codex must independently record (privately) that the test installation at product `309305b2eeeb79aed0925dc4dfee45af5193f8b5`, independent **test workspace/writer transaction store**, supervisor, queue, browser profile, CDP endpoint and login are all separate from the still-running production agent. Use the existing supported browser preferences method for the dedicated Edge5 test instance. Do not share a process, browser page, profile, port, or writer; inspect currently running processes to corroborate actual ownership.

Only after all of Gates 0–3 have objective PASS evidence may Codex start the dedicated supervisor and create ONE new uniquely identified, read-only Telegram-shaped LOCAL_TEST request through actual Receiver/Gateway/queue/Planner/Runtime/outbox. Privately correlate exact composer input, submit action, bound user turn, fresh assistant result and Runtime terminal receipt. A mock, five unit assertions, static hash, existing 15 receipts or missing Planner payload is **not** a new IT PASS.

**IT19** remains **UNSENT/BLOCKED** until a separate transaction store and writer ownership are proven. Never clear an unrelated composer draft, blindly retry uncertain prewrite, reuse old request IDs or rerun the 17 terminal historical requests.

## Restore gate — after dedicated test processes are stopped

```powershell
# Stop/confirm the ownership of every test supervisor, worker and interpreter
# BEFORE Restore. Production processes must remain untouched.
& $Adapter -Mode Restore -TestInstall $TestInstall -ProductionInstall $ProductionInstall -ObserverDir $ObserverDir -BackupDir $BackupDir -TestProcessesStopped
if (!$?) { throw 'NLIT_V2_RESTORE_FAILED' }
# Required: NLIT_PYTHON_PTH_RESTORE_PASS, original SHA256 restored exactly.
# Preserve restricted backup and state for audit; do not clean them blindly.
```

## Status contract and reporting

Record each gate separately as PASS / FAIL / BLOCKED / NOT_RUN with exit code and sanitized evidence hash only. Do NOT treat these instructions as executed. Source artifact review is independent from local Windows PowerShell results; no true hook or live NL-IT PASS exists until Codex performs and submits its evidence. At <=5% remaining Codex quota, checkpoint current safe state and do not initiate another local request.
