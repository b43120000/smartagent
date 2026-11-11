# Turbo B P3-C: production READ-ONLY broker switch review.
# Does NOT revoke a package, modify ACL/IL, switch Policy, or import Root.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TargetTurboRoot,
    [Parameter(Mandatory=$true)][string]$SnapshotId,
    [ValidateSet('Assess','VerifyRevoked')][string]$Action='Assess',
    [string]$ReviewId='',
    [ValidateRange(1,100000)][int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { throw 'TURBO_B_P3C_EMPTY_PATH' }
    return [IO.Path]::GetFullPath($path).TrimEnd('\')
}
function Same([string]$a,[string]$b) {
    return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Under([string]$a,[string]$b) {
    return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)
}
function Read-Json([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw ('TURBO_B_P3C_REQUIRED_STATE_MISSING:'+$path)
    }
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
function Check-ReviewDirectory([string]$path) {
    No-Reparse $path
    $acl=Get-Acl -LiteralPath $path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) {
        throw 'TURBO_B_P3C_REVIEW_DIR_INHERITED_ACL'
    }
    foreach($ace in @($acl.Access)) {
        if ($ace.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            $sid=$ace.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin @('S-1-5-18','S-1-5-32-544')) {
                throw 'TURBO_B_P3C_REVIEW_DIR_UNSAFE_ALLOW_ACE'
            }
        }
    }
}
function No-Reparse([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw ('TURBO_B_P3C_PATH_MISSING:'+$path)
    }
    if ((Get-Item -LiteralPath $path -Force -ErrorAction Stop).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw ('TURBO_B_P3C_REPARSE_COMPONENT:'+$path)
    }
}
function SidPresent([string]$path,[string]$sid) {
    $acl=Get-Acl -LiteralPath $path -ErrorAction Stop
    foreach($rule in @($acl.Access)) {
        try {
            $found=$rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]).Value
        } catch {throw 'TURBO_B_P3C_ACL_SID_UNRESOLVABLE'}
        if ($found -eq $sid) { return $true }
    }
    return $false
}
function Check-NoPackageGrants([string]$root,[string]$sid) {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw 'TURBO_B_P3C_OLD_ROOT_MISSING_REQUIRES_MANUAL_REVIEW'
    }
    $entries=@(Get-Item -LiteralPath $root -Force -ErrorAction Stop) +
        @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)
    if ($entries.Count -gt $MaxEntries) {throw 'TURBO_B_P3C_ENTRY_LIMIT_EXCEEDED'}
    foreach($node in $entries) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'TURBO_B_P3C_OLD_ROOT_REPARSE_FOUND'
        }
        if (SidPresent ([string]$node.FullName) $sid) {
            throw ('TURBO_B_P3C_OLD_PACKAGE_ACE_STILL_PRESENT:'+$node.FullName)
        }
    }
    return $entries.Count
}
$principal=[Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'TURBO_B_P3C_ELEVATED_ADMIN_REQUIRED'
}
$project=Full $ProjectRoot
$workspace=Full $Workspace
$target=Full $TargetTurboRoot
if (-not (Under $target $workspace) -or
    (Under $workspace $project) -or (Under $project $workspace) -or
    (Under $target $project) -or (Under $project $target)) {
    throw 'TURBO_B_P3C_TARGET_SCOPE_INVALID'
}
$secure=Join-Path $project 'localdata\secure\windows_security'
$journal=Join-Path $secure 'turbo_os_activation.json'
$reviewDir=Join-Path $secure 'turbo_broker_switch_reviews'
$prepare=Join-Path $PSScriptRoot 'turbo_existing_root_prepare.ps1'
if (-not (Test-Path -LiteralPath $prepare -PathType Leaf)) {
    throw 'TURBO_B_P3C_PREPARE_VERIFIER_MISSING'
}
& $prepare -Action Verify -ProjectRoot $project -Workspace $workspace -TurboRoot $target -SnapshotId $SnapshotId -MaxEntries $MaxEntries
if (-not $?) { throw 'TURBO_B_P3C_TARGET_VERIFY_FAILED' }
if ($Action -eq 'VerifyRevoked') {
    if ($ReviewId -notmatch '^[0-9a-f]{32}$') {
        throw 'TURBO_B_P3C_REVIEW_ID_REQUIRED'
    }
    Check-ReviewDirectory $reviewDir
    $reviewPath=Join-Path $reviewDir ('review-'+$ReviewId+'.json')
    No-Reparse $reviewPath
    $review=Read-Json $reviewPath
    if ($review.schema -ne 'SMARTAGENT_TURBO_BROKER_SWITCH_REVIEW_V1' -or
        $review.status -ne 'ASSESSMENT_ONLY' -or
        $review.authorization_to_revoke -ne $false -or
        $review.authorization_to_mutate -ne $false -or
        $review.review_id -cne $ReviewId -or
        $review.snapshot_id -cne $SnapshotId -or
        -not (Same $review.install_root $project) -or
        -not (Same $review.workspace_root $workspace) -or
        -not (Same $review.target_turbo_root $target)) {
        throw 'TURBO_B_P3C_REVIEW_BINDING_INVALID'
    }
    $sid=[string]$review.previous_package_sid
    if ($sid -notmatch '^S-1-15-2-(?:[0-9]+-){2,}[0-9]+$') {
        throw 'TURBO_B_P3C_PREVIOUS_PACKAGE_SID_INVALID'
    }
    $old=Full ([string]$review.previous_turbo_root)
    if (-not (Under $old $workspace) -or (Same $old $target) -or
        (Under $old $target) -or (Under $target $old)) {
        throw 'TURBO_B_P3C_PREVIOUS_SCOPE_INVALID'
    }
    if (Test-Path -LiteralPath $journal) {
        throw 'TURBO_B_P3C_REVOCATION_JOURNAL_STILL_PRESENT'
    }
    $count=Check-NoPackageGrants $old $sid
    $ancestor=[IO.Path]::GetDirectoryName($old)
    while ($ancestor -and
        ((Same $ancestor $workspace) -or (Under $ancestor $workspace))) {
        No-Reparse $ancestor
        if (SidPresent $ancestor $sid) {
            throw ('TURBO_B_P3C_PARENT_PACKAGE_ACE_STILL_PRESENT:'+$ancestor)
        }
        if (Same $ancestor $workspace) { break }
        $ancestor=[IO.Path]::GetDirectoryName($ancestor)
    }
    Write-Host ('TURBO_B_P3C_VERIFIED_OLD_PACKAGE_ACE_REMOVED entries='+$count)
    Write-Host 'TURBO_B_P3C_REVOCATION_VERIFIED_JOURNAL_ABSENT'
    Write-Host ('P3C_REVIEW_ID='+$ReviewId)
    Write-Host 'P3C_EXISTING_ROOT_IMPORT=NOT_ENABLED'
    Write-Host 'P3C_FULL_ACCESS=NOT_ENABLED'
    return
}
if ($ReviewId) {
    throw 'TURBO_B_P3C_REVIEW_ID_ONLY_FOR_VERIFY_REVOKED'
}
if (-not (Test-Path -LiteralPath $journal -PathType Leaf)) {
    Write-Host 'TURBO_B_P3C_ASSESS_PASS'
    Write-Host 'P3C_OLD_BROKER=NONE'
    Write-Host 'P3C_REVOKE_REQUIRED=NO'
    Write-Host 'P3C_EXISTING_ROOT_IMPORT=NOT_ENABLED'
    Write-Host 'P3C_FULL_ACCESS=NOT_ENABLED'
    return
}
No-Reparse $journal
$record=Read-Json $journal
if ($record.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
    $record.status -ne 'ACTIVE_PROVISIONED' -or
    -not (Same $record.install_root $project) -or
    -not (Same $record.workspace_root $workspace)) {
    throw 'TURBO_B_P3C_BROKER_JOURNAL_IDENTITY_MISMATCH'
}
$old=Full ([string]$record.turbo_root)
if (-not (Under $old $workspace) -or
    (Same $old $target) -or (Under $old $target) -or
    (Under $target $old)) {
    throw 'TURBO_B_P3C_OLD_BROKER_SCOPE_CONFLICT'
}
$sid=[string]$record.package_sid
if ($sid -notmatch '^S-1-15-2-(?:[0-9]+-){2,}[0-9]+$') {
    throw 'TURBO_B_P3C_BROKER_PACKAGE_SID_INVALID'
}
if (-not (Test-Path -LiteralPath $old -PathType Container)) {
    throw 'TURBO_B_P3C_OLD_BROKER_ROOT_MISSING'
}
No-Reparse $old
if (-not (SidPresent $old $sid)) {
    throw 'TURBO_B_P3C_OLD_BROKER_ROOT_GRANT_NOT_FOUND'
}
foreach($parent in @($record.traverse_parents)) {
    $p=Full ([string]$parent)
    if (-not ((Same $p $workspace) -or (Under $p $workspace)) -or
        (Under $p $old)) {
        throw 'TURBO_B_P3C_BROKER_TRAVERSE_SCOPE_INVALID'
    }
    No-Reparse $p
}
$journalSha=(Get-FileHash -LiteralPath $journal -Algorithm SHA256).Hash
foreach($p in @($project,(Join-Path $project 'localdata'),(Join-Path $project 'localdata\secure'),$secure)) {
    No-Reparse $p
}
if (-not (Test-Path -LiteralPath $reviewDir -PathType Container)) {
    New-Item -ItemType Directory -Path $reviewDir -ErrorAction Stop | Out-Null
}
No-Reparse $reviewDir
& icacls.exe $reviewDir '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'TURBO_B_P3C_REVIEW_DIR_PROTECTION_FAILED' }
Check-ReviewDirectory $reviewDir
$id=[Guid]::NewGuid().ToString('N')
$reviewPath=Join-Path $reviewDir ('review-'+$id+'.json')
$receipt=[ordered]@{
    schema='SMARTAGENT_TURBO_BROKER_SWITCH_REVIEW_V1'
    status='ASSESSMENT_ONLY'
    authorization_to_revoke=$false
    authorization_to_mutate=$false
    review_id=$id
    snapshot_id=$SnapshotId
    install_root=$project
    workspace_root=$workspace
    target_turbo_root=$target
    previous_turbo_root=$old
    previous_package_sid=$sid
    original_journal_sha256=$journalSha
    created_at=[DateTimeOffset]::UtcNow.ToString('o')
}
$data=[Text.Encoding]::UTF8.GetBytes(($receipt | ConvertTo-Json -Depth 8))
$stream=[IO.FileStream]::new($reviewPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try {$stream.Write($data,0,$data.Length);$stream.Flush($true)}
finally {$stream.Dispose()}
# Do not expose this receipt if its DACL does not inherit exclusively from a
# verified admin-only directory.
$reviewAcl=Get-Acl -LiteralPath $reviewPath -ErrorAction Stop
foreach($ace in @($reviewAcl.Access)) {
    if ($ace.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
        $aceSid=$ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($aceSid -notin @('S-1-5-18','S-1-5-32-544')) {
            throw 'TURBO_B_P3C_REVIEW_FILE_UNSAFE_ALLOW_ACE'
        }
    }
}
Write-Host 'TURBO_B_P3C_ASSESS_PASS'
Write-Host ('P3C_REVIEW_ID='+$id)
Write-Host 'P3C_OLD_BROKER=ACTIVE_PROVISIONED'
Write-Host ('P3C_OLD_ROOT='+$old)
Write-Host ('P3C_PACKAGE_SID='+$sid)
Write-Host ('P3C_BROKER_JOURNAL_SHA256='+$journalSha)
Write-Host 'P3C_REVOKE_REQUIRED=YES'
Write-Host 'P3C_REVOCATION_ACTION=NOT_AUTOMATIC'
Write-Host 'P3C_EXISTING_ROOT_IMPORT=NOT_ENABLED'
Write-Host 'P3C_FULL_ACCESS=NOT_ENABLED'
