# P3-D3 Windows-only disposable staging and recovery integration.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or
   -not $env:RUNNER_TEMP){throw 'TURBO_D3_CI_ONLY'}
function Check([bool]$ok,[string]$error){if(-not $ok){throw $error}}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('TurboD3-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$ws=Join-Path $fixture 'workspace'
$root=Join-Path $ws 'existing'
$secure=Join-Path $project 'localdata\secure\windows_security'
$action=Join-Path $PSScriptRoot '..\turbo_existing_root_import.ps1'
$prepare=Join-Path $PSScriptRoot '..\turbo_existing_root_prepare.ps1'
$journal=Join-Path $secure 'turbo_existing_root_import.json'
try {
    New-Item -ItemType Directory -Force -Path $secure,$root | Out-Null
    $sub=Join-Path $root 'folder'
    New-Item -ItemType Directory -Path $sub | Out-Null
    $file=Join-Path $sub 'original.txt'
    [IO.File]::WriteAllText($file,'USER_DATA_SHA256_MUST_NOT_CHANGE')
    $originalHash=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $baseline=@{}
    foreach($path in @($ws,$root,$sub,$file)){
        $baseline[$path]=(Get-Acl -LiteralPath $path -Audit -ErrorAction Stop).Sddl
    }
    $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=4
        writable_workspaces=@($ws)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($root)
        turbo_requested=$true
    }
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),($mode | ConvertTo-Json -Depth 8))
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),($policy | ConvertTo-Json -Depth 8))
    & $prepare -Action Prepare -ProjectRoot $project -Workspace $ws -TurboRoot $root | Out-Null
    $snaps=@(Get-ChildItem -LiteralPath (Join-Path $secure 'turbo_existing_root_snapshots') -Filter 'snapshot-*.json' -File)
    Check ($snaps.Count -eq 1) 'TURBO_D3_CI_SNAPSHOT_MISSING'
    $saved=Get-Content -LiteralPath $snaps[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    $sid=[string]$saved.snapshot_id
    $rejected=$false
    try {
        & $action -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $root -SnapshotId $sid | Out-Null
    } catch {
        $rejected=$_.Exception.Message -match 'TURBO_D3_PRODUCTION_ACTIVATION_NOT_YET_AUTHORIZED'
    }
    Check $rejected 'TURBO_D3_UNCONTROLLED_PRODUCTION_ACTIVATION_ACCEPTED'
    Check (-not (Test-Path -LiteralPath $journal)) 'TURBO_D3_GUARD_CREATED_JOURNAL'
    Write-Host 'TURBO_D3_CI_PRODUCTION_GUARD_PASS'

    & $action -Action Activate -ExperimentalCiOnly -ProjectRoot $project -Workspace $ws -TurboRoot $root -SnapshotId $sid
    Check (Test-Path -LiteralPath $journal -PathType Leaf) 'TURBO_D3_IMPORT_JOURNAL_NOT_WRITTEN'
    $receipt=Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json
    Check ($receipt.status -eq 'ACTIVE' -and $receipt.snapshot_id -eq $sid) 'TURBO_D3_STAGED_RECEIPT_UNTRUSTED'
    Check ((Get-Acl -LiteralPath $file -Audit).Sddl -match [Regex]::Escape([string]$receipt.package_sid)) 'TURBO_D3_GRANT_MISSING'
    Check ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $originalHash) 'TURBO_D3_STAGE_CHANGED_USER_CONTENT'
    Write-Host 'TURBO_D3_CI_EXISTING_ROOT_STAGED_PASS'
    # A newly created user file must remain after revocation.
    $new=Join-Path $sub 'user_new.txt'
    [IO.File]::WriteAllText($new,'KEEP_ME_AFTER_REVOKE')
    # The same native OS lifecycle used by ACLstatus.bat OFF must delegate
    # to the imported-root exact-SDDL/IL revoker, NOT its legacy M-level reset.
    $native=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
    & $native -Action Revoke -ProjectRoot $project
    Check (-not (Test-Path -LiteralPath $journal)) 'TURBO_D3_REVOKE_JOURNAL_REMAINS'
    Check (-not (Test-Path -LiteralPath (Join-Path $secure 'turbo_os_activation.json'))) 'TURBO_D3_ACTIVATION_JOURNAL_REMAINS'
    Check (Test-Path -LiteralPath $new -PathType Leaf) 'TURBO_D3_NEW_USER_FILE_DELETED'
    Check ([IO.File]::ReadAllText($new) -ceq 'KEEP_ME_AFTER_REVOKE') 'TURBO_D3_NEW_USER_FILE_CORRUPTED'
    foreach($path in @($ws,$root,$sub,$file)){
        Check ((Get-Acl -LiteralPath $path -Audit).Sddl -ceq $baseline[$path]) ('TURBO_D3_OLD_SDDL_DIFF:'+ $path)
    }
    Check ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $originalHash) 'TURBO_D3_ORIGINAL_HASH_DIFF'
    Write-Host 'TURBO_D3_CI_USER_FILE_AND_FULL_SDDL_RESTORED_PASS'
} finally {
    if(Test-Path -LiteralPath $journal -PathType Leaf){
        try {& $action -Action Recover -ProjectRoot $project | Out-Null}
        catch{Write-Warning ('TURBO_D3_CI_RECOVER_FAILED:'+ $_.Exception.Message)}
    }
    if(Test-Path -LiteralPath $journal -PathType Leaf) {
        Write-Warning ('TURBO_D3_CI_RETAINED_UNRECOVERED_ROOT:'+$fixture)
    } elseif(Test-Path -LiteralPath $fixture -PathType Container) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
    }
}
