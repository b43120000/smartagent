$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$installerRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $installerRoot 'sync_back.ps1') -LibraryOnly

$launcher = Get-Content -LiteralPath (Join-Path (Split-Path -Parent $installerRoot) 'sync_back.bat') -Raw
$systemPowerShellText = '%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe'
if ($launcher -notmatch [regex]::Escape($systemPowerShellText)) { throw 'sync-back launcher does not pin System32 PowerShell' }
if ($launcher -notmatch 'SYNC_BACK_DESTINATION') { throw 'sync-back launcher argument self-test missing' }

$temp = Join-Path ([IO.Path]::GetTempPath()) ('smartagent-sync-back-test-' + [Guid]::NewGuid().ToString('N'))
$installed = Join-Path $temp 'installed'
$destination = Join-Path $temp 'destination'
try {
    foreach ($root in @($installed,$destination)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $root 'config'),(Join-Path $root 'source') | Out-Null
    }
    $baseBytes = (New-Object Text.UTF8Encoding($false)).GetBytes('base')
    $baseFile = Join-Path $installed 'source\module.py'
    [IO.File]::WriteAllBytes($baseFile, $baseBytes)
    $baseHash = Get-FileSha256 $baseFile
    [IO.File]::WriteAllBytes((Join-Path $destination 'source\module.py'), $baseBytes)
    Save-DeploymentBaseline $installed $destination @{'source/module.py'=$baseHash} 'baseline' 1
    [IO.File]::WriteAllText($baseFile, 'installed-change', (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText((Join-Path $installed 'source\new_module.py'), 'new', (New-Object Text.UTF8Encoding($false)))
    New-Item -ItemType Directory -Force -Path (Join-Path $installed 'localdata') | Out-Null
    [IO.File]::WriteAllText((Join-Path $installed 'localdata\secret.txt'), 'secret')
    $manifest = [ordered]@{
        schema='SMARTAGENT_UPDATE_MANIFEST_V1'; release_id='baseline'; release_sequence=1
        files=[ordered]@{ 'source/module.py'=$baseHash }
    }
    [IO.File]::WriteAllText(
        (Join-Path $installed 'config\update_manifest.json'),
        ($manifest | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false))
    )
    $plan = Get-SyncBackPlan $installed $destination
    if (@($plan.changes).Count -ne 2) { throw "expected two changes, got $(@($plan.changes).Count)" }
    if (@($plan.conflicts).Count -ne 0) { throw 'clean destination produced a conflict' }
    if (@($plan.changes.relative) -contains 'localdata/secret.txt') { throw 'localdata entered sync-back plan' }
    # A rebuilt mutable release manifest must not erase installed edits.
    $manifest.files['source/module.py'] = Get-FileSha256 $baseFile
    [IO.File]::WriteAllText((Join-Path $installed 'config\update_manifest.json'), ($manifest | ConvertTo-Json -Depth 8))
    if (@((Get-SyncBackPlan $installed $destination).changes).Count -ne 2) { throw 'rebuilt manifest erased source edits' }
    $preview = Invoke-SyncBack $installed $destination -DryRun -NonInteractive
    if ($preview.status -ne 'DRY_RUN' -or (Test-Path -LiteralPath (Join-Path $installed 'localdata\sync_back'))) { throw 'dry run wrote transaction files' }
    Remove-Item -LiteralPath (Join-Path $destination 'source\module.py')
    if (@((Get-SyncBackPlan $installed $destination).conflicts).Count -ne 1) { throw 'destination deletion plus installed edit was not a conflict' }
    [IO.File]::WriteAllBytes((Join-Path $destination 'source\module.py'), $baseBytes)
    foreach ($bad in @('../escape', 'source/test.py:stream', '\rooted', 'source/test.')) {
        $rejected = $false
        try { Resolve-ChildPath $installed $bad 'test' | Out-Null } catch { $rejected = $true }
        if (-not $rejected) { throw "unsafe path accepted:$bad" }
    }
    $receiptPath = Get-DeploymentBaselinePath $installed
    $receiptBytes = [IO.File]::ReadAllBytes($receiptPath)
    $receiptHashBefore = Get-FileSha256 $receiptPath
    if (-not (Test-ExcludedRelativePath 'config/credentials.json') -or (Test-NewFileAllowed 'config/credentials.json')) { throw 'settings were not excluded' }
    Remove-Item -LiteralPath $receiptPath
    $missingRejected = $false
    try { Get-SyncBackPlan $installed $destination | Out-Null } catch { $missingRejected = $_.Exception.Message -like 'sync_back_deployment_baseline_missing*' }
    if (-not $missingRejected) { throw 'missing receipt fell back to mutable manifest' }
    [IO.File]::WriteAllBytes($receiptPath, $receiptBytes)

    [IO.File]::WriteAllText((Join-Path $destination 'source\module.py'), 'destination-change', (New-Object Text.UTF8Encoding($false)))
    $conflictPlan = Get-SyncBackPlan $installed $destination
    if (@($conflictPlan.conflicts).Count -ne 1 -or $conflictPlan.conflicts[0].relative -ne 'source/module.py') {
        throw 'two-sided edit conflict was not detected'
    }
    [IO.File]::WriteAllBytes((Join-Path $destination 'source\module.py'), $baseBytes)
    $finalizer = ${function:Invoke-ReleaseFinalization}
    function Read-Host { return 'SYNC_BACK' }
    function Invoke-ReleaseFinalization { throw 'injected_finalization_failure' }
    $rollbackPassed = $false
    try { Invoke-SyncBack $installed $destination | Out-Null }
    catch { $rollbackPassed = $_.Exception.Message -like 'sync_back_failed_rolled_back:injected_finalization_failure*' }
    if (-not $rollbackPassed -or (Get-FileSha256 (Join-Path $destination 'source\module.py')) -ne $baseHash -or (Test-Path -LiteralPath (Join-Path $destination 'source\new_module.py'))) { throw 'rollback failed to restore original destination' }
    $transactions = @(Get-ChildItem -LiteralPath (Join-Path $installed 'localdata\sync_back\transactions') -Directory)
    if ($transactions.Count -ne 1 -or -not (Test-Path -LiteralPath (Join-Path $transactions[0].FullName 'backup\source\module.py'))) { throw 'rollback backup was not retained' }
    function Invoke-ReleaseFinalization { return [pscustomobject]@{release_id='fixture'; release_sequence=2} }
    $synced = Invoke-SyncBack $installed $destination
    if ($synced.status -ne 'SYNCED' -or -not (Test-Path -LiteralPath $synced.backup_path) -or (Get-FileSha256 $receiptPath) -ne $receiptHashBefore) { throw 'transaction success did not preserve backup and baseline' }
    if (@((Get-SyncBackPlan $installed $destination).changes).Count -ne 0) { throw 'successful sync did not become the next comparison base' }
    [IO.File]::WriteAllText($baseFile, 'installed-second-change', (New-Object Text.UTF8Encoding($false)))
    $secondPlan = Get-SyncBackPlan $installed $destination
    if (@($secondPlan.conflicts).Count -ne 0 -or @($secondPlan.changes).Count -ne 1) { throw 'repeat sync falsely rejected previously synced destination' }
    # Backward compatibility with legacy successful transactions retaining stage.
    $syncedStatePath = Join-Path $synced.backup_path 'state.json'
    $syncedStateBytes = [IO.File]::ReadAllBytes($syncedStatePath)
    $legacyState = Read-JsonFile $syncedStatePath
    $legacyState.PSObject.Properties.Remove('applied_files')
    $legacyState.PSObject.Properties.Remove('baseline_hash')
    [IO.File]::WriteAllText($syncedStatePath, ($legacyState | ConvertTo-Json -Depth 8))
    if (@((Get-SyncBackPlan $installed $destination).conflicts).Count -ne 0) { throw 'legacy successful sync stage was not recognized' }
    [IO.File]::WriteAllBytes($syncedStatePath, $syncedStateBytes)
    [IO.File]::WriteAllText((Join-Path $destination 'source\module.py'), 'independent-destination-change')
    if (@((Get-SyncBackPlan $installed $destination).conflicts).Count -ne 1) { throw 'sync history bypassed a genuine two-sided conflict' }
    [IO.File]::WriteAllText((Join-Path $destination 'source\module.py'), 'installed-change', (New-Object Text.UTF8Encoding($false)))
    $secondSync = Invoke-SyncBack $installed $destination
    if ($secondSync.status -ne 'SYNCED' -or (Get-FileSha256 (Join-Path $destination 'source\module.py')) -ne (Get-FileSha256 $baseFile)) { throw 'second sync did not apply the new installed edit' }
    if ((Get-FileSha256 $receiptPath) -ne $receiptHashBefore) { throw 'repeat sync changed immutable deployment receipt' }
    Set-Item -Path function:Invoke-ReleaseFinalization -Value $finalizer
    Remove-Item -Path function:Read-Host
    $unfinished = Join-Path $installed 'localdata\sync_back\transactions\interrupted-fixture'
    New-Item -ItemType Directory -Path $unfinished | Out-Null
    [IO.File]::WriteAllText((Join-Path $unfinished 'state.json'), '{"status":"PREPARED"}')
    $blocked = $false
    try { Invoke-SyncBack $installed $destination -DryRun | Out-Null } catch { $blocked = $_.Exception.Message -like 'sync_back_pending_transaction_manual_recovery*' }
    if (-not $blocked) { throw 'interrupted transaction did not block later sync' }
    [IO.File]::WriteAllText((Join-Path $unfinished 'state.json'), '{"status":"ROLLED_BACK"}')
    foreach ($private in @('source/.env', 'doc/private.p12', '.ssh/id_rsa', '.aws/config', 'browser_profile/cookies')) {
        if (-not (Test-PrivateDeploymentPath $private) -or -not (Test-ExcludedRelativePath $private)) { throw "private path not excluded:$private" }
    }

    # A new deployment invalidates old sync comparison bases, even when
    # localdata and the same destination checkout have been retained.
    [IO.File]::WriteAllText($baseFile, 'new-deployment-version', (New-Object Text.UTF8Encoding($false)))
    $newDeploymentHash = Get-FileSha256 $baseFile
    Save-DeploymentBaseline $installed $destination @{'source/module.py'=$newDeploymentHash; 'source/new_module.py'=(Get-FileSha256 (Join-Path $installed 'source\new_module.py'))} 'new-deployment' 9
    $redeployedPlan = Get-SyncBackPlan $installed $destination
    if (@($redeployedPlan.changes).Count -ne 0 -or @($redeployedPlan.conflicts).Count -ne 0) { throw 'old sync history leaked into a new deployment baseline' }

    $projectRoot = Split-Path -Parent $installerRoot
    $finalizeRoot = Join-Path $temp 'finalize-release'
    & robocopy.exe $projectRoot $finalizeRoot '/E' '/R:1' '/W:1' '/XJ' `
        '/XD' '.git' '.venv' 'localdata' '.agents' '__pycache__' '.pytest_cache' `
        '/XF' '*.pyc' '*.pyo' '*.lnk' | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "sync-back finalize fixture copy failed: $LASTEXITCODE" }
    $baseline = Read-JsonFile (Join-Path $projectRoot 'config\update_manifest.json')
    $finalized = Invoke-ReleaseFinalization $projectRoot $finalizeRoot ([long]$baseline.release_sequence)
    if ([long]$finalized.release_sequence -le [long]$baseline.release_sequence) {
        throw 'release finalization did not advance the sequence'
    }
    if ([string]$finalized.release_id -notlike 'smartagent-v9-sync-back-*') {
        throw 'release finalization did not publish a sync-back release id'
    }
    Write-Output 'SMARTAGENT_SYNC_BACK_TEST_OK'
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
