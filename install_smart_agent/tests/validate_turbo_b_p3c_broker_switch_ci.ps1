# P3-C real Windows CI: review + revoke disposable old AppContainer broker.
# Uses only disposable RUNNER_TEMP roots. Existing target must remain unchanged.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows' -or
    [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    throw 'TURBO_B_P3C_TEST_REQUIRES_WINDOWS_CI'
}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('TurboBReview-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$secure=Join-Path $project 'localdata\secure\windows_security'
$workspace=Join-Path $fixture 'workspace'
$oldRoot=Join-Path $workspace 'turbo_old_new'
$target=Join-Path $workspace 'existing_target'
$auto=Join-Path $PSScriptRoot '..\turbo_auto_activate.ps1'
$activation=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
$prepare=Join-Path $PSScriptRoot '..\turbo_existing_root_prepare.ps1'
$review=Join-Path $PSScriptRoot '..\turbo_broker_switch_checkpoint.ps1'
$journal=Join-Path $secure 'turbo_os_activation.json'
function Assert([bool]$value,[string]$reason) {
    if (-not $value) { throw $reason }
}
try {
    New-Item -ItemType Directory -Force -Path $secure,$workspace,$target | Out-Null
    $existingFile=Join-Path $target 'case_a\process.py'
    New-Item -ItemType Directory -Path (Split-Path -Parent $existingFile) -Force | Out-Null
    [IO.File]::WriteAllText($existingFile,'P3C_UNTOUCHED_TARGET')
    $targetHash=(Get-FileHash -LiteralPath $existingFile -Algorithm SHA256).Hash
    $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=88
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($oldRoot)
        turbo_requested=$true
    }
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),($mode | ConvertTo-Json -Depth 8))
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),($policy | ConvertTo-Json -Depth 8))
    & $auto -ProjectRoot $project -Action Apply | Out-Null
    Assert (Test-Path -LiteralPath $journal -PathType Leaf) 'TURBO_B_P3C_OLD_ROOT_ACTIVATION_MISSING'
    $oldRecord=Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($oldRecord.status -eq 'ACTIVE_PROVISIONED') 'TURBO_B_P3C_OLD_ROOT_NOT_ACTIVE'
    # Baseline AFTER old broker activation. Parent DACL provisioning can
    # cause inherited control bits on existing sibling trees.
    $targetSddl=(Get-Acl -LiteralPath $existingFile -Audit).Sddl
    $targetRootSddl=(Get-Acl -LiteralPath $target -Audit).Sddl
    $oldSid=[string]$oldRecord.package_sid
    $data=Join-Path $oldRoot 'do_not_delete_user_data.txt'
    [IO.File]::WriteAllText($data,'P3C_PRESERVE_OLD_USER_OUTPUT')
    $oldHash=(Get-FileHash -LiteralPath $data -Algorithm SHA256).Hash

    & $prepare -ProjectRoot $project -Workspace $workspace -TurboRoot $target -Action Prepare | Out-Null
    $snapshots=@(Get-ChildItem -LiteralPath (Join-Path $secure 'turbo_existing_root_snapshots') -Filter 'snapshot-*.json' -File)
    Assert ($snapshots.Count -eq 1) 'TURBO_B_P3C_SNAPSHOT_NOT_CREATED'
    $targetSnapshot=Get-Content -LiteralPath $snapshots[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    $snapId=[string]$targetSnapshot.snapshot_id
    & $review -Action Assess -ProjectRoot $project -Workspace $workspace -TargetTurboRoot $target -SnapshotId $snapId
    $reviewDir=Join-Path $secure 'turbo_broker_switch_reviews'
    $receipts=@(Get-ChildItem -LiteralPath $reviewDir -Filter 'review-*.json' -File)
    Assert ($receipts.Count -eq 1) 'TURBO_B_P3C_REVIEW_NOT_CREATED'
    $receipt=Get-Content -LiteralPath $receipts[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($receipt.authorization_to_mutate -eq $false -and
        $receipt.authorization_to_revoke -eq $false -and
        $receipt.previous_package_sid -eq $oldSid -and
        $receipt.status -eq 'ASSESSMENT_ONLY') 'TURBO_B_P3C_REVIEW_WAS_NOT_READONLY'
    $reviewId=[string]$receipt.review_id
    Assert ((Get-FileHash -LiteralPath $existingFile -Algorithm SHA256).Hash -eq $targetHash) 'TURBO_B_P3C_ASSESS_CHANGED_TARGET'
    Assert ((Get-Acl -LiteralPath $existingFile -Audit).Sddl -ceq $targetSddl) 'TURBO_B_P3C_ASSESS_CHANGED_TARGET_ACL'
    Write-Host 'TURBO_B_P3C_OLD_BROKER_SCOPE_AND_BINDING_REVIEW_PASS'

    $blocked=$false
    try { & $review -Action VerifyRevoked -ProjectRoot $project -Workspace $workspace -TargetTurboRoot $target -SnapshotId $snapId -ReviewId $reviewId | Out-Null }
    catch { $blocked=$_.Exception.Message -match 'TURBO_B_P3C_REVOCATION_JOURNAL_STILL_PRESENT' }
    Assert $blocked 'TURBO_B_P3C_ACCEPTED_ACTIVE_JOURNAL_AS_REVOKED'
    Write-Host 'TURBO_B_P3C_ACTIVE_OLD_BROKER_FAIL_CLOSED_PASS'

    & $activation -Action Revoke -ProjectRoot $project | Out-Null
    Assert (-not (Test-Path -LiteralPath $journal)) 'TURBO_B_P3C_REVOCATION_JOURNAL_REMAINS'
    Assert ((Get-FileHash -LiteralPath $data -Algorithm SHA256).Hash -eq $oldHash) 'TURBO_B_P3C_REVOKE_DELETED_OLD_USER_OUTPUT'
    Assert ((Get-FileHash -LiteralPath $existingFile -Algorithm SHA256).Hash -eq $targetHash) 'TURBO_B_P3C_REVOKE_MODIFIED_TARGET_CONTENT'
    Assert ((Get-Acl -LiteralPath $existingFile -Audit).Sddl -ceq $targetSddl) 'TURBO_B_P3C_REVOKE_MODIFIED_TARGET_ACL'
    Assert ((Get-Acl -LiteralPath $target -Audit).Sddl -ceq $targetRootSddl) 'TURBO_B_P3C_REVOKE_MODIFIED_TARGET_ROOT_ACL'
    & $review -Action VerifyRevoked -ProjectRoot $project -Workspace $workspace -TargetTurboRoot $target -SnapshotId $snapId -ReviewId $reviewId
    Write-Host 'TURBO_B_P3C_VERIFIED_REVOCATION_PRESERVES_BOTH_ROOTS_PASS'

    $blocked=$false
    try { & $review -Action VerifyRevoked -ProjectRoot $project -Workspace $workspace -TargetTurboRoot $target -SnapshotId $snapId -ReviewId 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' | Out-Null }
    catch { $blocked=$true }
    Assert $blocked 'TURBO_B_P3C_ARBITRARY_REVIEW_ID_ACCEPTED'
    Write-Host 'TURBO_B_P3C_REVIEW_ID_NOT_FORGEABLE_BY_INPUT_PASS'
    $policy.turbo_roots=@($target)
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),($policy | ConvertTo-Json -Depth 8))
    $blocked=$false
    try { & $auto -ProjectRoot $project -Action Apply | Out-Null }
    catch { $blocked=$_.Exception.Message -match 'TURBO_AUTOSTART_EXISTING_ROOT_IMPORT_UNSUPPORTED' }
    Assert $blocked 'TURBO_B_P3C_EXISTING_ROOT_IMPORT_UNSAFELY_ENABLED'
    Write-Host 'TURBO_B_P3C_EXISTING_ROOT_IMPORT_STILL_FAIL_CLOSED_PASS'
} finally {
    if (Test-Path -LiteralPath $journal -PathType Leaf) {
        try { & $activation -Action Revoke -ProjectRoot $project | Out-Null }
        catch { Write-Warning 'TURBO_B_P3C_DISPOSABLE_REVOKE_FAILED_FOR_INSPECTION' }
    }
    if (Test-Path -LiteralPath $fixture -PathType Container -ErrorAction SilentlyContinue) {
        if (-not (Test-Path -LiteralPath $journal)) {
            Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
        }
    }
}
