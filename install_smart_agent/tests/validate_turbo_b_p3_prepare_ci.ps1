# Real Windows CI: scheme B P3 preparation on an EXISTING populated Root.
# Absolutely NO user-root ACL mutation. All files are disposable RUNNER_TEMP.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'TURBO_B_P3_TEST_GITHUB_WINDOWS_ONLY'
}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('TurboBPrepare-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$workspace=Join-Path $fixture 'workspace'
$root=Join-Path $workspace 'existing_case'
$secure=Join-Path $project 'localdata\secure\windows_security'
$prepare=Join-Path $PSScriptRoot '..\turbo_existing_root_prepare.ps1'
$policyPath=Join-Path $secure 'workspace_access_policy.json'
function Assert([bool]$value,[string]$message) {if(-not $value){throw $message}}
try {
    New-Item -ItemType Directory -Force -Path $secure,$root | Out-Null
    $sub=Join-Path $root 'case_a'
    New-Item -ItemType Directory -Path $sub | Out-Null
    $file=Join-Path $sub 'result.json'
    [IO.File]::WriteAllText($file,'{"valid_count":2}')
    $digest=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $rootSddl=(Get-Acl -LiteralPath $root -Audit).Sddl
    $fileSddl=(Get-Acl -LiteralPath $file -Audit).Sddl
    $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=62
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@()
        turbo_requested=$false
    }
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),($mode | ConvertTo-Json -Depth 8))
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8))
    $prepareOutput=@(& $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $root -Action Prepare)
    Assert (@($prepareOutput | Where-Object {$_ -match 'TURBO_B_P3_PREPARE_PASS'}).Count -eq 1) 'TURBO_B_P3_PREPARE_DID_NOT_PASS'
    $backupDir=Join-Path $secure 'turbo_existing_root_snapshots'
    $files=@(Get-ChildItem -LiteralPath $backupDir -Filter 'snapshot-*.json' -File)
    Assert ($files.Count -eq 1) ('TURBO_B_P3_SNAPSHOT_COUNT_WRONG:'+ $files.Count)
    $snap=Get-Content -LiteralPath $files[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($snap.candidate_only -eq $true) 'TURBO_B_P3_CANDIDATE_ONLY_NOT_RECORDED'
    Assert ($snap.authorization_to_mutate -eq $false) 'TURBO_B_P3_CANDIDATE_MUTATION_FLAG'
    Assert ([int]$snap.entries_count -eq 3) 'TURBO_B_P3_MISSING_EXISTING_ROOT_ENTRIES'
    Assert ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $digest) 'TURBO_B_P3_PREPARE_CHANGED_FILE'
    Assert ((Get-Acl -LiteralPath $root -Audit).Sddl -ceq $rootSddl) 'TURBO_B_P3_PREPARE_CHANGED_ROOT_DACL'
    Assert ((Get-Acl -LiteralPath $file -Audit).Sddl -ceq $fileSddl) 'TURBO_B_P3_PREPARE_CHANGED_FILE_DACL'
    Assert ($policy.turbo_requested -eq $false) 'TURBO_B_P3_PREPARE_CHANGED_POLICY'
    $readPolicy=Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json
    Assert ($readPolicy.turbo_requested -eq $false) 'TURBO_B_P3_POLICY_CHANGED_ON_DISK'
    Write-Host 'TURBO_B_P3_POPULATED_ROOT_PREPARE_WITH_TURBO_OFF_PASS'

    $verify=@(& $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $root -Action Verify -SnapshotId ([string]$snap.snapshot_id))
    Assert (@($verify | Where-Object {$_ -match 'TURBO_B_P3_VERIFY_PASS entries=3'}).Count -eq 1) 'TURBO_B_P3_SECOND_VERIFY_NOT_PASS'
    Assert (@($verify | Where-Object {$_ -match 'P3_BROKER_BLOCKER=NONE'}).Count -eq 1) 'TURBO_B_P3_BROKER_UNEXPECTED'
    Write-Host 'TURBO_B_P3_INDEPENDENT_VERIFY_PASS'

    $extra=Join-Path $root 'new_unlisted.txt'
    [IO.File]::WriteAllText($extra,'new')
    $blocked=$false
    try { & $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $root -Action Verify -SnapshotId ([string]$snap.snapshot_id) | Out-Null }
    catch { $blocked=([string]$_.Exception.Message -match 'TURBO_B_P3_TREE_COUNT_MISMATCH') }
    Assert $blocked 'TURBO_B_P3_ADDED_FILE_NOT_DETECTED'
    Remove-Item -LiteralPath $extra -Force
    Write-Host 'TURBO_B_P3_POST_SNAPSHOT_NEW_FILE_BLOCKED_PASS'

    $policy.revision=63
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8))
    $blocked=$false
    try { & $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $root -Action Verify -SnapshotId ([string]$snap.snapshot_id) | Out-Null }
    catch { $blocked=([string]$_.Exception.Message -match 'TURBO_B_P3_SNAPSHOT_STALE_OR_UNTRUSTED') }
    Assert $blocked 'TURBO_B_P3_REVISION_NOT_REJECTED'
    $policy.revision=62
    $policy.turbo_roots=@(Join-Path $workspace 'active_other_root')
    $policy.turbo_requested=$true
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8))
    & $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $root -Action Verify -SnapshotId ([string]$snap.snapshot_id) | Out-Null
    Write-Host 'TURBO_B_P3_POLICY_REVISION_FAIL_CLOSED_AND_OTHER_ROOT_ALLOWED_PASS'

    $policy.denied_write_roots=@($root)
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8))
    $blocked=$false
    try { & $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $root -Action Prepare | Out-Null }
    catch { $blocked=([string]$_.Exception.Message -match 'TURBO_B_P3_PROTECTED_ROOT_OVERLAP') }
    Assert $blocked 'TURBO_B_P3_DENIED_ROOT_PROTECTION_BYPASSED'
    Assert (@(Get-ChildItem -LiteralPath $backupDir -Filter 'snapshot-*.json' -File).Count -eq 1) 'TURBO_B_P3_DENIED_ROOT_CREATED_SNAPSHOT'
    Write-Host 'TURBO_B_P3_DENIED_ROOT_NO_MUTATION_FAIL_CLOSED_PASS'
    Assert ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $digest) 'TURBO_B_P3_FINAL_FILE_HASH_CHANGED'
    Assert ((Get-Acl -LiteralPath $file -Audit).Sddl -ceq $fileSddl) 'TURBO_B_P3_FINAL_FILE_SECURITY_CHANGED'
    Assert ((Get-Acl -LiteralPath $root -Audit).Sddl -ceq $rootSddl) 'TURBO_B_P3_FINAL_ROOT_SECURITY_CHANGED'
    Write-Host 'TURBO_B_P3_WINDOWS_REAL_ROOT_READONLY_PREPARE_VERIFY_PASS'
} finally {
    if (Test-Path -LiteralPath $fixture -PathType Container) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
    }
}
