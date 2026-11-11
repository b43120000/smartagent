# Read-only ACL OFF audit for SmartAgent 6b5629+.
# Never mutates ACLs, accounts, tasks or the SmartAgent installation.
# SCOPED_READ_ONLY_PASS is NOT a full OS postcondition / write-probe pass.
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ExpectedExecutorSid = '',
    [string[]]$ManagedPaths = @(),
    [switch]$RecurseManagedPaths,
    [string]$ReportPath = '',
    [switch]$TestFixture,
    [string]$FixtureMachineRoot = ''
)

$ErrorActionPreference = 'Stop'
$checks = [System.Collections.Generic.List[object]]::new()
$root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$sec = Join-Path $root 'localdata\secure\windows_security'
$machine = Join-Path $env:ProgramData 'SmartAgent'
if ($TestFixture) {
    if (-not $FixtureMachineRoot) { throw 'TEST_FIXTURE_REQUIRES_ISOLATED_MACHINE_ROOT' }
    $machine = [IO.Path]::GetFullPath($FixtureMachineRoot)
} elseif ($FixtureMachineRoot) {
    throw 'FIXTURE_MACHINE_ROOT_MUST_NOT_BE_USED_FOR_REAL_AUDIT'
}

function Add-Check([string]$Name, [string]$Status, [string]$Detail) {
    $script:checks.Add([pscustomobject]@{
        name=$Name; status=$Status; detail=$Detail
    })
}
function Read-Json([string]$Path) {
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
        ConvertFrom-Json -ErrorAction Stop
}
function Compare-InstallRoot([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    try {
        return [IO.Path]::GetFullPath($Value).TrimEnd('\').Equals(
            $root, [StringComparison]::OrdinalIgnoreCase
        )
    } catch { return $false }
}
function Check-LocalAbsent([string]$Name, [string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Add-Check $Name 'FAIL' ('Residual file: ' + $Path)
    } else {
        Add-Check $Name 'PASS' ('Absent: ' + $Path)
    }
}
function Check-MachineRecord([string]$Name, [string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Add-Check $Name 'PASS' 'Machine record absent'
        return
    }
    try {
        $record = Read-Json $Path
        $owner = [string]$record.install_root
        if (-not $owner -and $record.PSObject.Properties['root']) {
            $owner = [string]$record.root
        }
        if (Compare-InstallRoot $owner) {
            Add-Check $Name 'FAIL' ('Record still bound to this installation: ' + $Path)
        } elseif ($owner) {
            Add-Check $Name 'INFO' ('Other installation owns record: ' + $owner)
        } else {
            Add-Check $Name 'UNKNOWN' ('Machine record has no verifiable owner: ' + $Path)
        }
    } catch {
        Add-Check $Name 'UNKNOWN' ('Unreadable machine record: ' + $_.Exception.Message)
    }
}
function Check-PathAce([string]$Path, [string]$Sid) {
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Add-Check 'executor_ace' 'UNKNOWN' ('Reparse path not traversed: ' + $Path)
            return
        }
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        foreach ($rule in @($acl.Access)) {
            $ruleSid = ''
            try {
                $ruleSid = $rule.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]
                ).Value
            } catch {
                $ruleSid = [string]$rule.IdentityReference.Value
            }
            if ($ruleSid -eq $Sid) {
                Add-Check 'executor_ace' 'FAIL' ('Executor ACE remains: ' + $Path)
                return
            }
        }
        Add-Check 'executor_ace' 'PASS' ('No target Executor ACE: ' + $Path)
    } catch {
        Add-Check 'executor_ace' 'UNKNOWN' ('Unable to inspect ACL: ' + $Path)
    }
}

# The persisted transition must be complete and bound to this installation.
$modePath = Join-Path $sec 'acl_mode.json'
$mode = $null
if (-not (Test-Path -LiteralPath $modePath -PathType Leaf)) {
    Add-Check 'acl_mode' 'UNKNOWN' 'acl_mode.json missing; cannot attest explicit OFF'
} else {
    try {
        $mode = Read-Json $modePath
        if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
            -not (Compare-InstallRoot ([string]$mode.install_root))) {
            Add-Check 'acl_mode' 'FAIL' 'ACL mode schema/install_root mismatch'
        } elseif ($mode.mode -ne 'off') {
            Add-Check 'acl_mode' 'FAIL' ('Not in committed OFF state: ' + $mode.mode)
        } else {
            Add-Check 'acl_mode' 'PASS' 'Committed OFF; install_root matches'
        }
    } catch {
        Add-Check 'acl_mode' 'UNKNOWN' ('ACL mode not readable: ' + $_.Exception.Message)
    }
}

# Persistent workspace policy is allowed; Turbo must be fully suppressed.
$policyPath = Join-Path $sec 'workspace_access_policy.json'
if (Test-Path -LiteralPath $policyPath -PathType Leaf) {
    try {
        $policy = Read-Json $policyPath
        if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1') {
            Add-Check 'turbo_policy' 'FAIL' 'Unexpected policy schema'
        } elseif ($policy.turbo_requested -eq $true -or
                @($policy.turbo_roots).Count -gt 0) {
            Add-Check 'turbo_policy' 'FAIL' 'Turbo request or roots survived ACL OFF'
        } else {
            Add-Check 'turbo_policy' 'PASS' 'Turbo disabled and roots empty'
        }
    } catch {
        Add-Check 'turbo_policy' 'UNKNOWN' ('Unreadable policy: ' + $_.Exception.Message)
    }
} else {
    Add-Check 'turbo_policy' 'INFO' 'No workspace policy; not evidence of active ACL'
}

Check-LocalAbsent 'security_profile' (Join-Path $sec 'security_profile.json')
Check-LocalAbsent 'executor_install_pending' (Join-Path $sec 'executor_install_pending.json')
Check-LocalAbsent 'turbo_os_activation' (Join-Path $sec 'turbo_os_activation.json')
Check-MachineRecord 'machine_authorization' (Join-Path $machine 'machine_authorization.json')
Check-MachineRecord 'pending_acl_transaction' (Join-Path $machine 'pending_acl_transaction.json')
Check-MachineRecord 'acl_revocation_pending' (Join-Path $machine 'acl_revocation_pending.json')

# Machine-wide task may belong to another SmartAgent installation.
if ($TestFixture) {
    Add-Check 'scheduled_executor' 'INFO' 'Fixture: real machine-wide tasks intentionally not queried'
} else {
    try {
        $task = Get-ScheduledTask -TaskName 'SmartAgent Restricted Executor' -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Add-Check 'scheduled_executor' 'PASS' 'Machine-wide executor task absent'
        } else {
            $actionText = (@($task.Actions | ForEach-Object {
                [string]$_.Execute + ' ' + [string]$_.Arguments
            }) -join ' ')
            if ($actionText.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                Add-Check 'scheduled_executor' 'FAIL' 'Task action still references this installation'
            } else {
                Add-Check 'scheduled_executor' 'UNKNOWN' 'Executor task exists; ownership requires explicit verification'
            }
        }
    } catch {
        Add-Check 'scheduled_executor' 'UNKNOWN' ('Task query failed: ' + $_.Exception.Message)
    }
}

# A retained local account alone does not prove it has effective permissions.
$sid = [string]$ExpectedExecutorSid
if (-not $sid -and $mode) {
    $sid = [string]$mode.retired_executor_sid
}
if ($sid -notmatch '^S-1-[0-9-]+$') {
    Add-Check 'executor_identity' 'UNKNOWN' 'Executor SID unavailable; cannot audit ACE remnants'
} else {
    Add-Check 'executor_identity' 'PASS' ('Executor SID supplied: ' + $sid)
    $locations = @($root, $sec) + @($ManagedPaths)
    $visited = @{}
    foreach ($location in $locations) {
        if (-not $location) { continue }
        try {
            $p = [IO.Path]::GetFullPath([string]$location).TrimEnd('\')
            if ($visited.ContainsKey($p.ToUpperInvariant())) { continue }
            $visited[$p.ToUpperInvariant()] = $true
            if (-not (Test-Path -LiteralPath $p)) {
                Add-Check 'managed_path' 'UNKNOWN' ('Not found: ' + $p)
                continue
            }
            Check-PathAce $p $sid
            if ($RecurseManagedPaths -and $ManagedPaths -contains $location) {
                $entries = @(Get-ChildItem -LiteralPath $p -Recurse -Force -ErrorAction Stop)
                if ($entries.Count -gt 10000) {
                    Add-Check 'managed_path' 'UNKNOWN' 'Over 10000 descendants: scan requires explicit smaller roots'
                } else {
                    foreach ($entry in $entries) {
                        Check-PathAce $entry.FullName $sid
                    }
                }
            }
        } catch {
            Add-Check 'managed_path' 'UNKNOWN' ('Cannot enumerate path: ' + $_.Exception.Message)
        }
    }
}
if (-not $ManagedPaths -or -not $RecurseManagedPaths) {
    Add-Check 'coverage' 'UNKNOWN' 'No complete historical managed-path inventory with recursive ACE scan'
}
Add-Check 'write_postcondition' 'UNKNOWN' 'Read-only audit cannot prove controller create/rename/delete postconditions'

$fail = @($checks | Where-Object { $_.status -eq 'FAIL' }).Count
$unknown = @($checks | Where-Object { $_.status -eq 'UNKNOWN' }).Count
$status = if ($fail -gt 0) { 'FAIL' } else { 'INCOMPLETE' }
# By design this read-only checker NEVER declares full ACL OFF success.
$report = [ordered]@{
    schema = 'SMARTAGENT_ACL_OFF_AUDIT_V1'
    project_root = $root
    mode = if ($mode) { [string]$mode.mode } else { 'unknown' }
    status = $status
    failed = $fail
    unknown = $unknown
    full_os_verification = $false
    fixture_only = [bool]$TestFixture
    checks = @($checks)
}
$json = $report | ConvertTo-Json -Depth 8
if ($ReportPath) {
    $reportTarget = [IO.Path]::GetFullPath($ReportPath)
    [IO.File]::WriteAllText($reportTarget, $json, (New-Object Text.UTF8Encoding($false)))
}
Write-Output $json
if ($fail -gt 0) { exit 1 }
exit 2
