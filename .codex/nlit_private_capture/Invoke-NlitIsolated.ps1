# SmartAgent NLIT offline observer launcher (ChatGPT-reviewed).
# Preflight inspects only. StartSupervisor stays foreground. RunCase sends ONE NEW task.
[CmdletBinding()]
param(
 [ValidateSet('Preflight','StartSupervisor','RunCase')][string]$Mode='Preflight',
 [Parameter(Mandatory=$true)][string]$TestInstall,
 [Parameter(Mandatory=$true)][string]$ProductionInstall,
 [Parameter(Mandatory=$true)][string]$TestWorkspace,
 [Parameter(Mandatory=$true)][string]$ProductionWorkspace,
 [Parameter(Mandatory=$true)][string]$ObserverDir,
 [Parameter(Mandatory=$true)][string]$PrivateDir,
 [Parameter(Mandatory=$true)][string]$TestAppData,
 [Parameter(Mandatory=$true)][string]$ProductionAppData,
 [string]$GitExe='git',
 [switch]$PrivateBrowserVerified,
 [string]$CaseFile,
 [string]$NewCaseId,
 [int]$TimeoutSec=900
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Fail([string]$Reason) { throw ("NLIT_PREFLIGHT_BLOCKED:"+$Reason) }
function Full([string]$PathText) {
 if (![IO.Path]::IsPathRooted($PathText)) { Fail 'RELATIVE_PATH' }
 if (!(Test-Path -LiteralPath $PathText -PathType Container)) { Fail 'ROOT_MISSING' }
 return (Resolve-Path -LiteralPath $PathText).ProviderPath.TrimEnd('\','/')
}
function SameOrContained([string]$A,[string]$B) {
 $a1=$A.TrimEnd('\','/')+'\';$b1=$B.TrimEnd('\','/')+'\'
 return ($a1.StartsWith($b1,[StringComparison]::OrdinalIgnoreCase) -or
         $b1.StartsWith($a1,[StringComparison]::OrdinalIgnoreCase))
}
function NoLinks([string]$P) {
 $d=Get-Item -LiteralPath $P -Force
 while ($null -ne $d) {
  if (($d.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail 'REPARSE_POINT' }
  $d=$d.Parent
 }
}
$TestInstall=Full $TestInstall
$ProductionInstall=Full $ProductionInstall
$TestWorkspace=Full $TestWorkspace
$ProductionWorkspace=Full $ProductionWorkspace
$ObserverDir=Full $ObserverDir
$PrivateDir=Full $PrivateDir
$TestAppData=Full $TestAppData
$ProductionAppData=Full $ProductionAppData
foreach ($p in @($TestInstall,$ProductionInstall,$TestWorkspace,$ProductionWorkspace,$ObserverDir,$PrivateDir,$TestAppData,$ProductionAppData)) { NoLinks $p }
foreach ($p in @($TestInstall,$TestWorkspace,$ObserverDir,$PrivateDir,$TestAppData)) {
 foreach ($production in @($ProductionInstall,$ProductionWorkspace,$ProductionAppData)) {
  if (SameOrContained $p $production) { Fail 'PRODUCTION_PATH_OVERLAP' }
 }
}
foreach ($p in @($TestInstall,$TestWorkspace,$ObserverDir,$PrivateDir)) {
 if (SameOrContained $TestAppData $p) { Fail 'APPDATA_TEST_ROOT_OVERLAP' }
}
if ([int]$TimeoutSec -lt 15 -or [int]$TimeoutSec -gt 1800) { Fail 'BAD_TIMEOUT' }
$preflight=Join-Path $ObserverDir 'preflight.ps1'
if (!(Test-Path -LiteralPath $preflight -PathType Leaf)) { Fail 'OFFLINE_PREFLIGHT_SCRIPT_MISSING' }
$phase=if ($Mode -eq 'RunCase') {'AfterStart'} else {'BeforeStart'}
& $preflight -ProductionInstall $ProductionInstall -ProductionWorkspace $ProductionWorkspace -TestInstall $TestInstall -TestWorkspace $TestWorkspace -ObserverDir $ObserverDir -PrivateCaptureRoot $PrivateDir -ArtifactManifest (Join-Path $ObserverDir 'artifact_manifest.json') -GitExe $GitExe -Phase $phase
if (!$?) { Fail 'OFFLINE_PREFLIGHT_FAILED' }
if ($Mode -eq 'Preflight') {
 Write-Output 'NLIT_OFFLINE_PREFLIGHT_PASS: no browser or task launched'
 exit 0
}
$token=([string]$env:SMARTAGENT_NLIT_CAPTURE_NONCE).Trim()
if ($token -notmatch '^[a-fA-F0-9]{32,128}$') { Fail 'PRIVATE_OPT_IN_NONCE_REQUIRED' }
$py=Join-Path $TestInstall '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $py -PathType Leaf)) { Fail 'ISOLATED_PYTHON_MISSING' }
$env:APPDATA=$TestAppData
$env:LOCALAPPDATA=$TestAppData
$env:SMARTAGENT_NLIT_CAPTURE_ENABLED='1'
$env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED='YES'
$env:SMARTAGENT_NLIT_CAPTURE_ROOT=$PrivateDir
$env:SMARTAGENT_TELEGRAM_WORKSPACE=$TestWorkspace
$env:PYTHONPATH=(Join-Path $TestInstall 'source') + [IO.Path]::PathSeparator + $ObserverDir
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
if ($Mode -eq 'StartSupervisor') {
 Write-Output 'NLIT_DEDICATED_SUPERVISOR_STARTING: separate process/browser ownership still required'
 & (Join-Path $TestInstall 'launch_remote_agent.bat')
 exit $LASTEXITCODE
}
if (!$PrivateBrowserVerified) { Fail 'DEDICATED_BROWSER_OWNERSHIP_NOT_CONFIRMED' }
if ($NewCaseId -notmatch '^NLIT-NEW-R2-[A-Z0-9-]{3,80}$') { Fail 'NEW_CASE_ID_REQUIRED' }
if (!$CaseFile -or ![IO.Path]::IsPathRooted($CaseFile)) { Fail 'PRIVATE_CASE_FILE_REQUIRED' }
$case=[IO.Path]::GetFullPath($CaseFile)
$prefix=$PrivateDir.TrimEnd('\','/')+'\'
if (!$case.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { Fail 'CASE_FILE_OUTSIDE_PRIVATE_ROOT' }
if (!(Test-Path -LiteralPath $case -PathType Leaf)) { Fail 'CASE_FILE_MISSING' }
if (((Get-Item -LiteralPath $case).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Fail 'CASE_FILE_LINK_REJECTED' }
$body=(Get-Content -LiteralPath $case -Raw -Encoding UTF8).Trim()
if (!$body -or $body.Length -gt 10000) { Fail 'EMPTY_OR_OVERSIZED_NEW_REQUEST' }
if ($body -match 'NLIT_TRACE_TOKEN=' -or $body -match 'NLIT_CASE_ID=') { Fail 'DOUBLE_TRACE_ID_REJECTED' }
$request='NLIT_TRACE_TOKEN='+$token+[Environment]::NewLine+'NLIT_CASE_ID='+$NewCaseId+[Environment]::NewLine+$body
Push-Location $TestInstall
try {
 & $py -m RemoteAgent.local_telegram_sender --workspace $TestWorkspace --request $request --timeout $TimeoutSec
 exit $LASTEXITCODE
} finally { Pop-Location }
