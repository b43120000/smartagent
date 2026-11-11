# Windows read-only tests for Turbo scheme B existing-root preflight.
[CmdletBinding()]
param([string]$FixtureParent=$env:RUNNER_TEMP)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if (-not $FixtureParent) { $FixtureParent=[IO.Path]::GetTempPath() }
$base=Join-Path ([IO.Path]::GetFullPath($FixtureParent)) ('TurboBPreflight-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $base 'project'
$workspace=Join-Path $base 'workspace'
$root=Join-Path $workspace 'existing'
$pre=Join-Path $PSScriptRoot '..\turbo_existing_root_preflight.ps1'
function Assert([bool]$Value,[string]$Message) { if (-not $Value) { throw $Message } }
function Inventory {
    return @(& $pre -ProjectRoot $project -Workspace $workspace -TurboRoot $root |
        Where-Object { $_ -is [System.Collections.Specialized.OrderedDictionary] } |
        Select-Object -Last 1)
}
function Expected-Block([string]$Needle) {
    $blocked=$false
    try { $null=Inventory }
    catch { $blocked=([string]$_.Exception.Message -match [Regex]::Escape($Needle)) }
    Assert $blocked ('TURBO_B_EXPECTED_BLOCK_MISSING:'+$Needle)
}
try {
    New-Item -ItemType Directory -Force -Path $project,$root | Out-Null
    $nested=Join-Path $root 'case_a'
    New-Item -ItemType Directory -Force -Path $nested | Out-Null
    $file=Join-Path $nested 'process.py'
    [IO.File]::WriteAllText($file,'ORIGINAL_CONTENT')
    $hashBefore=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $rootSddlBefore=(Get-Acl -LiteralPath $root -Audit).Sddl
    $fileSddlBefore=(Get-Acl -LiteralPath $file -Audit).Sddl
    $snapshot=Inventory
    Assert ($snapshot.Count -eq 1) 'TURBO_B_NO_INVENTORY_OBJECT'
    Assert ($snapshot[0].schema -eq 'SMARTAGENT_TURBO_EXISTING_ROOT_PREFLIGHT_V1') 'TURBO_B_BAD_SCHEMA'
    Assert ($snapshot[0].count -eq 3) ('TURBO_B_UNEXPECTED_ENTRY_COUNT:'+ $snapshot[0].count)
    Assert ($snapshot[0].import_allowed_for_mutation -eq $false) 'TURBO_B_PRECHECK_MUST_NOT_GRANT'
    Assert ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hashBefore) 'TURBO_B_FILE_CHANGED'
    Assert ((Get-Acl -LiteralPath $root -Audit).Sddl -ceq $rootSddlBefore) 'TURBO_B_ROOT_SDDL_CHANGED'
    Assert ((Get-Acl -LiteralPath $file -Audit).Sddl -ceq $fileSddlBefore) 'TURBO_B_FILE_SDDL_CHANGED'
    Write-Host 'TURBO_B_EXISTING_ROOT_READONLY_INVENTORY_PASS'
    # Real NTFS hardlink on a disposable copy: refuse escape aliases.
    $alias=Join-Path $workspace 'external_hardlink.py'
    & fsutil.exe hardlink create $alias $file | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'TURBO_B_FIXTURE_HARDLINK_CREATE_FAILED'
    Expected-Block 'TURBO_B_HARDLINK_FORBIDDEN'
    Remove-Item -LiteralPath $alias -Force
    # A junction must never be traversed into a different path.
    $junction=Join-Path $root 'unsafe_junction'
    & cmd.exe /d /c ('mklink /J "'+$junction+'" "'+$workspace+'"') | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'TURBO_B_FIXTURE_JUNCTION_CREATE_FAILED'
    Expected-Block 'TURBO_B_PREFLIGHT_REPARSE_ENTRY'
    Remove-Item -LiteralPath $junction -Force
    Write-Host 'TURBO_B_REPARSE_AND_HARDLINK_FAIL_CLOSED_PASS'
} finally {
    if (Test-Path -LiteralPath $base) {
        Remove-Item -LiteralPath $base -Recurse -Force
    }
}
exit 0
