# SmartAgent NLIT offline sidecar. Never touches running production Agent.
# Modes: Preflight (read-only), StartSupervisor (foreground, own terminal),
# RunCase (one NEW request, requires dedicated running supervisor and browser).
[CmdletBinding()]
param(
    [ValidateSet('Preflight','StartSupervisor','RunCase')]
    [string]$Mode = 'Preflight',
    [Parameter(Mandatory)][string]$TestInstall,
    [Parameter(Mandatory)][string]$ProductionInstall,
    [Parameter(Mandatory)][string]$TestWorkspace,
    [Parameter(Mandatory)][string]$ProductionWorkspace,
    [Parameter(Mandatory)][string]$ObserverDir,
    [Parameter(Mandatory)][string]$PrivateDir,
    [Parameter(Mandatory)][string]$TestAppData,
    [Parameter(Mandatory)][string]$ProductionAppData,
    [string]$GitExe = 'git',
    [switch]$PrivateBrowserVerified,
    [string]$CaseFile,
    [string]$NewCaseId
)
$ErrorActionPreference = 'Stop'
function Fail([string]$reason) { throw ("NLIT_PREFLIGHT_BLOCKED:" + $reason) }
function Full([string]$path) { return [IO.Path]::GetFullPath($path).TrimEnd('\','/') }
function Private-Acl-Checked([string]$dir) {
    $acl = Get-Acl -LiteralPath $dir -ErrorAction Stop
    $blocked = @('S-1-1-0','S-1-5-11','S-1-5-32-545','S-1-5-32-546')
    foreach ($entry in $acl.Access) {
        if ($entry.AccessControlType -ne 'Allow') { continue }
        try {
            $sid = $entry.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]).Value
        } catch { Fail 'UNRESOLVED_ACL_PRINCIPAL' }
        if ($sid -in $blocked) { Fail 'PRIVATE_FOLDER_BROAD_ACL' }
    }
    if ($acl.AreAccessRulesProtected -ne $true) {
        Fail 'PRIVATE_ACL_INHERITANCE_NOT_DISABLED'
    }
}
$TestInstall = Full $TestInstall
$ProductionInstall = Full $ProductionInstall
$TestWorkspace = Full $TestWorkspace
$ProductionWorkspace = Full $ProductionWorkspace
$ObserverDir = Full $ObserverDir
$PrivateDir = Full $PrivateDir
$TestAppData = Full $TestAppData
$ProductionAppData = Full $ProductionAppData

if (-not (Test-Path -LiteralPath $PrivateDir -PathType Container)) {
    Fail 'PRIVATE_DIR_MISSING'
}
Private-Acl-Checked $PrivateDir
if ($env:SMARTAGENT_NLIT_CAPTURE_NONCE -notmatch '^[0-9a-fA-F]{32,128}$') {
    Fail 'FRESH_PRIVATE_OPT_IN_NONCE_REQUIRED'
}
$py = Join-Path $TestInstall '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
    Fail 'ISOLATED_PYTHON_NOT_FOUND'
}
# Environment changes apply ONLY to this terminal/child processes.
$env:APPDATA = $TestAppData
$env:LOCALAPPDATA = $TestAppData
$env:SMARTAGENT_NLIT_CAPTURE_ENABLED = '1'
$env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED = 'YES'
$env:SMARTAGENT_NLIT_CAPTURE_ROOT = $PrivateDir
$env:PYTHONPATH = ((Join-Path $TestInstall 'source') + [IO.Path]::PathSeparator + $ObserverDir)
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:SMARTAGENT_TELEGRAM_WORKSPACE = $TestWorkspace
$preflight = Join-Path $ObserverDir 'offline_preflight.py'
$preflightArgs = @($preflight,
    '--test-install', $TestInstall, '--production-install', $ProductionInstall,
    '--test-workspace', $TestWorkspace, '--production-workspace', $ProductionWorkspace,
    '--observer-dir', $ObserverDir, '--private-dir', $PrivateDir,
    '--test-appdata', $TestAppData, '--production-appdata', $ProductionAppData,
    '--git', $GitExe)
& $py @preflightArgs
if ($LASTEXITCODE -ne 0) { Fail 'OFFLINE_PREFLIGHT_FAILED' }
if ($Mode -eq 'Preflight') {
    Write-Host 'NLIT_OFFLINE_PREFLIGHT_PASS: no browser or task launched'
    exit 0
}
if (-not $PrivateBrowserVerified) { Fail 'PRIVATE_BROWSER_LOGIN_NOT_CONFIRMED' }
function Browser-Selection([string]$installation) {
    $cfg = Join-Path $installation 'config\browser_session.json'
    $brand = 'chrome'; $instance = 1
    if (Test-Path -LiteralPath $cfg -PathType Leaf) {
        $data = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
        $brand = [string]$data.browser
        $instance = [int]$data.instance
    }
    if ($brand -notin @('chrome','edge') -or $instance -lt 1 -or $instance -gt 5) {
        Fail 'INVALID_BROWSER_INSTANCE'
    }
    $port = $(if ($brand -eq 'chrome') {12800} else {12810}) + $instance
    return $port
}
$testPort = Browser-Selection $TestInstall
$prodPort = Browser-Selection $ProductionInstall
if ($testPort -eq $prodPort) { Fail 'CDP_PORT_COLLIDES_WITH_PRODUCTION' }
# Network response is ONLY liveness, NOT browser-profile ownership proof.
try {
    $u = "http://127.0.0.1:$testPort/json/version"
    Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 3 | Out-Null
} catch { Fail 'DEDICATED_BROWSER_NOT_AVAILABLE_OR_NOT_LOGGED_IN' }

if ($Mode -eq 'StartSupervisor') {
    Write-Host 'Starting isolated supervisor; dedicated browser ownership must be verified.'
    & (Join-Path $TestInstall 'launch_remote_agent.bat')
    exit $LASTEXITCODE
}
if ($Mode -eq 'RunCase') {
    if (-not $NewCaseId -or -not $CaseFile) { Fail 'NEW_CASE_ID_AND_CASE_FILE_REQUIRED' }
    $case = Full $CaseFile
    if (-not $case.StartsWith($PrivateDir + [IO.Path]::DirectorySeparatorChar,
                             [StringComparison]::OrdinalIgnoreCase)) {
        Fail 'CASE_FILE_OUTSIDE_PRIVATE_DIR'
    }
    & $py (Join-Path $ObserverDir 'submit_private_case.py') (
        @('--test-workspace', $TestWorkspace, '--private-root', $PrivateDir,
          '--case-file', $case, '--case-id', $NewCaseId))
    exit $LASTEXITCODE
}
