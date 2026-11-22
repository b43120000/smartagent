# P3-D handoff: strict read-only switch prerequisites, plus protected
# OLD-root full SDDL snapshot. Never alter workspace ACL or revoke a broker.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TargetTurboRoot,
    [Parameter(Mandatory=$true)][string]$SnapshotId,
    [Parameter(Mandatory=$true)][string]$ReviewId,
    [ValidateRange(1,100000)][int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { throw 'TURBO_B_P3D_EMPTY_PATH' }
    return [IO.Path]::GetFullPath($p).TrimEnd('\')
}
function Same([string]$a,[string]$b) {
    return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Under([string]$a,[string]$b) {
    return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)
}
function Check-Protected([string]$p,[bool]$protected) {
    if (-not (Test-Path -LiteralPath $p -PathType Any)) {
        throw ('TURBO_B_P3D_PROTECTED_STATE_MISSING:'+$p)
    }
    if ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw ('TURBO_B_P3D_PROTECTED_PATH_REPARSE:'+$p)
    }
    $acl=Get-Acl -LiteralPath $p -ErrorAction Stop
    if ($protected -and -not $acl.AreAccessRulesProtected) {
        throw 'TURBO_B_P3D_INHERITED_PROTECTED_DIR'
    }
    foreach($ace in @($acl.Access)) {
        if ($ace.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            try {
                $sid=$ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            } catch {throw 'TURBO_B_P3D_UNRESOLVABLE_PROTECTED_ACE'}
            if ($sid -notin @('S-1-5-18','S-1-5-32-544')) {
                throw 'TURBO_B_P3D_UNTRUSTED_PROTECTED_ACE'
            }
        }
    }
}
function Read-Json([string]$p) {
    return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'TURBO_B_P3D_ELEVATION_REQUIRED'
}
if ($SnapshotId -cnotmatch '^[0-9a-f]{32}$' -or $ReviewId -cnotmatch '^[0-9a-f]{32}$') {
    throw 'TURBO_B_P3D_INVALID_ID'
}
$project=Full $ProjectRoot
$workspace=Full $Workspace
$target=Full $TargetTurboRoot
if (-not (Under $target $workspace) -or (Under $workspace $project) -or
    (Under $project $workspace) -or (Under $project $target) -or
    (Under $target $project)) {
    throw 'TURBO_B_P3D_SCOPE_INVALID'
}
$secure=Join-Path $project 'localdata\secure\windows_security'
$reviewDir=Join-Path $secure 'turbo_broker_switch_reviews'
$snapshotDir=Join-Path $secure 'turbo_existing_root_snapshots'
$reviewPath=Join-Path $reviewDir ('review-'+$ReviewId+'.json')
$targetSnapshotPath=Join-Path $snapshotDir ('snapshot-'+$SnapshotId+'.json')
$journalPath=Join-Path $secure 'turbo_os_activation.json'
Check-Protected $reviewDir $true
Check-Protected $snapshotDir $true
Check-Protected $reviewPath $false
Check-Protected $targetSnapshotPath $false
if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
    throw 'TURBO_B_P3D_OLD_BROKER_JOURNAL_REQUIRED'
}
if ((Get-Item -LiteralPath $journalPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'TURBO_B_P3D_JOURNAL_REPARSE'
}
$review=Read-Json $reviewPath
$journal=Read-Json $journalPath
if ($review.schema -ne 'SMARTAGENT_TURBO_BROKER_SWITCH_REVIEW_V1' -or
    $review.status -ne 'ASSESSMENT_ONLY' -or
    $review.authorization_to_revoke -ne $false -or
    $review.authorization_to_mutate -ne $false -or
    $review.review_id -cne $ReviewId -or
    $review.snapshot_id -cne $SnapshotId -or
    -not (Same $review.install_root $project) -or
    -not (Same $review.workspace_root $workspace) -or
    -not (Same $review.target_turbo_root $target)) {
    throw 'TURBO_B_P3D_REVIEW_BINDING_MISMATCH'
}
if ($journal.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
    $journal.status -ne 'ACTIVE_PROVISIONED' -or
    -not (Same $journal.install_root $project) -or
    -not (Same $journal.workspace_root $workspace) -or
    -not (Same $journal.turbo_root $review.previous_turbo_root) -or
    [string]$journal.package_sid -cne [string]$review.previous_package_sid) {
    throw 'TURBO_B_P3D_BROKER_BINDING_CHANGED'
}
$journalHash=(Get-FileHash -LiteralPath $journalPath -Algorithm SHA256).Hash
if ($journalHash -cne [string]$review.original_journal_sha256) {
    throw 'TURBO_B_P3D_BROKER_JOURNAL_CHANGED_REASSESS_REQUIRED'
}
$old=Full ([string]$review.previous_turbo_root)
if (-not (Under $old $workspace) -or (Same $old $target) -or
    (Under $old $target) -or (Under $target $old)) {
    throw 'TURBO_B_P3D_OLD_ROOT_SCOPE_INVALID'
}
# Recheck the target inventory and its full SDDL, IDs and Policy Revision.
$prepare=Join-Path $PSScriptRoot 'turbo_existing_root_prepare.ps1'
& $prepare -Action Verify -ProjectRoot $project -Workspace $workspace -TurboRoot $target -SnapshotId $SnapshotId -MaxEntries $MaxEntries
if (-not $?) {throw 'TURBO_B_P3D_TARGET_VERIFICATION_FAILED'}
# Root must still grant the exact observed AppContainer package SID.
$oldSid=[string]$review.previous_package_sid
if ($oldSid -notmatch '^S-1-15-2-(?:[0-9]+-){2,}[0-9]+$') {
    throw 'TURBO_B_P3D_BROKER_PACKAGE_SID_INVALID'
}
$rootAcl=Get-Acl -LiteralPath $old -ErrorAction Stop
$found=$false
foreach($ace in @($rootAcl.Access)) {
    try {$value=$ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value}
    catch {throw 'TURBO_B_P3D_OLD_ROOT_ACE_UNRESOLVABLE'}
    if ($value -eq $oldSid -and $ace.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
        $found=$true
    }
}
if (-not $found) {throw 'TURBO_B_P3D_OLD_BROKER_ACE_MISSING'}
foreach($p in @($journal.traverse_parents)) {
    $parent=Full ([string]$p)
    if (-not ((Same $parent $workspace) -or (Under $parent $workspace)) -or (Under $parent $old)) {
        throw 'TURBO_B_P3D_TRAVERSE_OUTSIDE_WORKSPACE'
    }
    if (-not (Test-Path -LiteralPath $parent -PathType Container) -or
        ((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'TURBO_B_P3D_TRAVERSE_REPARSE_OR_MISSING'
    }
}
# CandidateOnly snapshot of OLD root: captures observed File ID, full SDDL,
# labels (as available), and topology without changing original ACL or files.
$writer=Join-Path $PSScriptRoot 'turbo_existing_root_snapshot.ps1'
$receipts=@(& $writer -ProjectRoot $project -Workspace $workspace -TurboRoot $old -CandidateOnly -MaxEntries $MaxEntries -EmitReceipt |
    Where-Object { $_.schema -eq 'SMARTAGENT_TURBO_B_SNAPSHOT_RECEIPT_V1' })
if ($receipts.Count -ne 1 -or $receipts[0].candidate_only -ne $true -or
    $receipts[0].authorization_to_mutate -ne $false) {
    throw 'TURBO_B_P3D_OLD_SNAPSHOT_NOT_TRUSTWORTHY'
}
$id=[string]$receipts[0].snapshot_id
$path=Join-Path $snapshotDir ('snapshot-'+$id+'.json')
Check-Protected $path $false
$readback=Read-Json $path
if ($readback.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1' -or
    $readback.snapshot_id -cne $id -or
    -not (Same $readback.turbo_root $old) -or
    [int]$readback.entries_count -ne [int]$receipts[0].entries_count) {
    throw 'TURBO_B_P3D_OLD_SNAPSHOT_READBACK_FAILED'
}
if ((Get-FileHash -LiteralPath $journalPath -Algorithm SHA256).Hash -cne $journalHash) {
    throw 'TURBO_B_P3D_JOURNAL_CHANGED_DURING_SCAN'
}
Write-Host 'TURBO_B_P3D_SWITCH_PRECONDITION_PASS'
Write-Host ('P3D_REVIEW_ID='+$ReviewId)
Write-Host ('P3D_TARGET_SNAPSHOT_ID='+$SnapshotId)
Write-Host ('P3D_OLD_ROOT='+$old)
Write-Host ('P3D_OLD_ROOT_SNAPSHOT_ID='+$id)
Write-Host ('P3D_OLD_ROOT_ENTRIES='+[int]$readback.entries_count)
Write-Host ('P3D_OLD_ROOT_SNAPSHOT_SHA256='+(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash)
Write-Host 'P3D_PROCESS_QUIESCENCE=NOT_ATTESTED'
Write-Host 'P3D_ATOMIC_SWITCH=NOT_IMPLEMENTED'
Write-Host 'P3D_REVOCATION=NOT_EXECUTED'
Write-Host 'P3D_EXISTING_ROOT_IMPORT=DISABLED'
Write-Host 'P3D_FULL_ACCESS=NOT_ENABLED'
