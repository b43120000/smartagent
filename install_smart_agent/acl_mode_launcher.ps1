# Internal ACLstatus.bat runner. Generate the result ID in PowerShell rather
# than inside CMD FOR /F, whose nested quotes break on some Windows hosts.
[CmdletBinding()]
param(
    [ValidateSet('on','off')][string]$Mode = '',
    [string]$ProjectRoot = '',
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'
$runId = [Guid]::NewGuid().ToString('N')
if ($runId -notmatch '^[0-9a-f]{32}$') {
    Write-Host '[SmartAgent ACL] FAIL: invalid result identity.' -ForegroundColor Red
    exit 2
}
if ($SelfTest) {
    Write-Host 'ACLSTATUS_LAUNCHER_PARSE_OK'
    exit 0
}
if (-not $Mode -or -not $ProjectRoot) {
    Write-Host '[SmartAgent ACL] FAIL: ON/OFF mode or project root missing.' -ForegroundColor Red
    exit 2
}
try {
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $command = Join-Path $root 'install_smart_agent\acl_mode_command.ps1'
    if (-not (Test-Path -LiteralPath $command -PathType Leaf)) {
        throw ('ACL_STATUS_INTERNAL_COMMAND_MISSING: ' + $command)
    }
    $callerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $logDir = Join-Path $env:LOCALAPPDATA 'SmartAgent\acl_status'
    if (-not (Test-Path -LiteralPath $logDir -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    $receipt = Join-Path $logDir ('acl_' + $Mode + '_' + $runId + '.json')
    $transcript = Join-Path $logDir ('acl_' + $Mode + '_' + $runId + '.txt')
    $exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    Write-Host ('[SmartAgent ACL] Starting ' + $Mode.ToUpperInvariant() + '...')
    Write-Host '[SmartAgent ACL] Windows security permissions will change.'
    Write-Host '[SmartAgent ACL] PASS/FAIL and original error will be shown here.'
    $childExit = 3
    try {
        # Start-Process -ArgumentList joins the array with spaces.
        # Quote all filesystem paths explicitly, including spaces.
        $arguments = @(
            '-NoLogo','-NoProfile','-ExecutionPolicy','Bypass',
            '-File',('"' + $command + '"'),
            '-Mode',$Mode,
            '-ProjectRoot',('"' + $root + '"'),
            '-ControllerSid',$callerSid,
            '-ReceiptPath',('"' + $receipt + '"'),
            '-TranscriptPath',('"' + $transcript + '"')
        )
        $process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $root -Verb RunAs -Wait -PassThru -ErrorAction Stop
        $childExit = [int]$process.ExitCode
    } catch {
        Write-Host ('[SmartAgent ACL] Elevation or child launch failed: ' + $_.Exception.Message) -ForegroundColor Red
    }

    # A missing/invalid receipt must never turn a failed launch into PASS.
    & $command -PrintReceipt -Mode $Mode -ProjectRoot $root -ReceiptPath $receipt -ProcessExitCode $childExit
    $displayExit = $LASTEXITCODE

    $statusScript = Join-Path $root 'install_smart_agent\turbo_shared_status.ps1'
    if (Test-Path -LiteralPath $statusScript -PathType Leaf) {
        Write-Host ''
        Write-Host '[SmartAgent ACL] Current shared ACL/Turbo state:'
        try {
            & $statusScript -ProjectRoot $root
        } catch {
            Write-Host ('[SmartAgent ACL] Status reporting unavailable: '+$_.Exception.Message) -ForegroundColor Yellow
        }
    }
    if ($childExit -ne 0 -or $displayExit -ne 0) { exit 1 }
    exit 0
} catch {
    Write-Host ('[SmartAgent ACL] RESULT: FAIL; launcher error: '+$_.Exception.Message) -ForegroundColor Red
    exit 2
}
