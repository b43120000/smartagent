# SA-NLIT-20261024 — Offline Python 3.13 embedded import-path repair

## Confirmed local RCA (read-only evidence from Codex)

The isolated test install's bundled Python reports `sys.flags.isolated=1`, `ignore_environment=1`, `no_site=0`. Its `.venv/Scripts/python313._pth` controls `sys.path` and omits the external observer root. **`PYTHONPATH` is ignored by design** even when the environment variable exists. Therefore the previous `ModuleNotFoundError: No module named 'private_capture'` is a runtime import-path failure, not GitHub access, missing downloaded files, product Manifest mismatch, or a live Planner/Runtime defect.

**Do not edit product Source, Manifest, global Python, live Agent, browser session or the previously verified observer five-file evidence.** Do not imply observer has loaded until the new independent-process Probe passes.

## Source/version axes, exact GitHub reads

1. Product baseline and isolated test Git HEAD: `309305b2eeeb79aed0925dc4dfee45af5193f8b5` (tracked clean, Manifest 192 files, baseline focused tests reported PASS by Codex; not observer or live PASS).
2. Original five observer files unchanged, originally pinned under `7e19ee5181712f38f8cf3aef6375e510bef15046`, previous manifest `5c541bf467cad53c401aff2cdf25421b5a624f79`.
3. **New overlay artifact source Commit:** `6c06088369bdbcd3a8eae65750263cc785c40c95`. Exact two new files, `Install-OfflineObserverPath.ps1` and `Probe-OfflineObserver.ps1`, are bundled as external artifacts under `.codex/nlit_private_capture/` at this Commit.
4. **New seven-file artifact manifest Commit:** `688341b95889b8c45341c85ae8df0d3dbd03f77b`; path `.codex/nlit_private_capture/artifact_manifest_v2.json` contains original five unchanged SHA-256 / byte lengths plus two new exact SHA-256 / byte lengths. This manifest does not replace or overwrite the prior five-file manifest; retain both privately.
5. CI validation SHA is its own independent axis and must come from the latest successful `NLIT Private Capture Regression` run. A passing PowerShell parser or fake-unit suite is **not** evidence of real Windows private import until the Probe below succeeds locally.

The new files' exact UTF-8 (NO BOM) pin:

| File | byte count | SHA-256 |
|---|---:|---|
| `Install-OfflineObserverPath.ps1` | 6368 | `4651d8ae4f93bfad28fc6720ff14230718010b22256924e3fd04a381385897ff` |
| `Probe-OfflineObserver.ps1` | 3707 | `cd97253957c2f545448faf1679637c0daeaaab6ff4151b5ae758446b5d703fd6` |

The manifest pins the complete original five as well. GitHub blob SHA is **not** a SHA-256, and a Git commit SHA is **not** the current product HEAD.

## Offline staging

Because the local Windows DNS cannot resolve `github.com`, **do not perform git fetch/pull**. Codex obtains the two new artifact files and manifest via the GitHub connector from their exact Commits and writes the byte-identical UTF-8 representations to the existing external observer directory. Leave old original five in place unchanged. Never attempt to switch the detached product Git checkout to the observer artifact SHA.

Using only the existing, verified private directory and paths (all placeholders replaced with local paths; do not publish these paths):

```powershell
$TestInstall = '<TEST_INSTALL>'
$ProductionInstall = '<LIVE_INSTALL>'
$ObserverDir = '<EXTERNAL_OBSERVER_DIR>'
$PrivateRoot = '<PRIVATE_ACL_RESTRICTED_DIR>'
$GitExe = '<PREINSTALLED_GIT_EXE>'
$Manifest = Join-Path $ObserverDir 'artifact_manifest_v2.json'

# Read-only: expected exit 3 / NLIT_EMBEDDED_PTH_ACTION_REQUIRED when not yet enabled.
& (Join-Path $ObserverDir 'Install-OfflineObserverPath.ps1') `
  -TestInstall $TestInstall -ProductionInstall $ProductionInstall `
  -ObserverDir $ObserverDir -PrivateRoot $PrivateRoot `
  -ArtifactManifest $Manifest -GitExe $GitExe

# Explicit test-install-only activation: immutable original bytes backed up
# OUTSIDE checkout before atomic replacement of test python313._pth.
& (Join-Path $ObserverDir 'Install-OfflineObserverPath.ps1') `
  -TestInstall $TestInstall -ProductionInstall $ProductionInstall `
  -ObserverDir $ObserverDir -PrivateRoot $PrivateRoot `
  -ArtifactManifest $Manifest -GitExe $GitExe -Apply
```

Expected second command: `NLIT_EMBEDDED_PTH_INSTALLED` plus original and installed SHA-256. If failed, save error locally and STOP; never improvise a global `PYTHONPATH` or copy observer into tracked source. Never execute when test Worker processes are active.

## Real Python hook probe (NEW interpreter, no browser)

```powershell
$TestAppData = '<TEST_APPDATA_ALREADY_SEPARATED_FROM_LIVE_APPDATA>'
& (Join-Path $ObserverDir 'Probe-OfflineObserver.ps1') `
  -TestInstall $TestInstall -ObserverDir $ObserverDir `
  -PrivateRoot $PrivateRoot -TestAppData $TestAppData
```

Expected two signals: a metadata-only JSON with `status=PASS`, `ignored_pythonpath=true`, `loaded_sitecustomize_from_external_observer=true`, `real_classes_wrapped=4`, `hook_families=3`, `product_manifest_pass=true`, followed by `NLIT_REAL_WORKER_HOOK_IMPORT_PASS`. This verifies all of the real signatures and code hooks under actual isolated Python. It does **not** send a request or claim any old-case PASS.

The probe deliberately sets `PYTHONPATH=IGNORED_IN_EMBEDDED_ISOLATED_MODE` to distinguish an actual `._pth` solution from a misleading environment-only workaround. It imports the real classes, but neither starts a browser/supervisor nor modifies a task/Composer or writer.

After successful probe, rerun the existing `preflight.ps1 -Phase BeforeStart` in the isolated installation. Run the 5 original observer unit tests with `python -m unittest` using the same standalone interpreter, and save local stdout/stderr/exit. Expect product `git rev-parse HEAD` still exactly `309305b2...` and Manifest unchanged. **Do not edit test `._pth` by hand; preserve private backup**. Only later, after new tests finish and the dedicated Worker is stopped, restore the previous `._pth` from the private byte-exact backup with a separately verified SHA; never restore while Python is running.

## Startup gate — NOT automatic

Only after all offline checks PASS may Codex independently check **Edge5** under separate TEST_APPDATA for a separate authenticated profile/process/port. Existing production Chrome1 must remain untouched. Merely seeing `/json/version` or a bound CDP port is **not** proof of ownership or login.

- If the private Edge5 browser is **not** logged in, record `BLOCKED_PRIVATE_BROWSER_NOT_AUTHENTICATED`; do not attach to a live Agent or Codex browser.
- Do not rerun old IT07–IT23. When the test environment is proven safe, use a fresh `NLIT-NEW-...` ID through its own LOCAL_TEST sender/supervisor and keep actual Prompt/Response bytes only in the private directory. `Verify-Capture` result is merely fresh transport evidence, never automatic Predicate PASS.
- IT19 remains `BLOCKED` until the formal normal-entry isolated workspace and `.agents/chunked_writes` writer state are proven disjoint from production.

**Codex responsibilities:** execute the downloaded scripts, confirm exact byte hashes, import path, real hook, test process/active browser, store the private before/after `._pth` backup and probe logs; report only hashes, statuses and sanitized technical findings to GitHub. **ChatGPT responsibility:** own these implementations and repair source if the next local probe identifies a code defect. No product source changes, no main update or merge.
