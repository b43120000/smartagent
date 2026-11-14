# Git62: admin-only, manifest-only upgrade of an already enrolled multi-root
# Native AppContainer installation. Called ONLY after ACLstatus has verified
# the Git protocol manifest and the installed Executor ownership.
# Never recreates AppContainer SIDs, changes user workspace DACLs, or
# rewrites a Turbo Root. An incomplete or mixed enrollment fails closed.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProjectRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 throw 'GIT62_UPGRADE_ADMIN_REQUIRED'
}
$project=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$sec=Join-Path $project 'localdata\secure\windows_security'
$manifestPath=Join-Path $project 'config\protocol_manifest.json'
$modePath=Join-Path $sec 'acl_mode.json'
$policyPath=Join-Path $sec 'workspace_access_policy.json'
$profilePath=Join-Path $sec 'security_profile.json'
$activations=Join-Path $sec 'turbo_root_activations'
if([string]::IsNullOrWhiteSpace([string]$env:ProgramData)){
 throw 'GIT62_UPGRADE_PROGRAMDATA_MISSING'
}
$machineDir=Join-Path ([IO.Path]::GetFullPath($env:ProgramData)) 'SmartAgent'
$machinePath=Join-Path $machineDir 'machine_authorization.json'
function Assert-RealPath([string]$path) {
 $cursor=[IO.Path]::GetFullPath($path)
 while($cursor) {
  if(Test-Path -LiteralPath $cursor) {
   $obj=Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
   if(($obj.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw ('GIT62_UPGRADE_REPARSE_PATH:'+$cursor)
   }
  }
  $next=[IO.Path]::GetDirectoryName($cursor)
  if(-not $next -or $next -eq $cursor){break}
  $cursor=$next
 }
}
function Read-VerifiedJson([string]$path) {
 Assert-RealPath $path
 return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop |
  ConvertFrom-Json -ErrorAction Stop)
}
function SamePath([string]$left,[string]$right) {
 return [IO.Path]::GetFullPath($left).TrimEnd('\').Equals(
  [IO.Path]::GetFullPath($right).TrimEnd('\'),
  [StringComparison]::OrdinalIgnoreCase)
}
foreach($path in @($manifestPath,$modePath,$policyPath,$profilePath,$machinePath,$activations)){
 Assert-RealPath $path
}
$mode=Read-VerifiedJson $modePath
$policy=Read-VerifiedJson $policyPath
$profile=Read-VerifiedJson $profilePath
$machine=Read-VerifiedJson $machinePath
$manifest=Read-VerifiedJson $manifestPath
if($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
   -not (SamePath ([string]$mode.install_root) $project) -or
   $policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
   $policy.turbo_requested -ne $true -or [int]$policy.revision -lt 1){
 throw 'GIT62_UPGRADE_ACL_POLICY_NOT_TRUSTED'
}
if($profile.schema -ne 'SMARTAGENT_WINDOWS_SECURITY_V2' -or
   $machine.schema -ne 'SMARTAGENT_MACHINE_AUTHORIZATION_V1' -or
   -not (SamePath ([string]$machine.install_root) $project) -or
   -not (SamePath ([string]$machine.security_profile) $profilePath) -or
   [string]$machine.executor_sid -ne [string]$profile.executor_sid -or
   [string]$machine.executor_user -ne [string]$profile.executor_user -or
   [string]$machine.executor_task_name -ne [string]$profile.executor_task_name -or
   -not (SamePath ([string]$machine.executor_endpoint) ([string]$profile.executor_endpoint)) -or
   $machine.protocol_family -ne $manifest.protocol_family -or
   [int]$machine.protocol_version -ne [int]$manifest.protocol_version){
 throw 'GIT62_UPGRADE_MACHINE_OWNER_NOT_TRUSTED'
}
$recordedHash=[string]$machine.protocol_manifest_sha256
if($recordedHash -notmatch '^[0-9a-fA-F]{64}$'){
 throw 'GIT62_UPGRADE_MACHINE_DIGEST_INVALID'
}
# The protected updater remains pinned; manifest-only migration is forbidden
# if updated updater code would require separate security reprovisioning.
$updater=Join-Path $project 'install_smart_agent\update.ps1'
$updaterPolicy=Join-Path $project 'install_smart_agent\security_acl_policy.ps1'
if([string]$machine.updater_sha256 -ne (Get-FileHash -LiteralPath $updater -Algorithm SHA256).Hash.ToLowerInvariant() -or
   [string]$machine.updater_policy_sha256 -ne (Get-FileHash -LiteralPath $updaterPolicy -Algorithm SHA256).Hash.ToLowerInvariant() -or
   -not (Test-Path -LiteralPath ([string]$machine.updater_path) -PathType Leaf) -or
   [string]$machine.updater_sha256 -ne (Get-FileHash -LiteralPath ([string]$machine.updater_path) -Algorithm SHA256).Hash.ToLowerInvariant()){
 throw 'GIT62_UPGRADE_PINNED_UPDATER_CHANGED'
}
$originalAcl=Get-Acl -LiteralPath $machinePath -ErrorAction Stop
if(-not $originalAcl.AreAccessRulesProtected){
 throw 'GIT62_UPGRADE_MACHINE_AUTHORIZATION_ACL_UNPROTECTED'
}
$roots=@($policy.turbo_roots)
if($roots.Count -lt 2){throw 'GIT62_UPGRADE_MULTI_ROOT_REQUIRED'}
if(-not (Test-Path -LiteralPath $activations -PathType Container)){
 throw 'GIT62_UPGRADE_ACTIVATIONS_MISSING'
}
if(Test-Path -LiteralPath (Join-Path $sec 'turbo_os_activation.json')){
 throw 'GIT62_UPGRADE_LEGACY_ACTIVATION_CONFLICT'
}
foreach($pending in @('turbo_existing_root_switch.json',
                       'turbo_existing_root_import.json')){
 if(Test-Path -LiteralPath (Join-Path $sec $pending)){
  throw ('GIT62_UPGRADE_PENDING_TRANSACTION:'+$pending)
 }
}
$files=@(Get-ChildItem -LiteralPath $activations -Filter 'activation-*.json' -File -ErrorAction Stop)
if($files.Count -ne $roots.Count){throw 'GIT62_UPGRADE_PARTIAL_ENROLLMENT'}
. (Join-Path $PSScriptRoot 'turbo_multi_root_paths.ps1')
$rootNames=@{}
$packageSids=@{}
foreach($root in $roots){
 $full=[IO.Path]::GetFullPath([string]$root).TrimEnd('\')
 $key=$full.ToUpperInvariant()
 if($rootNames.ContainsKey($key)){throw 'GIT62_UPGRADE_DUPLICATE_ROOT'}
 $rootNames[$key]=$true
 if(-not (Test-Path -LiteralPath $full -PathType Container)){
  throw 'GIT62_UPGRADE_ROOT_UNAVAILABLE'
 }
 $journal=Get-TurboRootJournalPaths $sec $full
 if(-not (Test-Path -LiteralPath $journal.activation -PathType Leaf)){
  throw ('GIT62_UPGRADE_ROOT_JOURNAL_MISSING:'+$full)
 }
 $record=Read-VerifiedJson $journal.activation
 if($record.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
    $record.status -ne 'ACTIVE_PROVISIONED' -or
    -not (SamePath ([string]$record.turbo_root) $full) -or
    -not (SamePath ([string]$record.install_root) $project) -or
    [string]$record.package_sid -notmatch '^S-1-15-2-' -or
    $packageSids.ContainsKey([string]$record.package_sid)){
  throw 'GIT62_UPGRADE_ROOT_IDENTITY_INVALID'
 }
 $packageSids[[string]$record.package_sid]=$true
}
# Each protected journal is independently OS-attested by launching its
# AppContainer and verifying native token + kernel negative read.
# No pointer is published until every Root is verified.
& (Join-Path $PSScriptRoot 'turbo_multi_root_rebind.ps1') -ProjectRoot $project
$manifestHash=(Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if($recordedHash.ToLowerInvariant() -eq $manifestHash){
 Write-Host ('GIT62_MULTI_ROOT_MACHINE_AUTHORIZATION_PASS roots='+$roots.Count+' changed=false')
 return
}
$machine.protocol_manifest_sha256=$manifestHash
$machine.updated_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$tmp=Join-Path $machineDir ('machine_authorization.git62-'+[Guid]::NewGuid().ToString('N')+'.tmp')
try {
 # CREATE_NEW rejects an existing attacker-supplied link. The tmp is
 # protected BEFORE it replaces the previous admin-owned machine pointer.
 $bytes=[Text.Encoding]::UTF8.GetBytes(($machine | ConvertTo-Json -Depth 16))
 $stream=[IO.FileStream]::new($tmp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
 try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}
 finally {$stream.Dispose()}
 & icacls.exe $tmp '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' | Out-Null
 if($LASTEXITCODE -ne 0){throw 'GIT62_UPGRADE_MACHINE_ACL_UPDATE_FAILED'}
 # Recheck that the original identity has not changed while attesting Roots.
 $current=Read-VerifiedJson $machinePath
 if([string]$current.protocol_manifest_sha256 -ne $recordedHash -or
    -not (SamePath ([string]$current.install_root) $project) -or
    [string]$current.executor_sid -ne [string]$machine.executor_sid){
   throw 'GIT62_UPGRADE_MACHINE_AUTHORIZATION_CHANGED'
 }
 Move-Item -LiteralPath $tmp -Destination $machinePath -Force -ErrorAction Stop
 $published=Read-VerifiedJson $machinePath
 if([string]$published.protocol_manifest_sha256 -ne $manifestHash -or
    -not (Get-Acl -LiteralPath $machinePath).AreAccessRulesProtected){
   throw 'GIT62_UPGRADE_MACHINE_POSTCONDITION_FAILED'
 }
 Write-Host ('GIT62_MULTI_ROOT_MACHINE_AUTHORIZATION_PASS roots='+$roots.Count+' changed=true')
} finally {
 if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
}
