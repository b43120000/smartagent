# Scheme B phase 1: durable, protected, READ-ONLY snapshot of an existing
# NTFS Turbo Root. No AppContainer ACE or integrity label is changed here.
# Future importer MUST re-check IDs/SDDL and implement power-loss-safe rollback.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TurboRoot,
    [int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p) { return [IO.Path]::GetFullPath($p).TrimEnd('\') }
function Equal([string]$a,[string]$b) {
    return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Read-Json([string]$p) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw ('TURBO_B_SNAPSHOT_MISSING_STATE:'+ $p)
    }
    return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
$principal=[Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'TURBO_B_SNAPSHOT_REQUIRES_ELEVATED_ADMIN'
}
$project=Full $ProjectRoot
$workspace=Full $Workspace
$root=Full $TurboRoot
$secure=Join-Path $project 'localdata\secure\windows_security'
$mode=Read-Json (Join-Path $secure 'acl_mode.json')
$policy=Read-Json (Join-Path $secure 'workspace_access_policy.json')
if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
    $mode.mode -ne 'on' -or -not (Equal $mode.install_root $project)) {
    throw 'TURBO_B_SNAPSHOT_ACL_ON_INSTALL_MISMATCH'
}
if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
    $policy.turbo_requested -ne $true -or
    [int]$policy.revision -lt 1) {
    throw 'TURBO_B_SNAPSHOT_POLICY_NOT_REQUESTED'
}
$policyRoots=@($policy.turbo_roots)
$writable=@($policy.writable_workspaces)
if ($policyRoots.Count -ne 1 -or -not (Equal ([string]$policyRoots[0]) $root) -or
    @($writable | Where-Object { Equal ([string]$_) $workspace }).Count -ne 1) {
    throw 'TURBO_B_SNAPSHOT_SCOPE_POLICY_MISMATCH'
}
$preflight=Join-Path $PSScriptRoot 'turbo_existing_root_preflight.ps1'
if (-not (Test-Path -LiteralPath $preflight -PathType Leaf)) {
    throw 'TURBO_B_SNAPSHOT_PREFLIGHT_UNAVAILABLE'
}
$results=@(& $preflight -ProjectRoot $project -Workspace $workspace -TurboRoot $root -MaxEntries $MaxEntries -IncludeSensitiveInventory | Where-Object {
        $_ -is [System.Collections.Specialized.OrderedDictionary]
    })
if ($results.Count -ne 1) { throw 'TURBO_B_SNAPSHOT_PREFLIGHT_RESULT_INVALID' }
$inventory=$results[0]
if ($inventory.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_PREFLIGHT_V1' -or
    $inventory.import_allowed_for_mutation -ne $false -or
    @($inventory.entries).Count -ne [int]$inventory.count) {
    throw 'TURBO_B_SNAPSHOT_PREFLIGHT_RESULT_UNTRUSTED'
}
$backupDir=Join-Path $secure 'turbo_existing_root_snapshots'
# Never follow a reparse point in an administrator-owned receipt path.
foreach ($path in @($project, (Join-Path $project 'localdata'),
        (Join-Path $project 'localdata\secure'), $secure, $backupDir)) {
    if (Test-Path -LiteralPath $path) {
        $item=Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw ('TURBO_B_SNAPSHOT_PROTECTED_PATH_REPARSE:'+ $path)
        }
    }
}
if (-not (Test-Path -LiteralPath $backupDir -PathType Container)) {
    New-Item -ItemType Directory -Path $backupDir -ErrorAction Stop | Out-Null
}
& icacls.exe $backupDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'TURBO_B_SNAPSHOT_PROTECTION_FAILED' }
# /grant:r does not erase other pre-existing explicit Allow ACEs. Verify
# the ENTIRE resulting DACL rather than assuming the directory is private.
$acl=Get-Acl -LiteralPath $backupDir -ErrorAction Stop
foreach ($rule in @($acl.Access)) {
    if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
        try {
            $sid=$rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]).Value
        } catch {
            throw 'TURBO_B_SNAPSHOT_UNRESOLVABLE_ACL_PRINCIPAL'
        }
        if ($sid -notin @('S-1-5-18','S-1-5-32-544')) {
            throw ('TURBO_B_SNAPSHOT_UNTRUSTED_PREEXISTING_ALLOW_ACE:'+ $sid)
        }
    }
}
# Never overwrite: the first mutation plan must cite one immutable snapshot ID.
$id=[Guid]::NewGuid().ToString('N')
$path=Join-Path $backupDir ('snapshot-'+$id+'.json')
$snapshot=[ordered]@{
    schema='SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1'
    snapshot_id=$id
    status='PREPARED_READ_ONLY'
    authorization_to_mutate=$false
    project_root=$project
    workspace_root=$workspace
    turbo_root=$root
    policy_revision=[int]$policy.revision
    created_at=[DateTimeOffset]::UtcNow.ToString('o')
    entries_count=[int]$inventory.count
    entries=@($inventory.entries)
}
$data=[Text.Encoding]::UTF8.GetBytes(($snapshot | ConvertTo-Json -Depth 12))
$stream=[IO.FileStream]::new($path,[IO.FileMode]::CreateNew,
    [IO.FileAccess]::Write,[IO.FileShare]::None)
try { $stream.Write($data,0,$data.Length); $stream.Flush($true) }
finally { $stream.Dispose() }
# Validate the write and security envelope before reporting the location.
$readback=Read-Json $path
if ($readback.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1' -or
    $readback.snapshot_id -ne $id -or
    [int]$readback.entries_count -ne [int]$inventory.count) {
    throw 'TURBO_B_SNAPSHOT_READBACK_MISMATCH'
}
Write-Host ('TURBO_B_PROTECTED_EXISTING_ROOT_SNAPSHOT_PASS entries='+$inventory.count)
Write-Host ('TURBO_B_SNAPSHOT_PATH: '+$path)
Write-Host 'TURBO_B_IMPORT_NOT_ENABLED; original filesystem content and security untouched.'
