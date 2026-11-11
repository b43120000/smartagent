# Scheme B P2: CI-only real NTFS ACL mutation / rollback rehearsal.
# This is NOT an existing-root production importer. Any non-CI target
# is rejected BEFORE calling Set-Acl. This code must never be pointed at
# users' E:\ projects; it verifies transaction mechanics on disposable data.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TurboRoot,
    [Parameter(Mandatory=$true)][string]$SnapshotPath,
    [Parameter(Mandatory=$true)][string]$AppContainerSid,
    [ValidateRange(0,100000)][int]$InjectFailureAfter = 0
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Full([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { throw 'TURBO_B_TRANSACTION_EMPTY_PATH' }
    return [IO.Path]::GetFullPath($p).TrimEnd('\')
}
function Same([string]$a,[string]$b) {
    return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Within([string]$a,[string]$b) {
    return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)
}
$project=Full $ProjectRoot
$workspace=Full $Workspace
$root=Full $TurboRoot
$runner=Full ([string]$env:RUNNER_TEMP)
$fixture=Full ([IO.Path]::GetDirectoryName($project))
if ($env:GITHUB_ACTIONS -ne 'true' -or
    -not (Within $fixture $runner) -or
    -not ([IO.Path]::GetFileName($fixture) -match '^TurboBTransaction-[0-9a-f]{32}$') -or
    -not (Test-Path -LiteralPath (Join-Path $fixture '.turbo-b-ci-disposable') -PathType Leaf) -or
    ([IO.File]::ReadAllText((Join-Path $fixture '.turbo-b-ci-disposable')) -cne 'DISPOSABLE_FIXTURE') -or
    -not (Within $root $workspace) -or
    -not (Within $workspace $fixture) -or
    -not (Within $project $fixture)) {
    throw 'TURBO_B_TRANSACTION_PRODUCTION_MUTATION_DISABLED'
}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=[Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'TURBO_B_TRANSACTION_ELEVATION_REQUIRED'
}
if ($AppContainerSid -notmatch '^S-1-15-2-(?:[0-9]+-){2,}[0-9]+$') {
    throw 'TURBO_B_TRANSACTION_PACKAGE_SID_INVALID'
}
$security=Join-Path $project 'localdata\secure\windows_security'
$expectedSnapshotDir=Join-Path $security 'turbo_existing_root_snapshots'
$snapshot=Full $SnapshotPath
if (-not (Within $snapshot $expectedSnapshotDir) -or
    [IO.Path]::GetFileName($snapshot) -notmatch '^snapshot-[0-9a-f]{32}\.json$' -or
    -not (Test-Path -LiteralPath $snapshot -PathType Leaf)) {
    throw 'TURBO_B_TRANSACTION_SNAPSHOT_PATH_UNTRUSTED'
}
$doc=Get-Content -LiteralPath $snapshot -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if ($doc.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1' -or
    $doc.status -ne 'PREPARED_READ_ONLY' -or
    $doc.authorization_to_mutate -ne $false -or
    -not (Same $doc.project_root $project) -or
    -not (Same $doc.workspace_root $workspace) -or
    -not (Same $doc.turbo_root $root) -or
    @($doc.entries).Count -ne [int]$doc.entries_count) {
    throw 'TURBO_B_TRANSACTION_SNAPSHOT_INVALID'
}
$policy=Get-Content -LiteralPath (Join-Path $security 'workspace_access_policy.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$mode=Get-Content -LiteralPath (Join-Path $security 'acl_mode.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($mode.mode -ne 'on' -or -not (Same $mode.install_root $project) -or
    $policy.turbo_requested -ne $true -or [int]$policy.revision -ne [int]$doc.policy_revision -or
    @($policy.turbo_roots).Count -ne 1 -or -not (Same $policy.turbo_roots[0] $root)) {
    throw 'TURBO_B_TRANSACTION_POLICY_STALE'
}
$preflight=Join-Path $PSScriptRoot 'turbo_existing_root_preflight.ps1'
$items=@(& $preflight -ProjectRoot $project -Workspace $workspace -TurboRoot $root -IncludeSensitiveInventory | Where-Object {
    $_ -is [System.Collections.Specialized.OrderedDictionary]
})
if ($items.Count -ne 1 -or @($items[0].entries).Count -ne @($doc.entries).Count) {
    throw 'TURBO_B_TRANSACTION_TREE_CHANGED'
}
$live=@{}
foreach ($item in @($items[0].entries)) {
    $key=[string]$item.path
    if ($live.ContainsKey($key)) { throw 'TURBO_B_TRANSACTION_DUPLICATE_PATH' }
    $live[$key]=$item
}
$original=@{}
foreach ($entry in @($doc.entries)) {
    $path=[string]$entry.path
    if (-not $live.ContainsKey($path) -or
        [string]$live[$path].identity -cne [string]$entry.identity -or
        [string]$live[$path].sddl -cne [string]$entry.sddl -or
        [bool]$live[$path].directory -ne [bool]$entry.directory) {
        throw ('TURBO_B_TRANSACTION_SNAPSHOT_CHANGED:'+$path)
    }
    $original[$path]=$entry
}
$sid=[Security.Principal.SecurityIdentifier]::new($AppContainerSid)
$modified=New-Object System.Collections.Generic.List[object]
$failure=$null
$mutations=0
# This rehearsal is intentionally short-lived and ALWAYS restores the original
# security descriptor before returning, including an injected failure.
try {
    foreach ($entry in @($doc.entries | Sort-Object path)) {
        $p=[string]$entry.path
        if (-not ((Same $p $root) -or (Within $p $root))) {
            throw 'TURBO_B_TRANSACTION_OUTSIDE_ENTRY'
        }
        if (-not (Test-Path -LiteralPath $p)) {
            throw 'TURBO_B_TRANSACTION_ENTRY_DISAPPEARED'
        }
        if ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'TURBO_B_TRANSACTION_REPARSE_CHANGED'
        }
        $current=(Get-Acl -LiteralPath $p -Audit -ErrorAction Stop).Sddl
        if ($current -cne [string]$entry.sddl) {
            throw 'TURBO_B_TRANSACTION_CONCURRENT_ACL_CHANGE'
        }
        $rule=[Security.AccessControl.FileSystemAccessRule]::new(
            $sid,[Security.AccessControl.FileSystemRights]::Modify,
            [Security.AccessControl.InheritanceFlags]::None,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $acl=Get-Acl -LiteralPath $p -ErrorAction Stop
        [void]$acl.AddAccessRule($rule)
        # Add BEFORE Set-Acl: a failed/interrupted application must be in the
        # recovery set. No mutable manifest is trusted for production yet.
        [void]$modified.Add($entry)
        Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
        $mutations++
        if ($InjectFailureAfter -gt 0 -and $mutations -ge $InjectFailureAfter) {
            throw 'TURBO_B_TRANSACTION_INJECTED_FAILURE'
        }
    }
    Write-Host ('TURBO_B_CI_ACL_MUTATION_STAGE_PASS entries='+$mutations)
} catch {
    $failure=$_.Exception.Message
} finally {
    $restoreErrors=New-Object System.Collections.Generic.List[string]
    # Restore REVERSE order, while refusing to follow links/replaced file IDs.
    for($i=$modified.Count-1;$i -ge 0;$i--) {
        $entry=$modified[$i]
        $p=[string]$entry.path
        try {
            $item=Get-Item -LiteralPath $p -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'TURBO_B_TRANSACTION_RESTORE_REPARSE'
            }
            # The identity was audited immediately before applying the ACE.
            $now=[SmartAgentTurboBFileId]::Check($p,[bool]$entry.directory)
            if ($now -cne [string]$entry.identity) {
                throw 'TURBO_B_TRANSACTION_RESTORE_ID_MISMATCH'
            }
            $acl=Get-Acl -LiteralPath $p -Audit -ErrorAction Stop
            $acl.SetSecurityDescriptorSddlForm([string]$entry.sddl)
            Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
            $after=(Get-Acl -LiteralPath $p -Audit -ErrorAction Stop).Sddl
            if ($after -cne [string]$entry.sddl) {
                # ONLY CI disposable fixture is allowed to reach this point.
                Write-Host ('TURBO_B_CI_SDDL_BEFORE: '+[string]$entry.sddl)
                Write-Host ('TURBO_B_CI_SDDL_AFTER: '+$after)
                throw 'TURBO_B_TRANSACTION_RESTORE_SDDL_MISMATCH'
            }
        } catch {
            [void]$restoreErrors.Add($p+':'+$_.Exception.Message)
        }
    }
    if ($restoreErrors.Count -gt 0) {
        throw ('TURBO_B_TRANSACTION_RESTORE_FAILED:' + ($restoreErrors -join ';'))
    }
}
if ($failure) {
    if ($failure -eq 'TURBO_B_TRANSACTION_INJECTED_FAILURE') {
        Write-Host ('TURBO_B_CI_INJECTED_FAILURE_ROLLBACK_PASS entries='+$mutations)
    } else {
        throw ('TURBO_B_TRANSACTION_STAGE_FAILED_ROLLED_BACK:'+$failure)
    }
} else {
    Write-Host ('TURBO_B_CI_DACL_ROUNDTRIP_RESTORED_PASS entries='+$mutations)
}
