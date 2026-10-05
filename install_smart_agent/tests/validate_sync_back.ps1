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

    [IO.File]::WriteAllText((Join-Path $destination 'source\module.py'), 'destination-change', (New-Object Text.UTF8Encoding($false)))
    $conflictPlan = Get-SyncBackPlan $installed $destination
    if (@($conflictPlan.conflicts).Count -ne 1 -or $conflictPlan.conflicts[0].relative -ne 'source/module.py') {
        throw 'two-sided edit conflict was not detected'
    }

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
