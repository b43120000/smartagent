# Git52 P0: explicitly elevated operator setup, never invoked by Planner.
# No native command fallback, no outside-Turbo write grant to AppContainer.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [Parameter(Mandatory=$true)][string]$ExpectedCommit
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($ExpectedCommit -notmatch '^[a-fA-F0-9]{40}$') {throw 'GIT52_PREPARE_EXPECTED_COMMIT_INVALID'}
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
 throw 'GIT52_PREPARE_REQUIRES_ELEVATED_ADMINISTRATOR'
}
$ProjectRoot=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd([char]92)
$secure=Join-Path $ProjectRoot 'localdata\secure\windows_security'
$profilePath=Join-Path $secure 'security_profile.json'
$activationPath=Join-Path $secure 'turbo_os_activation.json'
$modePath=Join-Path $secure 'acl_mode.json'
$policyPath=Join-Path $secure 'workspace_access_policy.json'
$code=Join-Path $secure 'turbo_broker_code'
$venv=Join-Path $ProjectRoot '.venv'
$git=Join-Path $venv 'git\cmd\git.exe'
$python=Join-Path $venv 'Scripts\python.exe'
$toolPaths=@($python,$git)
$sourcePaths=@(
 (Join-Path $ProjectRoot 'install_smart_agent\turbo_os_execute.ps1'),
 (Join-Path $ProjectRoot 'install_smart_agent\turbo_appcontainer_probe.cs')
)
$targetPaths=@(
 (Join-Path $code 'turbo_os_execute.ps1'),
 (Join-Path $code 'turbo_appcontainer_probe.cs')
)
function Load([string]$p) {
 return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
function No-Reparse([string]$p) {
 if (-not (Test-Path -LiteralPath $p)) {throw 'GIT52_PREPARE_REQUIRED_FILE_MISSING'}
 if ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
  throw 'GIT52_PREPARE_REPARSE_REJECTED'
 }
}
function Save-InPlace([string]$p,$obj) {
 [IO.File]::WriteAllText($p,(ConvertTo-Json -InputObject $obj -Depth 14),
   (New-Object Text.UTF8Encoding($false)))
}
foreach($p in @($ProjectRoot,$secure,$code,$venv) + $toolPaths + $sourcePaths + $targetPaths +
    @($profilePath,$activationPath,$modePath,$policyPath)) {No-Reparse $p}
$head=(& $git -C $ProjectRoot rev-parse HEAD 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or
    -not $head.Equals($ExpectedCommit,[StringComparison]::OrdinalIgnoreCase)) {
 throw 'GIT52_PREPARE_GIT_HEAD_MISMATCH'
}
$dirty=@(& $git -C $ProjectRoot status --porcelain -- $sourcePaths)
if ($LASTEXITCODE -ne 0 -or $dirty.Count -gt 0) {
 throw 'GIT52_PREPARE_SOURCE_MODIFIED'
}
$sha=[Security.Cryptography.SHA256]::Create()
try {
 $hash=[BitConverter]::ToString(
   $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToUpperInvariant()))
 ).Replace('-','')
} finally {$sha.Dispose()}
$mutex=New-Object Threading.Mutex($false,('Global\SmartAgentTurbo-'+$hash.Substring(0,32)))
$held=$false
try {
 try {$held=$mutex.WaitOne([TimeSpan]::FromSeconds(305))}
 catch [Threading.AbandonedMutexException] {$held=$true}
 if (-not $held) {throw 'GIT52_PREPARE_MUTEX_TIMEOUT'}
 if (Test-Path -LiteralPath (Join-Path $secure 'turbo_existing_root_switch.json')) {
  throw 'GIT52_PREPARE_SWITCH_PENDING'
 }
 $mode=Load $modePath
 $policy=Load $policyPath
 $activation=Load $activationPath
 $profile=Load $profilePath
 if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
     $policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
     $policy.turbo_requested -ne $true -or
     $activation.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
     $activation.status -ne 'ACTIVE_PROVISIONED' -or
     [int]$activation.policy_revision -ne [int]$policy.revision -or
     $profile.schema -notin @('SMARTAGENT_WINDOWS_SECURITY_V1','SMARTAGENT_WINDOWS_SECURITY_V2') -or
     -not [IO.Path]::GetFullPath([string]$activation.install_root).TrimEnd([char]92).Equals(
      $ProjectRoot,[StringComparison]::OrdinalIgnoreCase) -or
     -not [IO.Path]::GetFullPath([string]$mode.install_root).TrimEnd([char]92).Equals(
      $ProjectRoot,[StringComparison]::OrdinalIgnoreCase)) {
  throw 'GIT52_PREPARE_POLICY_OR_ACTIVATION_MISMATCH'
 }
 if (-not [IO.Path]::GetFullPath([string]$activation.broker_entry).Equals(
     $targetPaths[0],[StringComparison]::OrdinalIgnoreCase)) {
  throw 'GIT52_PREPARE_BROKER_ENTRY_MISMATCH'
 }
 $sid=[string]$activation.package_sid
 if ($sid -notmatch '^S-1-15-2-[0-9-]+$') {throw 'GIT52_PREPARE_SID_INVALID'}
 $newPins=New-Object 'System.Collections.Generic.List[object]'
 foreach($old in @($profile.trusted_executables)) {
  if ($null -eq $old -or -not $old.path) {continue}
  $same=@($toolPaths | Where-Object {
    [IO.Path]::GetFullPath($_).Equals([IO.Path]::GetFullPath([string]$old.path),
       [StringComparison]::OrdinalIgnoreCase)
  }).Count -gt 0
  if (-not $same) {$newPins.Add($old)}
 }
 foreach($p in $toolPaths) {
  $newPins.Add([ordered]@{
   path=[IO.Path]::GetFullPath($p)
   sha256=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()
   kind='TURBO_NATIVE_TOOLCHAIN'
  })
 }
 if ($profile.PSObject.Properties['trusted_executables']) {
  $profile.trusted_executables=@($newPins.ToArray())
 } else {
  $profile | Add-Member -NotePropertyName trusted_executables -NotePropertyValue @($newPins.ToArray())
 }
 $files=@($profilePath,$targetPaths[0],$targetPaths[1],$activationPath)
 $backup=@($files | ForEach-Object {,[IO.File]::ReadAllBytes($_)})
 try {
  # Only package RX on the installation root and its private .venv.
  # No write/delete rights outside the Turbo Root.
  & icacls.exe $ProjectRoot '/grant' ('*'+$sid+':(RX)') | Out-Null
  if ($LASTEXITCODE -ne 0) {throw 'GIT52_PREPARE_ROOT_RX_FAILED'}
  & icacls.exe $venv '/grant' ('*'+$sid+':(OI)(CI)RX') | Out-Null
  if ($LASTEXITCODE -ne 0) {throw 'GIT52_PREPARE_VENV_RX_FAILED'}
  Save-InPlace $profilePath $profile
  [IO.File]::WriteAllBytes($targetPaths[0],[IO.File]::ReadAllBytes($sourcePaths[0]))
  [IO.File]::WriteAllBytes($targetPaths[1],[IO.File]::ReadAllBytes($sourcePaths[1]))
  $activation.broker_source_sha256=(Get-FileHash -LiteralPath $targetPaths[0] -Algorithm SHA256).Hash
  Save-InPlace $activationPath $activation
  for($i=0;$i -lt 2;$i++) {
   if ((Get-FileHash -LiteralPath $sourcePaths[$i] -Algorithm SHA256).Hash -ne
       (Get-FileHash -LiteralPath $targetPaths[$i] -Algorithm SHA256).Hash) {
    throw 'GIT52_PREPARE_PROTECTED_COPY_MISMATCH'
   }
  }
  [ordered]@{
    schema='SMARTAGENT_GIT52_NATIVE_TOOLCHAIN_PREPARE_V1'
    status='OPERATOR_PROVISIONED'
    git_pinned=$true
    python_pinned=$true
    package_rx_only_outside_turbo=$true
    expected_commit=$ExpectedCommit
    broker_sha256=[string]$activation.broker_source_sha256
    base_python_dependencies='LIVE_VERIFICATION_REQUIRED'
    native_execution='LIVE_VERIFICATION_REQUIRED'
    os_network='LIVE_VERIFICATION_REQUIRED'
  } | ConvertTo-Json -Compress
 } catch {
  for($i=0;$i -lt $files.Count;$i++) {
   try {[IO.File]::WriteAllBytes($files[$i],[byte[]]$backup[$i])} catch {}
  }
  throw
 }
} finally {
 if ($held) {try {$mutex.ReleaseMutex()}catch {}}
 $mutex.Dispose()
}
