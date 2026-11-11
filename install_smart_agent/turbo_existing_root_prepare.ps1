# Scheme B P3: production READ-ONLY candidate preparation and independent
# verification. Never mutate the existing Turbo Root, Policy or AppContainer.
# This is the stopping point for a real-system 68-entry live check.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TurboRoot,
    [ValidateSet('Prepare','Verify')][string]$Action='Prepare',
    [string]$SnapshotId='',
    [ValidateRange(1,100000)][int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { throw 'TURBO_B_P3_EMPTY_PATH' }
    return [IO.Path]::GetFullPath($p).TrimEnd('\')
}
function Same([string]$a,[string]$b) {
    return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Under([string]$a,[string]$b) {
    return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)
}
function Read-Json([string]$p) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw ('TURBO_B_P3_REQUIRED_STATE_MISSING:'+$p)
    }
    return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
function Assert-ProtectedPath([string]$path,[string]$label) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw ('TURBO_B_P3_PROTECTED_PATH_MISSING:'+$label)
    }
    if ((Get-Item -LiteralPath $path -Force -ErrorAction Stop).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw ('TURBO_B_P3_PROTECTED_PATH_REPARSE:'+$label)
    }
}
function Check-AdminOnlyAcl([string]$path) {
    $acl=Get-Acl -LiteralPath $path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) {
        throw 'TURBO_B_P3_SNAPSHOT_ACL_INHERITANCE_UNTRUSTED'
    }
    foreach($rule in @($acl.Access)) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            try {
                $sid=$rule.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]).Value
            } catch { throw 'TURBO_B_P3_UNRESOLVABLE_SNAPSHOT_ACL' }
            if ($sid -notin @('S-1-5-18','S-1-5-32-544')) {
                throw 'TURBO_B_P3_SNAPSHOT_ACL_GRANTS_NON_ADMIN'
            }
        }
    }
}
function CurrentBrokerBlocker([string]$journalPath,[string]$target) {
    if (-not (Test-Path -LiteralPath $journalPath)) {
        return 'NONE'
    }
    Assert-ProtectedPath $journalPath 'active-broker-journal'
    try {
        $a=Read-Json $journalPath
        if ($a.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1') {
            return 'UNTRUSTED_ACTIVATION_JOURNAL'
        }
        if ($a.status -eq 'ACTIVE_PROVISIONED') {
            if (Same $a.turbo_root $target) {return 'EXISTING_ENROLLMENT_REQUIRES_REVIEW'}
            return 'OTHER_TURBO_ROOT_ACTIVE_REQUIRES_VERIFIED_REVOCATION'
        }
        return ('PENDING_BROKER_LIFECYCLE_'+[string]$a.status)
    } catch {
        return 'ACTIVATION_JOURNAL_UNREADABLE'
    }
}
$principal=[Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'TURBO_B_P3_ADMINISTRATOR_REQUIRED'
}
$project=Full $ProjectRoot
$workspace=Full $Workspace
$root=Full $TurboRoot
if (-not (Under $root $workspace) -or
    (Same $project $workspace) -or (Under $workspace $project) -or
    (Under $project $workspace) -or (Under $root $project) -or
    (Under $project $root)) {
    throw 'TURBO_B_P3_SCOPE_INVALID'
}
$secure=Join-Path $project 'localdata\secure\windows_security'
$mode=Read-Json (Join-Path $secure 'acl_mode.json')
$policy=Read-Json (Join-Path $secure 'workspace_access_policy.json')
if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
    -not (Same $mode.install_root $project)) {
    throw 'TURBO_B_P3_ACL_ON_REQUIRED'
}
if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
    [int]$policy.revision -lt 1 -or
    @($policy.writable_workspaces | Where-Object { Same ([string]$_) $workspace }).Count -ne 1) {
    throw 'TURBO_B_P3_WORKSPACE_POLICY_MISMATCH'
}
foreach($value in @($policy.read_only_roots)+@($policy.denied_write_roots)) {
    if ([string]::IsNullOrWhiteSpace([string]$value)) { continue }
    $protected=Full ([string]$value)
    if ((Same $protected $root) -or (Under $protected $root) -or (Under $root $protected)) {
        throw 'TURBO_B_P3_PROTECTED_ROOT_OVERLAP'
    }
}
$preflight=Join-Path $PSScriptRoot 'turbo_existing_root_preflight.ps1'
$snapshotWriter=Join-Path $PSScriptRoot 'turbo_existing_root_snapshot.ps1'
if (-not (Test-Path -LiteralPath $preflight -PathType Leaf) -or
    -not (Test-Path -LiteralPath $snapshotWriter -PathType Leaf)) {
    throw 'TURBO_B_P3_MISSING_TRUSTED_SCRIPTS'
}
$backupDir=Join-Path $secure 'turbo_existing_root_snapshots'
function Verify-Snapshot([string]$id) {
    if ($id -notmatch '^[0-9a-f]{32}$') { throw 'TURBO_B_P3_SNAPSHOT_ID_INVALID' }
    Assert-ProtectedPath $backupDir 'snapshot-directory'
    Assert-ProtectedPath $secure 'security-root'
    Check-AdminOnlyAcl $backupDir
    $snapshotFile=Join-Path $backupDir ('snapshot-'+$id+'.json')
    Assert-ProtectedPath $snapshotFile 'snapshot-file'
    Check-AdminOnlyAcl $snapshotFile
    $snapshot=Read-Json $snapshotFile
    if ($snapshot.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1' -or
        $snapshot.snapshot_id -cne $id -or
        $snapshot.status -ne 'PREPARED_READ_ONLY' -or
        $snapshot.authorization_to_mutate -ne $false -or
        $snapshot.candidate_only -ne $true -or
        -not (Same $snapshot.project_root $project) -or
        -not (Same $snapshot.workspace_root $workspace) -or
        -not (Same $snapshot.turbo_root $root) -or
        [int]$snapshot.policy_revision -ne [int]$policy.revision -or
        [int]$snapshot.entries_count -gt $MaxEntries -or
        [int]$snapshot.entries_count -lt 1 -or
        @($snapshot.entries).Count -ne [int]$snapshot.entries_count) {
        throw 'TURBO_B_P3_SNAPSHOT_STALE_OR_UNTRUSTED'
    }
    $inventory=@(& $preflight -ProjectRoot $project -Workspace $workspace -TurboRoot $root -MaxEntries $MaxEntries -IncludeSensitiveInventory |
        Where-Object { $_ -is [System.Collections.Specialized.OrderedDictionary] })
    if ($inventory.Count -ne 1 -or
        @($inventory[0].entries).Count -ne [int]$snapshot.entries_count) {
        throw 'TURBO_B_P3_TREE_COUNT_MISMATCH'
    }
    $current=@{}
    foreach($node in @($inventory[0].entries)) {
        $path=[string]$node.path
        if ($current.ContainsKey($path)) { throw 'TURBO_B_P3_DUPLICATE_PATH' }
        $current[$path]=$node
    }
    foreach($entry in @($snapshot.entries)) {
        $path=[string]$entry.path
        if (-not ((Same $path $root) -or (Under $path $root)) -or
            -not $current.ContainsKey($path)) {
            throw 'TURBO_B_P3_TREE_PATH_MISMATCH'
        }
        $node=$current[$path]
        if ([string]$node.identity -cne [string]$entry.identity -or
            [string]$node.sddl -cne [string]$entry.sddl -or
            [bool]$node.directory -ne [bool]$entry.directory -or
            [string]$node.relative -cne [string]$entry.relative) {
            throw 'TURBO_B_P3_TREE_ID_OR_SDDL_MISMATCH'
        }
    }
    $digest=(Get-FileHash -LiteralPath $snapshotFile -Algorithm SHA256 -ErrorAction Stop).Hash
    $brokerBlocker=CurrentBrokerBlocker (Join-Path $secure 'turbo_os_activation.json') $root
    Write-Host ('TURBO_B_P3_VERIFY_PASS entries='+[int]$snapshot.entries_count)
    Write-Host ('P3_SNAPSHOT_ID='+$id)
    Write-Host ('P3_SNAPSHOT_SHA256='+$digest)
    Write-Host ('P3_POLICY_REVISION='+[int]$policy.revision)
    Write-Host ('P3_BROKER_BLOCKER='+$brokerBlocker)
    Write-Host 'P3_IMPORT_MUTATION=DISABLED'
    Write-Host 'P3_FULL_ACCESS=NOT_ENABLED'
}
if ($Action -eq 'Verify') {
    Verify-Snapshot $SnapshotId
    return
}
if ($SnapshotId) { throw 'TURBO_B_P3_SNAPSHOT_ID_ONLY_FOR_VERIFY' }
# The policy may currently request a DIFFERENT fresh Turbo root. This
# candidate preparation does not change/replace that grant or policy.
$receipt=@(& $snapshotWriter -ProjectRoot $project -Workspace $workspace -TurboRoot $root -MaxEntries $MaxEntries -CandidateOnly -EmitReceipt |
    Where-Object { $_.schema -eq 'SMARTAGENT_TURBO_B_SNAPSHOT_RECEIPT_V1' })
if ($receipt.Count -ne 1 -or $receipt[0].authorization_to_mutate -ne $false -or
    $receipt[0].candidate_only -ne $true) {
    throw 'TURBO_B_P3_PREPARE_RECEIPT_INVALID'
}
Verify-Snapshot ([string]$receipt[0].snapshot_id)
Write-Host 'TURBO_B_P3_PREPARE_PASS'
Write-Host 'P3_LIVE_CHECKPOINT=EXISTING_ROOT_SNAPSHOT_VERIFIED_NO_MUTATION'
