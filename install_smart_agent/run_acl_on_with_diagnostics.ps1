# Explicit opt-in ACL ON execution with a persistent, original PowerShell error log.
# Read-only examination is available separately via diagnose_acl_on.ps1.
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$LogDirectory = ''
)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$caller=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=New-Object Security.Principal.WindowsPrincipal($caller)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'DIAGNOSTIC_ACL_ON_REQUIRES_ADMIN'
}
$aclScript=Join-Path $PSScriptRoot 'acl_status.ps1'
$diagnosisScript=Join-Path $PSScriptRoot 'diagnose_acl_on.ps1'
if (-not (Test-Path -LiteralPath $aclScript -PathType Leaf) -or
    -not (Test-Path -LiteralPath $diagnosisScript -PathType Leaf)) {
    throw 'ACL_ON_DIAGNOSTIC_SCRIPTS_MISSING'
}
Write-Host 'This command WILL attempt ACL ON and modify Windows permissions / restricted executor state.' -ForegroundColor Yellow
Write-Host ('Install root: ' + $root)
Write-Host 'For analysis without any state changes, run check_acl_on.bat instead.'
$choice=Read-Host 'Type exactly RUN-ACL-ON to proceed (anything else cancels)'
if ($choice -cne 'RUN-ACL-ON') {
    Write-Host '[CANCELLED] ACL ON not executed.'
    exit 3
}
if (-not $LogDirectory) {
    $LogDirectory=Join-Path $env:LOCALAPPDATA 'SmartAgent\acl_on_diagnostics'
}
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$stamp=(Get-Date).ToUniversalTime().ToString('yyyyMMdd_HHmmss')
$id=[Guid]::NewGuid().ToString('N').Substring(0,8)
$prefix=Join-Path $LogDirectory ('acl_on_' + $stamp + '_' + $id)
$transcript=$prefix+'.txt'
$failureFile=$prefix+'_error.json'
$reportFile=$prefix+'_snapshot.json'
$originalFailed=$false
$onCommitted=$false
$transcriptStarted=$false
try {
    Start-Transcript -Path $transcript -ErrorAction Stop | Out-Null
    $transcriptStarted=$true
    Write-Host ('[ACL_ON_DIAG] Transcript: ' + $transcript)
    Write-Host '[ACL_ON_DIAG] Original ACL ON implementation begins.'
    try {
        & $aclScript -Mode on -ProjectRoot $root -ControllerSid ([string]$caller.User.Value)
        $modePath=Join-Path $root 'localdata\secure\windows_security\acl_mode.json'
        if (-not (Test-Path -LiteralPath $modePath -PathType Leaf)) {
            throw 'ACL_ON_RETURNED_WITHOUT_MODE_FILE'
        }
        $mode=Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($mode.mode -ne 'on') {
            throw ('ACL_ON_RETURNED_WITHOUT_ON_COMMIT:mode=' + [string]$mode.mode)
        }
        $onCommitted=$true
        Write-Host '[ACL_ON_DIAG] ACLstatus.ps1 finished and mode=on.'
    } catch {
        $originalFailed=$true
        $errorRecord=$_
        $message=[string]$errorRecord.Exception.Message
        $at=if ($errorRecord.InvocationInfo) {
            [string]$errorRecord.InvocationInfo.PositionMessage
        } else { '' }
        $stack=[string]$errorRecord.ScriptStackTrace
        $failure=[ordered]@{
            schema='SMARTAGENT_ACL_ON_CAPTURED_ERROR_V1'
            project_root=$root
            error_message=$message
            invocation=$at
            script_stack_trace=$stack
            captured_at=(Get-Date).ToUniversalTime().ToString('o')
            original_acl_on_failed=$true
        }
        [IO.File]::WriteAllText($failureFile,
            ($failure | ConvertTo-Json -Depth 6),
            (New-Object Text.UTF8Encoding($false)))
        Write-Host '[ACL_ON_DIAG] ORIGINAL ACL ON ERROR:' -ForegroundColor Red
        Write-Host $message
        Write-Host $at
        Write-Host $stack
        Write-Host ('[ACL_ON_DIAG] Exact error receipt: ' + $failureFile)
    }
    Write-Host '[ACL_ON_DIAG] Reading postmortem stage evidence. No repairs or retries.'
    try {
        & $diagnosisScript -ProjectRoot $root -ReportPath $reportFile
        Write-Host ('[ACL_ON_DIAG] Snapshot exit code: ' + [string]$LASTEXITCODE)
    } catch {
        Write-Host ('[ACL_ON_DIAG] Snapshot failed: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
    Write-Host ('[ACL_ON_DIAG] Stage evidence: ' + $reportFile)
} finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
    Write-Host ('[ACL_ON_DIAG] Full original console transcript: ' + $transcript)
}
if ($originalFailed -or -not $onCommitted) { exit 1 }
exit 0
