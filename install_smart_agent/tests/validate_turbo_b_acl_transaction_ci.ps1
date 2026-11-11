# Real Windows CI: guarded scheme B NTFS ACL transition and rollback.
# Executes only under GitHub disposable RUNNER_TEMP; does not touch user data.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'TURBO_B_TRANSACTION_TEST_CI_ONLY'
}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('TurboBTransaction-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$workspace=Join-Path $fixture 'workspace'
$root=Join-Path $workspace 'existing'
$secure=Join-Path $project 'localdata\secure\windows_security'
$modePath=Join-Path $secure 'acl_mode.json'
$policyPath=Join-Path $secure 'workspace_access_policy.json'
$snapshotScript=Join-Path $PSScriptRoot '..\turbo_existing_root_snapshot.ps1'
$transaction=Join-Path $PSScriptRoot '..\turbo_existing_root_transaction_rehearsal.ps1'
$probe=Join-Path $PSScriptRoot '..\turbo_appcontainer_probe.cs'
$moniker=''
function Assert([bool]$value,[string]$why) {
    if (-not $value) { throw $why }
}
function Check-Tree([string]$snapshotFile,[string]$sid,[string]$file,[string]$contentHash) {
    $snapshot=Get-Content -LiteralPath $snapshotFile -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in @($snapshot.entries)) {
        $actual=Get-Acl -LiteralPath ([string]$entry.path) -Audit -ErrorAction Stop
        Assert ($actual.Sddl -ceq [string]$entry.sddl) ('TURBO_B_RESTORED_SDDL_MISMATCH:'+$entry.path)
        Assert ($actual.Sddl -notmatch [Regex]::Escape($sid)) ('TURBO_B_TEST_SID_LEAKED:'+$entry.path)
    }
    Assert ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $contentHash) 'TURBO_B_REHEARSAL_FILE_CONTENT_CHANGED'
}
try {
    New-Item -ItemType Directory -Path $secure,$root -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixture '.turbo-b-ci-disposable'),'DISPOSABLE_FIXTURE')
    $nested=Join-Path $root 'case_a'
    New-Item -ItemType Directory -Path $nested | Out-Null
    $file=Join-Path $nested 'data.csv'
    [IO.File]::WriteAllText($file,('item,value'+[Environment]::NewLine+'x,7'+[Environment]::NewLine))
    $digest=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $mode=[ordered]@{
        schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project
    }
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=54
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($root)
        turbo_requested=$true
    }
    [IO.File]::WriteAllText($modePath,($mode | ConvertTo-Json -Depth 12))
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 12))
    & $snapshotScript -ProjectRoot $project -Workspace $workspace -TurboRoot $root | Out-Null
    $backupDir=Join-Path $secure 'turbo_existing_root_snapshots'
    $backups=@(Get-ChildItem -LiteralPath $backupDir -File -Filter 'snapshot-*.json')
    Assert ($backups.Count -eq 1) 'TURBO_B_CI_SNAPSHOT_NOT_CREATED'
    $backup=$backups[0].FullName
    if (-not ('SmartAgentGit47AppContainerProbe' -as [type])) {
        Add-Type -Path $probe -ErrorAction Stop
    }
    $moniker='TurboB_Ci_'+[Guid]::NewGuid().ToString('N')
    $sid=[SmartAgentGit47AppContainerProbe]::CreateProfile($moniker)
    Assert ($sid -match '^S-1-15-2-') 'TURBO_B_CI_APP_CONTAINER_SID_INVALID'

    & $transaction -ProjectRoot $project -Workspace $workspace -TurboRoot $root -SnapshotPath $backup -AppContainerSid $sid
    Check-Tree $backup $sid $file $digest
    Write-Host 'TURBO_B_CI_SDDL_COMPLETE_ROUNDTRIP_PASS'

    & $transaction -ProjectRoot $project -Workspace $workspace -TurboRoot $root -SnapshotPath $backup -AppContainerSid $sid -InjectFailureAfter 1
    Check-Tree $backup $sid $file $digest
    Write-Host 'TURBO_B_CI_INJECTED_FAILURE_EXACT_SDDL_RESTORE_PASS'

    # A stale policy revision must block before even a single Set-Acl.
    $policy.revision=55
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 12))
    $blocked=$false
    try {
        & $transaction -ProjectRoot $project -Workspace $workspace -TurboRoot $root -SnapshotPath $backup -AppContainerSid $sid
    } catch {
        $blocked=$_.Exception.Message -match 'TURBO_B_TRANSACTION_POLICY_STALE'
    }
    Assert $blocked 'TURBO_B_CI_STALE_POLICY_NOT_BLOCKED'
    Check-Tree $backup $sid $file $digest
    Write-Host 'TURBO_B_CI_TRANSACTION_POLICY_STALE_FAIL_CLOSED_PASS'
} finally {
    if ($moniker) {
        try { [SmartAgentGit47AppContainerProbe]::DeleteProfile($moniker) } catch {}
    }
    if (Test-Path -LiteralPath $fixture -PathType Container) {
        Remove-Item -LiteralPath $fixture -Recurse -Force
    }
}
