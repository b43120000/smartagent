# ACLstatus single-entrypoint; disposable fixture, never configures Windows ACL.
$ErrorActionPreference = 'Stop'
$command = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\acl_mode_command.ps1'))
$launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\ACLstatus.bat'))
$fixture = Join-Path $env:RUNNER_TEMP ('SmartAgentAclStatusInline-' + [Guid]::NewGuid().ToString('N'))
$root = Join-Path $fixture 'install'
$sec = Join-Path $root 'localdata\secure\windows_security'
$corePath = Join-Path $root 'install_smart_agent\acl_status.ps1'
$fixtureCasePath = Join-Path $root 'ci_case.txt'
$count = 0

function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw ('ACL_STATUS_INLINE_FIXTURE_FAILED: '+$Message) }
}
function Verify([string]$Mode,[string]$Case,[string]$Result,[int]$ExpectedExit,
                [string]$ExpectedStage,[string]$ExpectedMode) {
    [IO.File]::WriteAllText($fixtureCasePath,$Case)
    $id=[Guid]::NewGuid().ToString('N')
    $receipt=Join-Path $fixture ($id+'.json')
    $trace=Join-Path $fixture ($id+'.txt')
    $args=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$command,'-Mode',$Mode,
            '-ProjectRoot',$root,'-ControllerSid','S-1-5-21-111-222-333-1000',
            '-ReceiptPath',$receipt,'-TranscriptPath',$trace)
    $raw=& powershell.exe @args 2>&1
    $actualExit=$LASTEXITCODE
    Assert ($actualExit -eq $ExpectedExit) ('unexpected child exit: '+$Case+':'+$actualExit+':'+($raw|Out-String))
    Assert (Test-Path -LiteralPath $receipt -PathType Leaf) ('receipt missing: '+$Case)
    $parsed=Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($parsed.schema -eq 'SMARTAGENT_ACL_OPERATION_RESULT_V1') ('receipt schema: '+$Case)
    Assert ($parsed.result -eq $Result) ('receipt result: '+$Case)
    Assert ($parsed.observed_mode -eq $ExpectedMode) ('observed mode: '+$Case+':'+$parsed.observed_mode)
    if ($ExpectedStage) {
        Assert ($parsed.failure_stage -eq $ExpectedStage) ('stage: '+$Case+':'+$parsed.failure_stage)
        Assert (-not [string]::IsNullOrWhiteSpace([string]$parsed.error_message)) ('failure error missing: '+$Case)
        Assert (-not [string]::IsNullOrWhiteSpace([string]$parsed.script_stack_trace)) ('stack missing: '+$Case)
    }
    $viewArgs=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$command,
                '-PrintReceipt','-Mode',$Mode,'-ProjectRoot',$root,
                '-ReceiptPath',$receipt,'-ProcessExitCode',$actualExit)
    $view=& powershell.exe @viewArgs 2>&1
    $viewExit=$LASTEXITCODE
    Assert ($viewExit -eq $ExpectedExit) ('display exit: '+$Case+':'+$viewExit+':'+($view|Out-String))
    $display=($view | Out-String)
    Assert ($display.Contains('RESULT: '+$Result)) ('display wrong result: '+$Case)
    Assert ($display.Contains('Current persisted mode: '+$ExpectedMode)) ('display no current mode: '+$Case)
    if ($ExpectedStage) {
        Assert ($display.Contains('Failed stage: '+$ExpectedStage)) ('display no stage: '+$Case)
        Assert ($display.Contains('Breakpoint:')) ('display missing code location: '+$Case)
    }
    $script:count += 1
    Write-Host ('PASS: '+$Case+' rendered result '+$Result+' at '+$Mode+'; code '+$actualExit)
}

try {
    New-Item -ItemType Directory -Force -Path $sec,(Split-Path -Parent $corePath) | Out-Null
    $stub=@'
param([string]$Mode,[string]$ProjectRoot,[string]$ControllerSid)
$ErrorActionPreference='Stop'
$case=(Get-Content -LiteralPath (Join-Path $ProjectRoot 'ci_case.txt') -Raw).Trim()
if ($case -eq 'ON_FAIL') {
    $target='transitioning_on'
} elseif ($case -eq 'OFF_FAIL') {
    $target='transitioning_off'
} else {
    $target=$Mode
}
$p=Join-Path $ProjectRoot 'localdata\secure\windows_security\acl_mode.json'
@{schema='SMARTAGENT_ACL_MODE_V1';mode=$target;install_root=$ProjectRoot} |
    ConvertTo-Json | Set-Content -LiteralPath $p -Encoding UTF8
if ($case -eq 'ON_FAIL') {
    throw 'Register-ScheduledTask: CI_INJECTED_TASK_REGISTRATION_ERROR'
}
if ($case -eq 'OFF_FAIL') {
    throw 'acl_off_postcondition_write_probe_failed: CI_INJECTED_WRITE_PROBE'
}
if ($case -eq 'ON_FALSE_SUCCESS') {
    $obj=Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
    $obj.mode='transitioning_on'
    $obj | ConvertTo-Json | Set-Content -LiteralPath $p -Encoding UTF8
}
'@
    [IO.File]::WriteAllText($corePath,$stub,(New-Object Text.UTF8Encoding($false)))
    $launcherResult=& cmd.exe /d /c ('"' + $launcher + '" --launcher-self-test')
    Assert ($LASTEXITCODE -eq 0) ('launcher selftest: '+($launcherResult|Out-String))
    Assert (($launcherResult | Out-String).Contains('ACLSTATUS_LAUNCHER_PARSE_OK')) 'launcher did not generate GUID'
    # Exercise the exact CMD invocation with an absolute script path containing spaces.
    $withSpaces=Join-Path $fixture 'root with spaces'
    $testScripts=Join-Path $withSpaces 'install_smart_agent'
    New-Item -ItemType Directory -Path $testScripts -Force | Out-Null
    Copy-Item -LiteralPath $launcher -Destination (Join-Path $withSpaces 'ACLstatus.bat') -ErrorAction Stop
    Copy-Item -LiteralPath ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\acl_mode_launcher.ps1'))) -Destination (Join-Path $testScripts 'acl_mode_launcher.ps1') -ErrorAction Stop
    # Absolute invocation matching the reported C:\smartagent\ACLstatus.bat off.
    $absoluteRun=& cmd.exe /d /c ($launcher + ' --launcher-self-test') 2>&1
    Assert ($LASTEXITCODE -eq 0) ('absolute launcher exit: '+($absoluteRun|Out-String))
    Assert (($absoluteRun | Out-String).Contains('ACLSTATUS_LAUNCHER_PARSE_OK')) 'absolute launcher has no GUID'
    # CMD location with spaces; avoid nested, synthetic cmd /s quotation.
    Push-Location -LiteralPath $withSpaces
    try {
        $spacesRun=& cmd.exe /d /c 'ACLstatus.bat --launcher-self-test' 2>&1
        Assert ($LASTEXITCODE -eq 0) ('spaced-path launcher exit: '+($spacesRun|Out-String))
        Assert (($spacesRun | Out-String).Contains('ACLSTATUS_LAUNCHER_PARSE_OK')) 'spaced-path launcher has no GUID'
    } finally { Pop-Location }
    Verify 'on' 'ON_FAIL' 'FAIL' 1 'ON_EXECUTOR_TASK_REGISTRATION' 'transitioning_on'
    Verify 'off' 'OFF_FAIL' 'FAIL' 1 'OFF_CONTROLLER_WRITE_VERIFICATION' 'transitioning_off'
    Verify 'on' 'ON_PASS' 'PASS' 0 '' 'on'
    Verify 'off' 'OFF_PASS' 'PASS' 0 '' 'off'
    Verify 'on' 'ON_FALSE_SUCCESS' 'FAIL' 1 'ON_FINAL_COMMIT' 'transitioning_on'
    $missing=Join-Path $fixture 'missing.json'
    $viewArgs=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$command,
                '-PrintReceipt','-Mode','on','-ProjectRoot',$root,
                '-ReceiptPath',$missing,'-ProcessExitCode','0')
    $view=& powershell.exe @viewArgs 2>&1
    Assert ($LASTEXITCODE -ne 0) 'missing receipt unexpectedly accepted'
    Assert (($view | Out-String).Contains('RESULT: FAIL')) 'missing receipt did not render FAIL'
    $count += 1
    Write-Host ('ACLSTATUS_INLINE_FIXTURE_PASS cases='+$count)
} finally {
    if (Test-Path -LiteralPath $fixture) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
    }
}

# Expected negative cases must not leak LASTEXITCODE=1 to the CI shell.
exit 0
