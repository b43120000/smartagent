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
    & $pre -ProjectRoot $project -Workspace $workspace -TurboRoot $root -IncludeSensitiveInventory |
        Where-Object { $_ -is [System.Collections.Specialized.OrderedDictionary] } |
        Select-Object -Last 1
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
    $snapshot=@(Inventory)
    Assert ($snapshot.Count -eq 1) ('TURBO_B_NO_INVENTORY_OBJECT:actual='+$snapshot.Count)
    Assert ($snapshot[0].schema -eq 'SMARTAGENT_TURBO_EXISTING_ROOT_PREFLIGHT_V1') 'TURBO_B_BAD_SCHEMA'
    Assert ($snapshot[0].count -eq 3) ('TURBO_B_UNEXPECTED_ENTRY_COUNT:'+ $snapshot[0].count)
    Assert ($snapshot[0].import_allowed_for_mutation -eq $false) 'TURBO_B_PRECHECK_MUST_NOT_GRANT'
    Assert ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hashBefore) 'TURBO_B_FILE_CHANGED'
    Assert ((Get-Acl -LiteralPath $root -Audit).Sddl -ceq $rootSddlBefore) 'TURBO_B_ROOT_SDDL_CHANGED'
    Assert ((Get-Acl -LiteralPath $file -Audit).Sddl -ceq $fileSddlBefore) 'TURBO_B_FILE_SDDL_CHANGED'
    Write-Host 'TURBO_B_EXISTING_ROOT_READONLY_INVENTORY_PASS'

    # Persist an immutable admin-only snapshot OUTSIDE the existing Turbo
    # project and prove no existing content/security was modified.
    $secure=Join-Path $project 'localdata\secure\windows_security'
    New-Item -ItemType Directory -Path $secure -Force | Out-Null
    $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=17
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($root)
        turbo_requested=$true
    }
    $modePath=Join-Path $secure 'acl_mode.json'
    $policyPath=Join-Path $secure 'workspace_access_policy.json'
    [IO.File]::WriteAllText($modePath,($mode | ConvertTo-Json -Depth 10))
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 10))
    $snapshotScript=Join-Path $PSScriptRoot '..\turbo_existing_root_snapshot.ps1'
    & $snapshotScript -ProjectRoot $project -Workspace $workspace -TurboRoot $root | Out-Null
    $backupDir=Join-Path $secure 'turbo_existing_root_snapshots'
    $snapshots=@(Get-ChildItem -LiteralPath $backupDir -Filter 'snapshot-*.json' -File)
    Assert ($snapshots.Count -eq 1) 'TURBO_B_PROTECTED_SNAPSHOT_MISSING_OR_MULTIPLE'
    $saved=Get-Content -LiteralPath $snapshots[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($saved.schema -eq 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1') 'TURBO_B_SNAPSHOT_SCHEMA_INVALID'
    Assert ($saved.authorization_to_mutate -eq $false) 'TURBO_B_SNAPSHOT_MUST_NOT_AUTHORIZE_MUTATION'
    Assert ($saved.entries_count -eq 3) 'TURBO_B_SNAPSHOT_ENTRIES_MISSING'
    $storedFile=@($saved.entries | Where-Object { $_.path -eq $file })
    Assert ($storedFile.Count -eq 1 -and $storedFile[0].sddl -ceq $fileSddlBefore) 'TURBO_B_SNAPSHOT_FILE_SDDL_NOT_EXACT'
    Assert ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hashBefore) 'TURBO_B_SNAPSHOT_CONTENT_MUTATED'
    Assert ((Get-Acl -LiteralPath $root -Audit).Sddl -ceq $rootSddlBefore) 'TURBO_B_SNAPSHOT_ROOT_ACL_MUTATED'
    Assert ((Get-Acl -LiteralPath $file -Audit).Sddl -ceq $fileSddlBefore) 'TURBO_B_SNAPSHOT_FILE_ACL_MUTATED'
    # A stale policy cannot create a new privileged backup.
    $policy.turbo_requested=$false
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 10))
    $rejected=$false
    try { & $snapshotScript -ProjectRoot $project -Workspace $workspace -TurboRoot $root | Out-Null }
    catch { $rejected=([string]$_.Exception.Message -match 'TURBO_B_SNAPSHOT_POLICY_NOT_REQUESTED') }
    Assert $rejected 'TURBO_B_SNAPSHOT_UNAUTHORIZED_POLICY_NOT_REJECTED'
    Assert (@(Get-ChildItem -LiteralPath $backupDir -Filter 'snapshot-*.json' -File).Count -eq 1) 'TURBO_B_UNAUTHORIZED_SNAPSHOT_CREATED'
    $policy.turbo_requested=$true
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 10))
    Write-Host 'TURBO_B_ADMIN_PROTECTED_SNAPSHOT_AND_POLICY_BINDING_PASS'

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
    # Windows PowerShell 5.1 Remove-Item can throw NullReferenceException for
    # junctions. cmd rmdir removes the reparse ENTRY, not its target.
    & cmd.exe /d /c ('rmdir "'+$junction+'"') | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'TURBO_B_FIXTURE_JUNCTION_REMOVE_FAILED'
    Assert (Test-Path -LiteralPath $workspace -PathType Container) 'TURBO_B_JUNCTION_TARGET_MODIFIED'
    Write-Host 'TURBO_B_REPARSE_AND_HARDLINK_FAIL_CLOSED_PASS'
} finally {
    if (Test-Path -LiteralPath $base) {
        Remove-Item -LiteralPath $base -Recurse -Force
    }
}
exit 0
