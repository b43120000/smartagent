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
    # ToUnixTimeSeconds rounds down while Python time.time() has fractions:
    # a freshly published heartbeat may appear up to ~1 second in the future.
    Assert ($heartbeatAge -ge -2 -and $heartbeatAge -lt 15) 'CI_EXECUTOR_HEARTBEAT_STALE'
    $verification.self_test_passed=$true
    $verification.self_test_mode='on'
    $verification.executor_non_admin=$true
    $verification.heartbeat_age_sec=$heartbeatAge
    $onPassed=$true
    Write-Host 'CI_REAL_ACL_ON_ATTESTATION_PASS'

    # Reproduce Edit Workspace Apply AFTER ACL ON: same password-backed
    # Executor, an added Turbo root strictly beneath the writable workspace.
    # This must reuse the stored task credential without Set-ScheduledTask.
    Step 'WORKSPACE_REAPPLY_EXISTING_PASSWORD_TASK'
    $turboRoot=Join-Path $workspace 'SmartAgent_GoalDriven_IT_GH_v1'
    New-Item -ItemType Directory -Force -Path $turboRoot | Out-Null
    $policy.turbo_roots=@($turboRoot)
    $policy.revision=2
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($false)))
    $taskBefore=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $beforeUser=[string]$taskBefore.Principal.UserId
    $beforeLogon=[string]$taskBefore.Principal.LogonType
    $beforeRunLevel=[string]$taskBefore.Principal.RunLevel
    $beforePasswordSet=(Get-LocalUser -Name ([string]$profile.executor_user) -ErrorAction Stop).PasswordLastSet
    $configure=Join-Path $root 'install_smart_agent\configure_security.ps1'
    & $configure -WorkspaceContainer $workspace -AdditionalWriteRoots @() `
        -RuntimeRoot ([string]$profile.runtime_root) -DeniedWriteRoots @($denied) `
        -ReadOnlyRoots @($readonly) -SkillRoot ([string]$profile.skill_roots[0]) `
        -ExecutorUser ([string]$profile.executor_user) -ProfilePath (Join-Path $sec 'security_profile.json') `
        -ExecutorPython $py -ControllerSid ([string]$identity.User.Value) `
        -SkipAclModeRestore -NonInteractive -Apply
    Step 'WORKSPACE_REAPPLY_ATTESTATION'
    $taskAfter=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Assert ([string]$taskAfter.Principal.UserId -eq $beforeUser) 'CI_REAPPLY_TASK_USER_CHANGED'
    Assert ([string]$taskAfter.Principal.LogonType -eq $beforeLogon) 'CI_REAPPLY_TASK_LOGON_CHANGED'
    Assert ([string]$taskAfter.Principal.RunLevel -eq $beforeRunLevel) 'CI_REAPPLY_TASK_RUNLEVEL_CHANGED'
    Assert ([string]$taskAfter.State -eq 'Running') 'CI_REAPPLY_EXECUTOR_NOT_RUNNING'
    $afterPasswordSet=(Get-LocalUser -Name ([string]$profile.executor_user) -ErrorAction Stop).PasswordLastSet
    Assert ($afterPasswordSet -eq $beforePasswordSet) 'CI_REAPPLY_EXECUTOR_PASSWORD_ROTATED'
    Assert ((Get-Mode) -eq 'on') 'CI_REAPPLY_ACL_MODE_CHANGED'
    $reapplyProfile=Get-Content -LiteralPath (Join-Path $sec 'security_profile.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $reapplyState=Get-Content -LiteralPath ([IO.Path]::Combine([string]$reapplyProfile.executor_endpoint,'service_state.json')) -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($reapplyState.self_test.passed -eq $true -and $reapplyState.self_test.mode -eq 'on') 'CI_REAPPLY_SELFTEST_FAILED'
    Assert ($reapplyState.is_admin -eq $false) 'CI_REAPPLY_EXECUTOR_ADMIN'
    $verification.workspace_reapply_passed=$true
    $verification.workspace_reapply_executor_user_unchanged=$true
    $verification.workspace_reapply_password_unchanged=$true
    $verification.workspace_reapply_turbo_root=$turboRoot
    Write-Host 'CI_WORKSPACE_REAPPLY_PRESERVED_TASK_CREDENTIAL_PASS'

    # Real ACLstatus ON re-entry must auto-enroll the exact configured NEW
    # Turbo Root. Never silently import the existing/populated project tree.
    Step 'ACL_ON_AUTO_TURBO_NATIVE_ENROLLMENT'
    $freshTurboRoot=Join-Path $workspace 'fresh_turbo_native_root'
    Assert (-not (Test-Path -LiteralPath $freshTurboRoot)) 'CI_FRESH_TURBO_ROOT_ALREADY_EXISTS'
    $policy.turbo_roots=@($freshTurboRoot)
    $policy.turbo_requested=$true
    $policy.revision=3
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($false)))
    & $aclScript -Mode on -ProjectRoot $root -ControllerSid ([string]$identity.User.Value)
    Assert ((Get-Mode) -eq 'on') 'CI_AUTO_TURBO_ACL_ON_NOT_COMMITTED'
    $turboJournal=Join-Path $sec 'turbo_os_activation.json'
    Assert (Test-Path -LiteralPath $turboJournal -PathType Leaf) 'CI_AUTO_TURBO_JOURNAL_MISSING'
    $nativeRecord=Get-Content -LiteralPath $turboJournal -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($nativeRecord.status -eq 'ACTIVE_PROVISIONED') 'CI_AUTO_TURBO_ROOT_NOT_PROVISIONED'
    Assert ([int]$nativeRecord.policy_revision -eq 3) 'CI_AUTO_TURBO_POLICY_REVISION_MISMATCH'
    Assert ((Test-Path -LiteralPath $freshTurboRoot -PathType Container)) 'CI_AUTO_TURBO_ROOT_NOT_CREATED'
    Assert ([string]$nativeRecord.package_sid -match '^S-1-15-2-') 'CI_AUTO_TURBO_PACKAGE_SID_INVALID'
    $verification.acl_on_turbo_auto_enrolled=$true
    $verification.turbo_package_sid=[string]$nativeRecord.package_sid
    Write-Host 'CI_ACL_ON_NATIVE_TURBO_AUTO_ENROLLED_PASS'
} catch {
    $failure=[string]$_.Exception.Message
    $onPassed=$false
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
if ($failure -or -not ($onPassed -and $offPassed)) { throw ('CI_REAL_ACL_DEPLOYMENT_FAILED: '+$failure) }
Write-Host 'CI_REAL_ACL_ON_TO_OFF_FULL_CYCLE_PASS'
