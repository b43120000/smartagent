# SmartAgent ACL ON postmortem: read only, no authorization changes.
# Original script: install_smart_agent/acl_status.ps1 (6b5629 baseline).
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ReportPath = '',
    [string]$MachineRoot = '',
    [switch]$Fixture
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$security = Join-Path $root 'localdata\secure\windows_security'
$machine = Join-Path $env:ProgramData 'SmartAgent'
if ($Fixture) {
    if (-not $MachineRoot) { throw 'FIXTURE_MACHINE_ROOT_REQUIRED' }
    $machine = [IO.Path]::GetFullPath($MachineRoot)
} elseif ($MachineRoot) {
    throw 'MACHINE_ROOT_OVERRIDE_REQUIRES_FIXTURE'
}
$checks = [System.Collections.Generic.List[object]]::new()
$stages = @(
    'PRECHECK','SECURITY_PROFILE','EXECUTOR_TASK','EXECUTOR_SELF_TEST',
    'MACHINE_AUTHORIZATION','EXECUTOR_FINAL_READY','ACL_MODE_COMMIT'
)
function Record([string]$Stage,[string]$Key,[string]$Status,[string]$Detail) {
    [void]$script:checks.Add([pscustomobject]@{
        stage=$Stage; key=$Key; status=$Status; detail=$Detail
    })
}
function Read-Json([string]$Path,[string]$Stage,[string]$Key) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
    } catch {
        Record $Stage $Key 'FAIL' ('Invalid or unreadable JSON: ' + $_.Exception.Message)
        return $null
    }
}
function Same-Path([string]$A,[string]$B) {
    if (-not $A -or -not $B) { return $false }
    try {
        return [IO.Path]::GetFullPath($A).TrimEnd('\').Equals(
            [IO.Path]::GetFullPath($B).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}
function Check-File([string]$Path,[string]$Key) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Record 'PRECHECK' $Key 'PASS' ('Found: ' + $Path)
    } else {
        Record 'PRECHECK' $Key 'FAIL' ('Missing: ' + $Path)
    }
}
function Check-State([string]$Path,[string]$Stage,[string]$Key) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Record $Stage $Key 'INFO' ('A pending state file exists: ' + $Path)
    }
}
$modeFile = Join-Path $security 'acl_mode.json'
$profileFile = Join-Path $security 'security_profile.json'
$policyFile = Join-Path $security 'workspace_access_policy.json'
$authFile = Join-Path $machine 'machine_authorization.json'
$taskName = 'SmartAgent Restricted Executor'

# Snapshot is current state, not a time machine. Files may have been removed
# by cleanup, and a stopped executor's heartbeat naturally becomes stale.
Check-File (Join-Path $root '.venv\Scripts\python.exe') 'executor_python'
Check-File (Join-Path $root 'config\protocol_manifest.json') 'protocol_manifest'
Check-File (Join-Path $root 'install_smart_agent\configure_security.ps1') 'security_provisioner'
Check-File (Join-Path $root 'source\agent_core\windows_security.py') 'security_attestation_module'
if (-not $Fixture -and
    (Test-Path -LiteralPath (Join-Path $root '.venv\Scripts\python.exe') -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $root 'config\protocol_manifest.json') -PathType Leaf)) {
    # Match ACLstatus.ps1's source-hash preflight without changing any files.
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $code = "import sys; sys.path.insert(0,sys.argv[1]); from agent_core.protocol_manifest import validate_protocol_manifest; r=validate_protocol_manifest(sys.argv[2]); h=r['hash_mismatches']; sys.exit('manifest_mismatch:'+','.join(h) if h else 0)"
    try {
        $manifestOutput = & $python -I -B -c $code (Join-Path $root 'source') $root 2>&1
        if ($LASTEXITCODE -eq 0) {
            Record 'PRECHECK' 'manifest_integrity' 'PASS' 'Protocol manifest hashes match local core files'
        } else {
            Record 'PRECHECK' 'manifest_integrity' 'FAIL' ('ACL_ON_PROTOCOL_MANIFEST_UNVERIFIED: ' + ($manifestOutput | Out-String).Trim())
        }
    } catch {
        Record 'PRECHECK' 'manifest_integrity' 'FAIL' ('Protocol manifest validation failed: ' + $_.Exception.Message)
    }
} elseif ($Fixture) {
    Record 'PRECHECK' 'manifest_integrity' 'UNKNOWN' 'Fixture does not execute local Python interpreter'
}
$mode = Read-Json $modeFile 'ACL_MODE_COMMIT' 'mode_json'
if ($null -eq $mode) {
    Record 'ACL_MODE_COMMIT' 'mode' 'FAIL' 'ACL mode record missing or unreadable'
} elseif ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
          -not (Same-Path ([string]$mode.install_root) $root)) {
    Record 'ACL_MODE_COMMIT' 'mode' 'FAIL' 'Mode schema or installation owner mismatch'
} elseif ($mode.mode -eq 'on') {
    Record 'ACL_MODE_COMMIT' 'mode' 'PASS' 'mode=on (other independent checks still required)'
} elseif ($mode.mode -eq 'transitioning_on') {
    Record 'ACL_MODE_COMMIT' 'mode' 'FAIL' 'mode=transitioning_on: ON failed or is still running'
} else {
    Record 'ACL_MODE_COMMIT' 'mode' 'FAIL' ('mode=' + $mode.mode + ', ON is not committed')
}
$policy = Read-Json $policyFile 'PRECHECK' 'policy_json'
if (-not (Test-Path -LiteralPath $policyFile -PathType Leaf)) {
    Record 'PRECHECK' 'workspace_policy' 'INFO' 'Policy absent; ON may use existing profile or provisioning defaults'
} elseif ($null -ne $policy) {
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1') {
        Record 'PRECHECK' 'workspace_policy' 'FAIL' 'Invalid workspace policy schema'
    } elseif (@($policy.writable_workspaces | Where-Object { $_ }).Count -lt 1) {
        Record 'PRECHECK' 'workspace_policy' 'FAIL' 'No writable workspace defined'
    } else {
        Record 'PRECHECK' 'workspace_policy' 'PASS' 'Policy schema and writable roots exist (path conflicts not evaluated)'
    }
}
Check-State (Join-Path $machine 'pending_acl_transaction.json') 'SECURITY_PROFILE' 'pending_acl_transaction'
Check-State (Join-Path $security 'executor_install_pending.json') 'SECURITY_PROFILE' 'executor_install_pending'
Check-State (Join-Path $machine 'acl_revocation_pending.json') 'PRECHECK' 'acl_revocation_pending'
Check-State (Join-Path $security 'turbo_os_activation.json') 'PRECHECK' 'turbo_os_activation'

$profile = Read-Json $profileFile 'SECURITY_PROFILE' 'profile_json'
if (-not (Test-Path -LiteralPath $profileFile -PathType Leaf)) {
    Record 'SECURITY_PROFILE' 'profile' 'FAIL' 'No committed Security Profile; provisioning did not reach publication'
} elseif ($null -ne $profile) {
    if ($profile.schema -ne 'SMARTAGENT_WINDOWS_SECURITY_V2' -or
        -not $profile.executor_sid -or -not $profile.executor_endpoint) {
        Record 'SECURITY_PROFILE' 'profile' 'FAIL' 'Profile missing mandatory executor identity or endpoint'
    } elseif (-not (Same-Path ([string]$profile.executor_code_root) (Join-Path $security 'executor_code'))) {
        Record 'SECURITY_PROFILE' 'profile' 'FAIL' 'Profile executor code root mismatch'
    } else {
        Record 'SECURITY_PROFILE' 'profile' 'PASS' ('Profile published; executor_sid=' + [string]$profile.executor_sid)
    }
}
$task = $null
if ($Fixture) {
    Record 'EXECUTOR_TASK' 'task_query' 'UNKNOWN' 'Fixture mode: machine-wide Task Scheduler not queried'
} else {
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Record 'EXECUTOR_TASK' 'task' 'FAIL' 'Windows Executor scheduled task not registered'
        } else {
            $actions = @($task.Actions)
            $expectedExe = Join-Path $root '.venv\Scripts\python.exe'
            if ($actions.Count -ne 1 -or -not (Same-Path ([string]$actions[0].Execute) $expectedExe)) {
                Record 'EXECUTOR_TASK' 'task' 'FAIL' 'Task executable is not bound to this installation (may belong to another installation)'
            } elseif ([string]$task.Principal.RunLevel -notin @('Limited','LeastPrivilege')) {
                Record 'EXECUTOR_TASK' 'task' 'FAIL' 'Task does not have a limited run level'
            } elseif ([string]$task.State -ne 'Running') {
                Record 'EXECUTOR_TASK' 'task' 'FAIL' ('Scheduled Executor is not running now: state=' + [string]$task.State)
            } else {
                Record 'EXECUTOR_TASK' 'task' 'PASS' 'Scheduled Executor is running now'
            }
        }
    } catch {
        Record 'EXECUTOR_TASK' 'task' 'UNKNOWN' ('Task query failed: ' + $_.Exception.Message)
    }
}
$serviceFile = ''
if ($profile -and $profile.executor_endpoint) {
    $serviceFile = Join-Path ([string]$profile.executor_endpoint) 'service_state.json'
}
if (-not $serviceFile) {
    Record 'EXECUTOR_SELF_TEST' 'service_state' 'UNKNOWN' 'No executor endpoint available from profile'
    Record 'EXECUTOR_FINAL_READY' 'heartbeat' 'UNKNOWN' 'Cannot evaluate final readiness'
} elseif (-not (Test-Path -LiteralPath $serviceFile -PathType Leaf)) {
    Record 'EXECUTOR_SELF_TEST' 'service_state' 'FAIL' ('Executor did not publish service state: ' + $serviceFile)
    Record 'EXECUTOR_FINAL_READY' 'heartbeat' 'UNKNOWN' 'No heartbeat to evaluate'
} else {
    $service = Read-Json $serviceFile 'EXECUTOR_SELF_TEST' 'service_json'
    if ($null -ne $service) {
        if ($service.self_test.passed -eq $true -and $service.self_test.mode -eq 'on') {
            Record 'EXECUTOR_SELF_TEST' 'self_test' 'PASS' 'Executor reported passed=true and mode=on'
        } else {
            Record 'EXECUTOR_SELF_TEST' 'self_test' 'FAIL' 'Executor self_test is missing/failed or reports unexpected mode'
        }
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$service.heartbeat_at
        if ($age -ge 0 -and $age -lt 10) {
            Record 'EXECUTOR_FINAL_READY' 'heartbeat' 'PASS' ('Heartbeat age seconds: ' + [math]::Round($age, 1))
        } else {
            Record 'EXECUTOR_FINAL_READY' 'heartbeat' 'UNKNOWN' ('Heartbeat stale/unavailable now (postmortem staleness does not prove original failure): ' + [math]::Round($age, 1))
        }
    }
}
$auth = Read-Json $authFile 'MACHINE_AUTHORIZATION' 'machine_json'
if (-not (Test-Path -LiteralPath $authFile -PathType Leaf)) {
    Record 'MACHINE_AUTHORIZATION' 'machine_auth' 'FAIL' 'No machine_authorization.json was published'
} elseif ($null -ne $auth) {
    if ($auth.schema -ne 'SMARTAGENT_MACHINE_AUTHORIZATION_V1' -or
        -not (Same-Path ([string]$auth.install_root) $root)) {
        Record 'MACHINE_AUTHORIZATION' 'machine_auth' 'FAIL' 'Machine Authorization belongs to another root or has invalid schema'
    } elseif (-not (Same-Path ([string]$auth.security_profile) $profileFile)) {
        Record 'MACHINE_AUTHORIZATION' 'machine_auth' 'FAIL' 'Machine Authorization points to a different Security Profile'
    } elseif ($profile -and [string]$auth.executor_sid -ne [string]$profile.executor_sid) {
        Record 'MACHINE_AUTHORIZATION' 'machine_auth' 'FAIL' 'Machine Authorization and profile Executor SID mismatch'
    } else {
        Record 'MACHINE_AUTHORIZATION' 'machine_auth' 'PASS' 'Machine Authorization matches this installation'
    }
}
$failed = @($checks | Where-Object { $_.status -eq 'FAIL' })
$unknown = @($checks | Where-Object { $_.status -eq 'UNKNOWN' })
$firstStage = ''
foreach ($stage in $stages) {
    if (@($failed | Where-Object { $_.stage -eq $stage }).Count -gt 0) {
        $firstStage = $stage
        break
    }
}
$verdict = if ($failed.Count -gt 0) { 'FAILED_OR_INCOMPLETE' } else { 'NOT_PROVEN' }
$modeValue = 'unknown'
if ($mode) { $modeValue = [string]$mode.mode }
# A snapshot never proves that old ACLs are correct or that no pending action
# was running; never output a "full ACL ON PASS" from this read-only audit.
$report = [ordered]@{
    schema='SMARTAGENT_ACL_ON_DIAGNOSIS_V1'
    project_root=$root
    acl_mode=$modeValue
    verdict=$verdict
    earliest_unmet_postcondition=$firstStage
    exact_failure_cause_proven=$false
    full_os_acl_attestation=$false
    fixture_only=[bool]$Fixture
    failed_checks=$failed.Count
    unknown_checks=$unknown.Count
    checks=@($checks)
}
Write-Host ''
Write-Host '===== SMARTAGENT ACL ON DIAGNOSTICS ====='
Write-Host ('Install Root: ' + $root)
Write-Host ('Mode: ' + $report.acl_mode)
foreach ($item in $checks) {
    Write-Host ('[{0}] {1}/{2}: {3}' -f $item.status,$item.stage,$item.key,$item.detail)
}
Write-Host ('Verdict: ' + $verdict)
Write-Host ('Earliest unmet postcondition: ' + $(if ($firstStage) { $firstStage } else { 'NOT_DETERMINABLE' }))
Write-Host 'Important: This is a current-state inference, NOT the original exception or full OS ACL proof.'
if ($ReportPath) {
    $file = [IO.Path]::GetFullPath($ReportPath)
    $json = $report | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($file, $json, (New-Object Text.UTF8Encoding($false)))
    Write-Host ('JSON report: ' + $file)
}
if ($failed.Count -gt 0) { exit 1 }
exit 2
