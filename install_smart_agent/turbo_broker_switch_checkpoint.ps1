# Turbo B P3-C: production READ-ONLY broker switch review.
# Does NOT revoke a package, modify ACL/IL, switch Policy, or import Root.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TargetTurboRoot,
    [Parameter(Mandatory=$true)][string]$SnapshotId,
    [ValidateSet('Assess','VerifyRevoked')][string]$Action='Assess',
    [string]$PreviousTurboRoot='',
    [string]$PreviousPackageSid='',
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
$prepare=Join-Path $PSScriptRoot 'turbo_existing_root_prepare.ps1'
if (-not (Test-Path -LiteralPath $prepare -PathType Leaf)) {
    throw 'TURBO_B_P3C_PREPARE_VERIFIER_MISSING'
}
& $prepare -Action Verify -ProjectRoot $project -Workspace $workspace -TurboRoot $target -SnapshotId $SnapshotId -MaxEntries $MaxEntries
if (-not $?) { throw 'TURBO_B_P3C_TARGET_VERIFY_FAILED' }
if ($Action -eq 'VerifyRevoked') {
    if (-not $PreviousTurboRoot -or -not $PreviousPackageSid) {
        throw 'TURBO_B_P3C_PREVIOUS_BINDING_REQUIRED'
    }
    if ($PreviousPackageSid -notmatch '^S-1-15-2-(?:[0-9]+-){2,}[0-9]+$') {
        throw 'TURBO_B_P3C_PREVIOUS_PACKAGE_SID_INVALID'
    }
    $old=Full $PreviousTurboRoot
    if (-not (Under $old $workspace) -or (Same $old $target) -or
        (Under $old $target) -or (Under $target $old)) {
        throw 'TURBO_B_P3C_PREVIOUS_SCOPE_INVALID'
    }
    if (Test-Path -LiteralPath $journal) {
        throw 'TURBO_B_P3C_REVOCATION_JOURNAL_STILL_PRESENT'
    }
    $count=Check-NoPackageGrants $old $PreviousPackageSid
    Write-Host ('TURBO_B_P3C_VERIFIED_OLD_PACKAGE_ACE_REMOVED entries='+$count)
    Write-Host 'TURBO_B_P3C_REVOCATION_VERIFIED_JOURNAL_ABSENT'
    Write-Host 'P3C_EXISTING_ROOT_IMPORT=NOT_ENABLED'
    Write-Host 'P3C_FULL_ACCESS=NOT_ENABLED'
    return
}
if ($PreviousTurboRoot -or $PreviousPackageSid) {
    throw 'TURBO_B_P3C_PREVIOUS_BINDING_ONLY_FOR_VERIFY'
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
Write-Host 'TURBO_B_P3C_ASSESS_PASS'
Write-Host 'P3C_OLD_BROKER=ACTIVE_PROVISIONED'
Write-Host ('P3C_OLD_ROOT='+$old)
Write-Host ('P3C_PACKAGE_SID='+$sid)
Write-Host ('P3C_BROKER_JOURNAL_SHA256='+$journalSha)
Write-Host 'P3C_REVOKE_REQUIRED=YES'
Write-Host 'P3C_REVOCATION_ACTION=NOT_AUTOMATIC'
Write-Host 'P3C_EXISTING_ROOT_IMPORT=NOT_ENABLED'
Write-Host 'P3C_FULL_ACCESS=NOT_ENABLED'
