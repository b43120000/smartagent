# Internal ACLstatus.bat transport: elevated execution + same-window terminal receipt.
# This is NOT a separate ACL diagnostic command and changes no ACL policy.
[CmdletBinding()]
param(
    [ValidateSet('on','off')][string]$Mode = '',
    [string]$ProjectRoot = '',
    [string]$ControllerSid = '',
    [string]$ReceiptPath = '',
    [string]$TranscriptPath = '',
    [switch]$PrintReceipt,
    [int]$ProcessExitCode = 1
)
$ErrorActionPreference = 'Stop'
function Normalize-Root([string]$Value) {
    try { return [IO.Path]::GetFullPath($Value).TrimEnd('\') }
    catch { return '' }
}
function Read-PersistedMode([string]$Root) {
    try {
        $path=Join-Path $Root 'localdata\secure\windows_security\acl_mode.json'
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return 'MISSING' }
        $v=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($v.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
            -not (Normalize-Root ([string]$v.install_root)).Equals(
                $Root,[StringComparison]::OrdinalIgnoreCase)) { return 'UNTRUSTED_OR_WRONG_INSTALL' }
        return [string]$v.mode
    } catch { return 'UNREADABLE' }
}
function Classify-Failure([string]$Mode,[string]$Message,[string]$Stack) {
    $both=$Message+' '+$Stack
    if ($Mode -eq 'on') {
        switch -Regex ($both) {
            'ACL_ON_ENVIRONMENT|PROTOCOL_MANIFEST|workspace_access_policy|provisioning_options' { return 'ON_PRECHECK' }
            'acl_executor_instance|executor_account_task|orphaned|Set-LocalUser|New-LocalUser|BatchLogon' { return 'ON_EXECUTOR_ACCOUNT' }
            'Register-ScheduledTask|New-ScheduledTaskAction|executor_task_definition_mismatch' { return 'ON_EXECUTOR_TASK_REGISTRATION' }
            'Restricted executor did not become ready|start restricted executor|service_state|heartbeat' { return 'ON_EXECUTOR_STARTUP' }
            'attestation failed|executor attestation' { return 'ON_EXECUTOR_ATTESTATION' }
            'machine_authorization|Machine Authorization' { return 'ON_MACHINE_AUTHORIZATION' }
            'icacls failed|ACL.*overlap|Refusing to replace custom|Security root|profile' { return 'ON_NTFS_ACL_OR_SECURITY_PROFILE' }
            'mode_transition_failed|ON_RETURNED_WITHOUT' { return 'ON_FINAL_COMMIT' }
            default { return 'ON_PROVISIONING_OR_POSTCONDITION' }
        }
    }
    switch -Regex ($both) {
        'revocation.*owner|revocation.*invalid|executor_is_controller' { return 'OFF_REVOCATION_OWNERSHIP' }
        'stop_timeout|Stop-ScheduledTask|Disable-ScheduledTask|task_changed' { return 'OFF_EXECUTOR_SHUTDOWN' }
        'TURBO_OS|TURBO_POLICY|turbo_os_activation' { return 'OFF_TURBO_REVOCATION' }
        'icacls failed|executor_ace_remaining|Refusing to replace' { return 'OFF_NTFS_ACL_REVOCATION' }
        'postcondition|write_probe|controller_write' { return 'OFF_CONTROLLER_WRITE_VERIFICATION' }
        'Unregister-ScheduledTask|task_remaining' { return 'OFF_TASK_REMOVAL' }
        'OFF_RETURNED_WITHOUT|acl_mode_invalid' { return 'OFF_FINAL_COMMIT' }
        default { return 'OFF_REVOCATION_OR_POSTCONDITION' }
    }
}
function Print-Receipt {
    $root=Normalize-Root $ProjectRoot
    $receipt=$null
    if ($ReceiptPath -and (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
        try { $receipt=Get-Content -LiteralPath $ReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { Write-Host ('[SmartAgent ACL] Receipt parse failed: '+$_.Exception.Message) -ForegroundColor Yellow }
    }
    $recordOK=$null -ne $receipt -and
        [string]$receipt.schema -eq 'SMARTAGENT_ACL_OPERATION_RESULT_V1' -and
        [string]$receipt.mode -eq $Mode -and
        (Normalize-Root ([string]$receipt.install_root)) -ieq $root
    $current=if ($root) { Read-PersistedMode $root } else { 'UNKNOWN' }
    $pass=$recordOK -and $ProcessExitCode -eq 0 -and
        [string]$receipt.result -eq 'PASS' -and $current -eq $Mode
    Write-Host ''
    Write-Host '============================================================'
    Write-Host ('[SmartAgent ACL] Operation: '+$Mode.ToUpperInvariant())
    if ($pass) {
        Write-Host '[SmartAgent ACL] RESULT: PASS' -ForegroundColor Green
        Write-Host ('[SmartAgent ACL] Completed: '+$Mode.ToUpperInvariant()+' postconditions accepted')
    } else {
        Write-Host '[SmartAgent ACL] RESULT: FAIL' -ForegroundColor Red
        if ($recordOK) {
            Write-Host ('[SmartAgent ACL] Failed stage: '+[string]$receipt.failure_stage) -ForegroundColor Red
            Write-Host ('[SmartAgent ACL] Error: '+[string]$receipt.error_message) -ForegroundColor Red
            if ($receipt.error_id) { Write-Host ('[SmartAgent ACL] Error ID: '+[string]$receipt.error_id) }
            if ($receipt.source_location) {
                Write-Host ('[SmartAgent ACL] Breakpoint: '+[string]$receipt.source_location)
            }
            if ($receipt.script_stack_trace) {
                Write-Host '[SmartAgent ACL] Script stack:'
                @(([string]$receipt.script_stack_trace -split '\r?\n') | Select-Object -First 8) |
                    ForEach-Object { if ($_.Trim()) { Write-Host ('    '+$_) } }
            }
        } else {
            Write-Host '[SmartAgent ACL] No verified child result was received.' -ForegroundColor Red
            Write-Host '[SmartAgent ACL] Possible elevation cancellation / child startup / receipt failure.'
        }
    }
    Write-Host ('[SmartAgent ACL] Current persisted mode: '+$current)
    Write-Host ('[SmartAgent ACL] Elevated process exit: '+$ProcessExitCode)
    if ($ReceiptPath) { Write-Host ('[SmartAgent ACL] Result receipt: '+$ReceiptPath) }
    if ($recordOK -and $receipt.transcript_path) {
        Write-Host ('[SmartAgent ACL] Full execution log: '+[string]$receipt.transcript_path)
    }
    Write-Host '============================================================'
    if ($pass) { exit 0 }
    exit 1
}
if ($PrintReceipt) { Print-Receipt }
if (-not $Mode -or -not $ProjectRoot -or -not $ReceiptPath) {
    throw 'ACL_STATUS_INTERNAL_ARGUMENTS_MISSING'
}
$root=Normalize-Root $ProjectRoot
if (-not $root) { throw 'ACL_STATUS_INTERNAL_ROOT_INVALID' }
$core=Join-Path $root 'install_smart_agent\acl_status.ps1'
$result='FAIL'
$failureStage=''
$errorMessage=''
$errorId=''
$sourceLocation=''
$stack=''
$transcriptStarted=$false
$transcriptError=''
$actualMode='UNKNOWN'
try {
    if (-not (Test-Path -LiteralPath $core -PathType Leaf)) {
        throw ('ACL_CORE_SCRIPT_MISSING: '+$core)
    }
    if ($TranscriptPath) {
        try {
            Start-Transcript -LiteralPath $TranscriptPath -ErrorAction Stop | Out-Null
            $transcriptStarted=$true
        } catch {
            $transcriptError=$_.Exception.Message
            Write-Host ('[SmartAgent ACL] Transcript unavailable: '+$transcriptError) -ForegroundColor Yellow
        }
    }
    Write-Host ('[SmartAgent ACL] Starting '+$Mode.ToUpperInvariant()+' security operation.')
    & $core -Mode $Mode -ProjectRoot $root -ControllerSid $ControllerSid
    $actualMode=Read-PersistedMode $root
    if ($actualMode -ne $Mode) {
        throw ('ACL_'+$Mode.ToUpperInvariant()+'_POSTCONDITION_MODE_MISMATCH: '+$actualMode)
    }
    $result='PASS'
    Write-Host ('[SmartAgent ACL] '+$Mode.ToUpperInvariant()+' operation PASS; mode='+$actualMode) -ForegroundColor Green
} catch {
    $e=$_
    $errorMessage=[string]$e.Exception.Message
    $errorId=[string]$e.FullyQualifiedErrorId
    $stack=[string]$e.ScriptStackTrace
    $sourceLocation=[string]$e.InvocationInfo.PositionMessage
    $failureStage=Classify-Failure $Mode $errorMessage $stack
    $actualMode=Read-PersistedMode $root
    Write-Host ('[SmartAgent ACL] '+$Mode.ToUpperInvariant()+' operation FAIL') -ForegroundColor Red
    Write-Host ('[SmartAgent ACL] Failed stage: '+$failureStage) -ForegroundColor Red
    Write-Host ('[SmartAgent ACL] Error: '+$errorMessage) -ForegroundColor Red
    if ($sourceLocation) { Write-Host ('[SmartAgent ACL] Breakpoint: '+$sourceLocation) }
    if ($stack) { Write-Host ('[SmartAgent ACL] Stack: '+$stack) }
    Write-Host ('[SmartAgent ACL] Current persisted mode: '+$actualMode)
} finally {
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
    $receipt=[ordered]@{
        schema='SMARTAGENT_ACL_OPERATION_RESULT_V1'
        mode=$Mode
        install_root=$root
        result=$result
        failure_stage=$failureStage
        error_message=$errorMessage
        error_id=$errorId
        source_location=$sourceLocation
        script_stack_trace=$stack
        observed_mode=$actualMode
        transcript_path=if ($transcriptStarted) { $TranscriptPath } else { '' }
        timestamp_utc=[DateTimeOffset]::UtcNow.ToString('o')
    }
    try {
        $parent=Split-Path -Parent $ReceiptPath
        if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
            throw 'ACL_STATUS_RECEIPT_DIRECTORY_MISSING'
        }
        [IO.File]::WriteAllText(
            [IO.Path]::GetFullPath($ReceiptPath),
            ($receipt | ConvertTo-Json -Depth 8),
            (New-Object Text.UTF8Encoding($false))
        )
    } catch {
        Write-Host ('[SmartAgent ACL] ERROR: Unable to write terminal receipt: '+$_.Exception.Message) -ForegroundColor Red
        $result='FAIL'
    }
}
if ($result -eq 'PASS') { exit 0 }
exit 1
