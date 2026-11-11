# Real Windows ACL ON -> OFF integration test.
# GitHub-hosted disposable Windows runner ONLY. Do NOT run on a personal PC.
[CmdletBinding()]
param([string]$InstallRoot = $env:GITHUB_WORKSPACE)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows' -or
    [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    throw 'REAL_ACL_ON_CI_REQUIRES_GITHUB_HOSTED_DISPOSABLE_WINDOWS_RUNNER'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'CI_RUNNER_NOT_ELEVATED_FOR_REAL_WINDOWS_ACL'
}
$root=[IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $root '.git') -PathType Container)) {
    throw 'CI_INSTALL_ROOT_MUST_BE_CHECKOUT'
}
$fixture=Join-Path $env:RUNNER_TEMP ('SmartAgentAclOnCi-' + [Guid]::NewGuid().ToString('N'))
$workspace=Join-Path $fixture 'workspace'
$readonly=Join-Path $fixture 'read_only'
$denied=Join-Path $fixture 'denied'
$sec=Join-Path $root 'localdata\secure\windows_security'
$policyPath=Join-Path $sec 'workspace_access_policy.json'
$modePath=Join-Path $sec 'acl_mode.json'
$aclScript=Join-Path $root 'install_smart_agent\acl_status.ps1'
$diagScript=Join-Path $root 'install_smart_agent\diagnose_acl_on.ps1'
$evidence=Join-Path $env:RUNNER_TEMP 'acl_on_ci_evidence.json'
$mode='NOT_STARTED'
$onPassed=$false
$offPassed=$false
$stage='START'
$failure=''
$diagOutput=''
$taskName='SmartAgent Restricted Executor'
$taskPresentAtOn=$false
$heartbeatAge=$null
$verification=[ordered]@{}

function Assert([bool]$Condition,[string]$ErrorCode) {
    if (-not $Condition) { throw $ErrorCode }
}
function Step([string]$Name) {
    $script:stage=$Name
    Write-Host ('[ACL_ON_CI_STAGE] '+$Name)
}
function Get-Mode {
    if (-not (Test-Path -LiteralPath $modePath -PathType Leaf)) { return 'MISSING' }
    return [string](Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json).mode
}
try {
    Step 'FIXTURE_SETUP'
    New-Item -ItemType Directory -Force -Path $workspace,$readonly,$denied,$sec | Out-Null
    [IO.File]::WriteAllText((Join-Path $readonly 'sample.txt'),'READ_ONLY_DATA')
    [IO.File]::WriteAllText((Join-Path $denied 'sample.txt'),'DENIED_DATA')
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=1
        writable_workspaces=@($workspace)
        read_only_roots=@($readonly)
        denied_write_roots=@($denied)
        turbo_roots=@()
        turbo_requested=$false
    }
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($false)))
    Step 'PRECHECK_MANIFEST'
    $py=Join-Path $root '.venv\Scripts\python.exe'
    Assert (Test-Path -LiteralPath $py -PathType Leaf) 'CI_VENV_PYTHON_MISSING'
    $source=Join-Path $root 'source'
    $validate="import sys;sys.path.insert(0,sys.argv[1]);from agent_core.protocol_manifest import validate_protocol_manifest;r=validate_protocol_manifest(sys.argv[2]);print('hash_mismatches='+repr(r['hash_mismatches']));sys.exit(1 if r['hash_mismatches'] else 0)"
    & $py -I -B -c $validate $source $root
    Assert ($LASTEXITCODE -eq 0) 'CI_MANIFEST_HASH_MISMATCH'
    Step 'ACL_ON_ORIGINAL_IMPLEMENTATION'
    & $aclScript -Mode on -ProjectRoot $root -ControllerSid ([string]$identity.User.Value)
    Step 'ACL_ON_COMMIT'
    $mode=Get-Mode
    Assert ($mode -eq 'on') ('CI_ACL_ON_MODE_NOT_COMMITTED:'+ $mode)
    Step 'EXECUTOR_TASK'
    $task=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $taskPresentAtOn=($null -ne $task)
    Assert $taskPresentAtOn 'CI_EXECUTOR_TASK_MISSING'
    $verification.task_running = ([string]$task.State -eq 'Running')
    Assert $verification.task_running 'CI_EXECUTOR_TASK_NOT_RUNNING'
    Step 'MACHINE_AUTHORIZATION'
    $machinePath=Join-Path $env:ProgramData 'SmartAgent\machine_authorization.json'
    Assert (Test-Path -LiteralPath $machinePath -PathType Leaf) 'CI_MACHINE_AUTHORIZATION_MISSING'
    $auth=Get-Content -LiteralPath $machinePath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ([string]$auth.install_root -ieq $root) 'CI_MACHINE_AUTH_OWNER_MISMATCH'
    Step 'EXECUTOR_SERVICE_REAL_ATTESTATION'
    $profile=Get-Content -LiteralPath (Join-Path $sec 'security_profile.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ([string]$profile.executor_sid -eq [string]$auth.executor_sid) 'CI_EXECUTOR_SID_MISMATCH'
    $servicePath=Join-Path ([string]$profile.executor_endpoint) 'service_state.json'
    Assert (Test-Path -LiteralPath $servicePath -PathType Leaf) 'CI_SERVICE_STATE_MISSING'
    $state=Get-Content -LiteralPath $servicePath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($state.self_test.passed -eq $true) ('CI_EXECUTOR_SELF_TEST_FAILED:'+ ($state.self_test.reasons -join ','))
    Assert ($state.self_test.mode -eq 'on') 'CI_EXECUTOR_SELF_TEST_MODE_NOT_ON'
    Assert ($state.is_admin -eq $false) 'CI_RESTRICTED_EXECUTOR_IS_ADMIN'
    Assert ([string]$state.user -ieq [string]$profile.executor_user) 'CI_EXECUTOR_IDENTITY_MISMATCH'
    $heartbeatAge=[math]::Round([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$state.heartbeat_at,2)
    Assert ($heartbeatAge -ge 0 -and $heartbeatAge -lt 15) 'CI_EXECUTOR_HEARTBEAT_STALE'
    $verification.self_test_passed=$true
    $verification.self_test_mode='on'
    $verification.executor_non_admin=$true
    $verification.heartbeat_age_sec=$heartbeatAge
    $onPassed=$true
    Write-Host 'CI_REAL_ACL_ON_ATTESTATION_PASS'
} catch {
    $failure=[string]$_.Exception.Message
    Write-Host ('[ACL_ON_CI_ERROR] stage='+$stage+' error='+$failure) -ForegroundColor Red
    Write-Host ($_ | Out-String)
    if (Test-Path -LiteralPath $diagScript -PathType Leaf) {
        try {
            $diagOutput=(& $diagScript -ProjectRoot $root 2>&1 | Out-String)
            Write-Host $diagOutput
        } catch {
            Write-Host ('Diagnostic snapshot failed: '+$_.Exception.Message)
        }
    }
} finally {
    Step 'ACL_OFF_REVOCATION'
    try {
        & $aclScript -Mode off -ProjectRoot $root -ControllerSid ([string]$identity.User.Value)
        $mode=Get-Mode
        Assert ($mode -eq 'off') ('CI_ACL_OFF_MODE_NOT_COMMITTED:'+ $mode)
        $task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Assert ($null -eq $task) 'CI_ACL_OFF_TASK_REMAINS'
        $machinePath=Join-Path $env:ProgramData 'SmartAgent\machine_authorization.json'
        if (Test-Path -LiteralPath $machinePath) {
            $auth=Get-Content -LiteralPath $machinePath -Raw -Encoding UTF8 | ConvertFrom-Json
            Assert ([string]$auth.install_root -ine $root) 'CI_ACL_OFF_MACHINE_AUTH_REMAINS'
        }
        $offPassed=$true
        Write-Host 'CI_REAL_ACL_OFF_REVOCATION_PASS'
    } catch {
        $offError=[string]$_.Exception.Message
        Write-Host ('[ACL_OFF_CI_ERROR] '+$offError) -ForegroundColor Red
        if (-not $failure) { $failure='ACL_OFF_REVOKE_FAILED:'+ $offError }
    }
    $report=[ordered]@{
        schema='SMARTAGENT_CI_REAL_ACL_ON_OFF_V1'
        github_hosted_windows=$true
        root=$root
        fixture=$fixture
        on_passed=$onPassed
        off_passed=$offPassed
        task_present_at_on=$taskPresentAtOn
        last_stage=$stage
        last_mode=$mode
        failure=$failure
        verification=$verification
        diagnosis_snapshot=$diagOutput
    }
    [IO.File]::WriteAllText($evidence,($report | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($false)))
    Write-Host ('[ACL_CI_EVIDENCE] '+$evidence)
}
if (-not ($onPassed -and $offPassed)) { throw ('CI_REAL_ACL_DEPLOYMENT_FAILED: '+$failure) }
Write-Host 'CI_REAL_ACL_ON_TO_OFF_FULL_CYCLE_PASS'
