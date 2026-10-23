# SA-NLIT-20261024 — Round 002 bundled-Python observer import correction

Status: SOURCE WRITTEN AND PINNED; WINDOWS REAL BUNDLED INTERPRETER NOT YET VERIFIED. This is an addendum and overrides any earlier PYTHONPATH-only import or supervisor instructions.

## Confirmed RCA (read-only local evidence)

Codex reported Python 3.13.16 with flags isolated=1, ignore_environment=1, no_site=0, and an installed Scripts/python313._pth. sys.path contains the bundled zip, Scripts, site-packages, and the baseline source, but not the observer folder despite the PYTHONPATH environment variable. The previous import of private_capture raised ModuleNotFoundError. No browser, supervisor or new targeted case has started. The baseline focused regressions and existing five observer file SHA-256+byte checks had already passed separately.

This is a CPython isolated interpreter startup configuration limitation. Environment-only PYTHONPATH import does not work for that embedded Python. Changing the application launcher alone is insufficient because its worker subprocesses also start the same embedded executable with separate -m arguments.

## Authoritative corrective artifact (separate from original five)

- Product baseline: 309305b2eeeb79aed0925dc4dfee45af5193f8b5 (unchanged, no manifest rewrite).
- Original five observer artifacts source Commit: 7e19ee5181712f38f8cf3aef6375e510bef15046.
- Original five-file manifest Commit: 5c541bf467cad53c401aff2cdf25421b5a624f79 (preserved exactly).
- New separate adapter file: .codex/nlit_private_capture/Enable-NlitIsolatedPython.ps1, read from Commit 6d35134920a89da0232366e1d95b7151a9492be6.
- Adapter Git blob SHA: d7114a7b5a09c68c2abe70baed08a42130887868 (Git SHA is NOT SHA-256).
- Adapter SHA-256: ff0626d81527a6a6834f99bbafd0caa632a1cfcf5f26b62232021636cc083fba.
- Adapter byte length: 6310, original UTF-8 without BOM, no newline conversion.
- Separate adapter_manifest.json was created at Commit 1549c045fcb23b4e2a890fefdb2ad9403236ee96.

Codex must use GitHub connector to fetch the exact adapter/manifest commits because git fetch from the Windows environment currently fails DNS. Put the adapter beside the existing five files in the external observer directory; do not replace any original file. Independently verify the adapter SHA-256 and bytes and compare the adapter manifest. Another concurrently added Install-OfflineObserverPath.ps1 also exists in GitHub; do NOT run both adapters or mix backup state; this runbook selects Enable-NlitIsolatedPython.ps1 only.

The adapter is reversible: it saves the exact preimage of ONLY the isolated test package Scripts/python313._pth to a separately restricted private backup directory, adds one canonical absolute path entry for the observer, and verifies the test interpreter still runs isolated=1, ignore_environment=1 while importing both private_capture and sitecustomize from the observer path. No global interpreter, original product installation, tracked source or product manifest is changed.

## Exact PowerShell test sequence, with local placeholders

The following variable values are private to the Codex local shell and must not be published in the GitHub report.

    $ProdInstall = '<EXISTING_PRODUCTION_INSTALL>'
    $TestInstall = '<DETACHED_TEST_INSTALL_AT_309305B2>'
    $ObserverDir = '<EXTERNAL_OBSERVER_DIRECTORY>'
    $PrivateRoot = '<ACL_RESTRICTED_PRIVATE_CAPTURE_DIRECTORY>'
    $BackupDir = Join-Path $PrivateRoot 'python-pth-backup'
    $Py = Join-Path $TestInstall '.venv\Scripts\python.exe'
    $Adapter = Join-Path $ObserverDir 'Enable-NlitIsolatedPython.ps1'

    if (!(Test-Path -LiteralPath $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir -ErrorAction Stop | Out-Null }
    $actual = (Get-FileHash -LiteralPath $Adapter -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne 'ff0626d81527a6a6834f99bbafd0caa632a1cfcf5f26b62232021636cc083fba') { throw 'ADAPTER_HASH_MISMATCH' }
    if ((Get-Item -LiteralPath $Adapter).Length -ne 6310) { throw 'ADAPTER_LENGTH_MISMATCH' }

    & $Adapter -Mode Probe -TestInstall $TestInstall -ProductionInstall $ProdInstall -ObserverDir $ObserverDir -BackupDir $BackupDir
    if (!$?) { throw 'NLIT_ADAPTER_PROBE_FAILED' }

    & $Adapter -Mode Enable -TestInstall $TestInstall -ProductionInstall $ProdInstall -ObserverDir $ObserverDir -BackupDir $BackupDir
    if (!$?) { throw 'NLIT_ADAPTER_ENABLE_FAILED' }

    & $Adapter -Mode Verify -TestInstall $TestInstall -ProductionInstall $ProdInstall -ObserverDir $ObserverDir -BackupDir $BackupDir
    if (!$?) { throw 'NLIT_ADAPTER_VERIFY_FAILED' }

Expected markers are NLIT_PYTHON_PTH_PROBE_PASS, NLIT_PYTHON_PTH_ENABLE_PASS and NLIT_PYTHON_PTH_VERIFY_PASS. Each one must have a zero exit code, and its test-interpreter import check must really execute. If Enable stops after partial write, do NOT blindly retry Enable; preserve backup and state, and use controlled recovery only after all test processes have stopped. If the adapter sees an existing mismatched backup/hash/root, it blocks.

### Fresh worker import auto-hook test — stronger than manual patching

Start a new Python interpreter process only. Set test-only environment variables BEFORE the process starts. Do not use PYTHONPATH as the observer path mechanism; the isolated _pth entry is now authoritative.

    $env:SMARTAGENT_REMOTE_WORKER='1'
    $env:SMARTAGENT_NLIT_CAPTURE_ENABLED='1'
    $env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED='YES'
    $env:SMARTAGENT_NLIT_CAPTURE_ROOT=$PrivateRoot
    $env:SMARTAGENT_NLIT_CAPTURE_NONCE=[guid]::NewGuid().ToString('N')

    & $Py -c "import sys,sitecustomize,private_capture; from WebAgent.protocol_loop import WebAgentProtocolLoop; from WebAgent.browser_client import WebAgentBrowserClient; from agent_core.web_runtime import WebLLMScraper; assert sys.flags.isolated==1 and sys.flags.ignore_environment==1; assert all(hasattr(f,'__wrapped__') for f in (WebAgentProtocolLoop._run_impl, WebAgentBrowserClient.ask, WebLLMScraper._write_prompt_to_composer, WebLLMScraper._log_stage)); print('NLIT_ISOLATED_WORKER_AUTO_HOOK_PASS')"
    if ($LASTEXITCODE -ne 0) { throw 'NLIT_ISOLATED_WORKER_AUTO_HOOK_FAILED' }

The auto-hook test must pass WITHOUT manually calling private_capture.patch_available_modules(). Manual calls would conceal an inactive startup hook. Even a successful import proves hook loading only; it is NOT evidence that the actual Planner payload was submitted.

Restore procedure after stopping the dedicated test supervisor/worker/interpreter processes:

    & $Adapter -Mode Restore -TestInstall $TestInstall -ProductionInstall $ProdInstall -ObserverDir $ObserverDir -BackupDir $BackupDir -TestProcessesStopped
    if (!$?) { throw 'NLIT_ADAPTER_RESTORE_FAILED' }

Expected marker: NLIT_PYTHON_PTH_RESTORE_PASS with the exact original SHA-256. Saved local backup and state remain for audit. No prior agent process or global Python change is authorized.

## Continue only after this gate

1. Run the existing five-file preflight.ps1 (BeforeStart) again and existing 5 collector unit tests in the isolated test interpreter. Ensure the old observer artifact manifest is unchanged and the new adapter is separately pinned.
2. Verify dedicated test browser profile (test Edge5 versus live Chrome1), separate test supervisor/queue and TestWorkspace writer state, no shared process, page, CDP or active production writer. This requires real Codex local observation, not a code assumption.
3. Only then start the isolated supervisor with the observer opt-in environment and run a NEW unique LOCAL_TEST case through the real Telegram-shaped receiver/Gateway/worker/Planner/Runtime.
4. Correlate prepared final composer-input bytes, submit click, user-turn bound to this request and fresh assistant response. A constructed prompt hash, persisted action snapshot or Runtime producer event alone is NOT sufficient.
5. IT19 remains UNSENT/BLOCKED until independently separate workspace, writer store, state, queue, supervisor and browser ownership are proven. Keep composer fail-closed, do not clear/retry unowned draft. No original 17 cases may be rerun or retroactively marked PASS.

All private paths, original prompts, assistant content, browser identifiers, CDP state and original evidence stay local. Publish only sanitized hash/byte-count/status/exit-code fields. At Codex quota <=5%, checkpoint and safely conclude the current idle test, then schedule only the one authorized replenishment continuation.
