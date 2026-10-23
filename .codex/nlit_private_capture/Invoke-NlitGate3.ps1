#requires -Version 5.1
# SA-NLIT Gate3, TEST-only control shell. Do not run from original Agent console.
[CmdletBinding()]
param(
 [ValidateSet('Inspect','OpenBrowser','Bind','CheckBinding','StartSupervisor','SendCase')]
 [string]$Mode='Inspect',
 [Parameter(Mandatory=$true)][string]$TestInstall,
 [Parameter(Mandatory=$true)][string]$ProductionInstall,
 [Parameter(Mandatory=$true)][string]$TestWorkspace,
 [Parameter(Mandatory=$true)][string]$ProductionWorkspace,
 [Parameter(Mandatory=$true)][string]$ObserverDir,
 [Parameter(Mandatory=$true)][string]$PrivateRoot,
 [Parameter(Mandatory=$true)][string]$TestAppData,
 [Parameter(Mandatory=$true)][string]$ProductionAppData,
 [string]$GitExe='git',
 [switch]$ManualLoginVerified,
 [string]$CaseFile,
 [string]$NewCaseId,
 [int]$TimeoutSec=900
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Block([string]$Reason) { throw ('NLIT_GATE3_BLOCKED:'+$Reason) }
function Directory([string]$PathText) {
 if (-not [IO.Path]::IsPathRooted($PathText)) { Block 'RELATIVE_DIRECTORY' }
 if (!(Test-Path -LiteralPath $PathText -PathType Container)) { Block 'DIRECTORY_MISSING' }
 return (Resolve-Path -LiteralPath $PathText).ProviderPath.TrimEnd('\','/')
}
function Overlap([string]$A,[string]$B) {
 $a1=$A.TrimEnd('\','/')+'\'; $b1=$B.TrimEnd('\','/')+'\'
 return $a1.StartsWith($b1,[StringComparison]::OrdinalIgnoreCase) -or $b1.StartsWith($a1,[StringComparison]::OrdinalIgnoreCase)
}
function NoReparse([string]$PathText) {
 [IO.FileSystemInfo]$node=Get-Item -LiteralPath $PathText -Force
 while ($null -ne $node) {
  if (($node.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Block 'REPARSE_PATH' }
  if ($node -is [IO.FileInfo]) { $node=$node.Directory }
  elseif ($node -is [IO.DirectoryInfo]) { $node=$node.Parent }
  else { Block 'UNEXPECTED_FILESYSTEM_NODE' }
 }
}
$TestInstall=Directory $TestInstall; $ProductionInstall=Directory $ProductionInstall
$TestWorkspace=Directory $TestWorkspace; $ProductionWorkspace=Directory $ProductionWorkspace
$ObserverDir=Directory $ObserverDir; $PrivateRoot=Directory $PrivateRoot
$TestAppData=Directory $TestAppData; $ProductionAppData=Directory $ProductionAppData
foreach($p in @($TestInstall,$ProductionInstall,$TestWorkspace,$ProductionWorkspace,$ObserverDir,$PrivateRoot,$TestAppData,$ProductionAppData)) { NoReparse $p }
foreach($test in @($TestInstall,$TestWorkspace,$ObserverDir,$PrivateRoot,$TestAppData)) {
 foreach($live in @($ProductionInstall,$ProductionWorkspace,$ProductionAppData)) {
  if (Overlap $test $live) { Block 'TEST_LIVE_ROOT_OVERLAP' }
 }
}
foreach($pair in @(@($TestInstall,$TestWorkspace),@($TestInstall,$ObserverDir),@($TestWorkspace,$ObserverDir))) {
 if (Overlap $pair[0] $pair[1]) { Block 'TEST_ROOTS_OVERLAP' }
}
$Py=Join-Path $TestInstall '.venv\Scripts\python.exe'
$Helper=Join-Path $ObserverDir 'nlit_gate3.py'
$Adapter=Join-Path $ObserverDir 'Enable-NlitIsolatedPython-v2.ps1'
$FiveManifest=Join-Path $ObserverDir 'artifact_manifest.json'
$V2Manifest=Join-Path $ObserverDir 'adapter_manifest.json'
$NonceFile=Join-Path $PrivateRoot 'gate3_capture_nonce.txt'
$Profile=Join-Path $TestAppData 'WebLLMScraper\edge_5'
$Launcher=Join-Path $TestInstall 'launch_remote_agent.bat'
foreach($f in @($Py,$Helper,$Adapter,$FiveManifest,$V2Manifest,$Launcher)) {
 if (!(Test-Path -LiteralPath $f -PathType Leaf)) { Block 'REQUIRED_FILE_MISSING' }
 NoReparse $f
}
$pin=Get-Content -LiteralPath $V2Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
if ($pin.schema -ne 'SMARTAGENT_NLIT_BUNDLED_PYTHON_ADAPTER_ARTIFACT_V2') { Block 'V2_MANIFEST_NOT_STAGED' }
if ((Get-FileHash -LiteralPath $Adapter -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.artifact.sha256 -or
 (Get-Item -LiteralPath $Adapter).Length -ne [long]$pin.artifact.bytes) { Block 'V2_ADAPTER_PIN_MISMATCH' }
$back=Join-Path $PrivateRoot 'python-pth-backup'
$state=Join-Path $back 'nlit_python313._pth.state.json'
if (!(Test-Path -LiteralPath $state -PathType Leaf)) { Block 'V2_ADAPTER_NOT_ENABLED' }
& $Adapter -Mode Verify -TestInstall $TestInstall -ProductionInstall $ProductionInstall -ObserverDir $ObserverDir -BackupDir $back | Out-Null
if (!$?) { Block 'V2_ADAPTER_VERIFY_FAILED' }
$vars=@('APPDATA','LOCALAPPDATA','TEMP','TMP','PYTHONPATH','SMARTAGENT_BROWSER_MODE',
 'SMARTAGENT_SHARED_BROWSER_CDP','SMARTAGENT_ATTACH_CDP','SMARTAGENT_CHATGPT_CDP',
 'SMARTAGENT_CHROME_USER_DATA_DIR','SMARTAGENT_ISOLATED_BROWSER',
 'SMARTAGENT_REMOTE_BINDING_SNAPSHOT','SMARTAGENT_TELEGRAM_BOT_TOKEN',
 'SMARTAGENT_TELEGRAM_ENABLED','SMARTAGENT_TELEGRAM_PAIRING_ENABLED',
 'SMARTAGENT_TELEGRAM_RECEIVER_DISABLED','SMARTAGENT_NLIT_CAPTURE_ENABLED',
 'SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED','SMARTAGENT_NLIT_CAPTURE_ROOT',
 'SMARTAGENT_NLIT_CAPTURE_NONCE','SMARTAGENT_REMOTE_WORKER','SMARTAGENT_TELEGRAM_WORKSPACE',
 'SMARTAGENT_REMOTE_EXECUTION_URL')
$previous=@{}
foreach($key in $vars) { $previous[$key]=[Environment]::GetEnvironmentVariable($key,'Process') }
$tempDir=Join-Path $PrivateRoot 'temp'
if (!(Test-Path -LiteralPath $tempDir)) { New-Item -ItemType Directory -Path $tempDir -ErrorAction Stop | Out-Null }
NoReparse $tempDir
$env:APPDATA=$TestAppData; $env:LOCALAPPDATA=$TestAppData
$env:TEMP=$tempDir; $env:TMP=$tempDir
$env:PYTHONPATH=Join-Path $TestInstall 'source'
$env:SMARTAGENT_BROWSER_MODE='legacy'
$env:SMARTAGENT_TELEGRAM_ENABLED='0'
$env:SMARTAGENT_TELEGRAM_PAIRING_ENABLED='0'
$env:SMARTAGENT_TELEGRAM_RECEIVER_DISABLED='1'
$env:SMARTAGENT_REMOTE_WORKER='0'
foreach($key in @('SMARTAGENT_SHARED_BROWSER_CDP','SMARTAGENT_ATTACH_CDP','SMARTAGENT_CHATGPT_CDP',
 'SMARTAGENT_CHROME_USER_DATA_DIR','SMARTAGENT_ISOLATED_BROWSER','SMARTAGENT_REMOTE_BINDING_SNAPSHOT',
 'SMARTAGENT_TELEGRAM_BOT_TOKEN','SMARTAGENT_TELEGRAM_WORKSPACE','SMARTAGENT_REMOTE_EXECUTION_URL')) {
 [Environment]::SetEnvironmentVariable($key,$null,'Process')
}
$pyArgs=@('--test-install',$TestInstall,'--production-install',$ProductionInstall,
 '--test-workspace',$TestWorkspace,'--production-workspace',$ProductionWorkspace,
 '--observer-dir',$ObserverDir,'--private-root',$PrivateRoot,
 '--test-appdata',$TestAppData,'--production-appdata',$ProductionAppData)
function PythonGate([string]$Operation) {
 & $Py $Helper $Operation @pyArgs
 if ($LASTEXITCODE -ne 0) { Block ('PYTHON_GATE_'+$Operation+'_FAILED') }
}
function EdgeOwned([bool]$RequireRunning) {
 if (!(Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { Block 'PORT_PROBE_UNAVAILABLE' }
 $listeners=@(Get-NetTCPConnection -State Listen -LocalPort 12815 -ErrorAction SilentlyContinue)
 if ($listeners.Count -eq 0) {
  if ($RequireRunning) { Block 'TEST_EDGE5_NOT_RUNNING' }
  return $false
 }
 $owners=@($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
 if ($owners.Count -ne 1 -or [int]$owners[0] -le 0) { Block 'EDGE5_MULTIPLE_OWNERS' }
 $entry=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$owners[0]) -ErrorAction Stop
 if ($null -eq $entry) { Block 'EDGE5_OWNER_MISSING' }
 $cmd=[string]$entry.CommandLine
 if ($entry.Name -ine 'msedge.exe' -or
     $cmd.IndexOf($Profile,[StringComparison]::OrdinalIgnoreCase) -lt 0 -or
     $cmd.IndexOf('--remote-debugging-port=12815',[StringComparison]::OrdinalIgnoreCase) -lt 0) {
  Block 'FOREIGN_CDP_PROFILE_OR_PROCESS'
 }
 foreach($item in $listeners) {
  if ($item.LocalAddress -notin @('127.0.0.1','::1')) { Block 'NON_LOOPBACK_CDP_BIND' }
 }
 return $true
}
function Preflight {
 $phase=if(EdgeOwned $false) {'AfterStart'} else {'BeforeStart'}
 $kw=@{ ProductionInstall=$ProductionInstall; ProductionWorkspace=$ProductionWorkspace
  TestInstall=$TestInstall; TestWorkspace=$TestWorkspace; ObserverDir=$ObserverDir
  PrivateCaptureRoot=$PrivateRoot; ArtifactManifest=$FiveManifest
  GitExe=$GitExe; Phase=$phase }
 & (Join-Path $ObserverDir 'preflight.ps1') @kw
 if (!$?) { Block 'PREFLIGHT_FAILED' }
}
try {
 if ($Mode -eq 'Inspect') {
  Preflight
  Write-Output 'NLIT_GATE3_INSPECT_PASS; NO_BROWSER_OR_TASK_STARTED'
  return
 }
 if ($Mode -eq 'OpenBrowser') {
  Preflight
  PythonGate 'open-browser'
  if (!(EdgeOwned $true)) { Block 'BROWSER_OWNERSHIP_FAILED' }
  Write-Output 'NLIT_GATE3_TEST_EDGE5_OPEN; LOGIN_NOT_ATTESTED'
  return
 }
 if ($Mode -eq 'Bind') {
  if (!(EdgeOwned $true)) { Block 'TEST_BROWSER_REQUIRED' }
  Preflight
  PythonGate 'bind'
  Write-Output 'NLIT_GATE3_BINDING_SAVED_TEST_ONLY'
  return
 }
 if ($Mode -eq 'CheckBinding') {
  if (!(EdgeOwned $true)) { Block 'TEST_BROWSER_REQUIRED' }
  Preflight
  PythonGate 'verify'
  Write-Output 'NLIT_GATE3_BINDING_AND_BROWSER_OWNER_PASS'
  return
 }
 if (!$ManualLoginVerified) { Block 'HUMAN_LOGIN_CONFIRMATION_REQUIRED' }
 if (!(EdgeOwned $true)) { Block 'BROWSER_NOT_OWNED_BY_TEST' }
 Preflight
 PythonGate 'verify'
 $env:SMARTAGENT_NLIT_CAPTURE_ENABLED='1'
 $env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED='YES'
 $env:SMARTAGENT_NLIT_CAPTURE_ROOT=$PrivateRoot
 if ($Mode -eq 'StartSupervisor') {
  if (Test-Path -LiteralPath $NonceFile -PathType Leaf) { Block 'NONCE_ALREADY_EXISTS_REFUSE_RESTART' }
  $statusFile=Join-Path $TestInstall 'localdata\runtime\remote_supervisor_state.json'
  if (Test-Path -LiteralPath $statusFile -PathType Leaf) {
   $status=Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
   if ($status.status -eq 'RUNNING') { Block 'TEST_SUPERVISOR_ALREADY_RUNNING' }
  }
  $nonce=[guid]::NewGuid().ToString('N')
  $stream=[IO.File]::Open($NonceFile,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  try {
   $bytes=[Text.Encoding]::ASCII.GetBytes($nonce)
   $stream.Write($bytes,0,$bytes.Length)
   $stream.Flush($true)
  } finally { $stream.Dispose() }
  $env:SMARTAGENT_NLIT_CAPTURE_NONCE=$nonce
  Write-Output 'NLIT_GATE3_STARTING_OFFICIAL_TEST_LAUNCHER_FOREGROUND'
  & $Launcher
  if ($LASTEXITCODE -ne 0) { Block 'SUPERVISOR_LAUNCH_FAILED' }
  return
 }
 if ($Mode -eq 'SendCase') {
  if (!(Test-Path -LiteralPath $NonceFile -PathType Leaf)) { Block 'NONCE_NOT_PREPARED' }
  if (!$NewCaseId -or !$CaseFile) { Block 'NEW_CASE_ID_AND_PRIVATE_FILE_REQUIRED' }
  if ($TimeoutSec -lt 60 -or $TimeoutSec -gt 1800) { Block 'INVALID_TIMEOUT' }
  & $Py $Helper send @pyArgs --case-id $NewCaseId --case-file $CaseFile --timeout $TimeoutSec
  if ($LASTEXITCODE -ne 0) { Block 'NEW_CASE_NON_SUCCESS_PRESERVE_LOCAL_EVIDENCE' }
  return
 }
 Block 'MODE_INVALID'
}
finally {
 foreach($key in $vars) { [Environment]::SetEnvironmentVariable($key,$previous[$key],'Process') }
}
