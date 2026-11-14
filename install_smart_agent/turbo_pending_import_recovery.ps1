# Git62: explicit, fail-closed recovery of ONE interrupted Turbo import.
# Only a successful Native rollback may remove a pending import journal.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$ProjectRoot,
  [Parameter(Mandatory=$true)][string]$TurboRoot,
  [switch]$Elevated
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p){return [IO.Path]::GetFullPath($p).TrimEnd('\')}
function Same([string]$a,[string]$b){
 return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Assert-NoStateReparse([string]$p){
 $cur=[IO.Path]::GetFullPath($p)
 while($cur){
  if(Test-Path -LiteralPath $cur){
   $item=Get-Item -LiteralPath $cur -Force -ErrorAction Stop
   if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){
    throw ('TURBO_RECOVERY_REPARSE_STATE_PATH:'+$cur)
   }
  }
  $parent=[IO.Path]::GetDirectoryName($cur)
  if(-not $parent -or $parent -eq $cur){break}
  $cur=$parent
 }
}
$project=Full $ProjectRoot
$target=Full $TurboRoot
if(-not ([IO.Path]::IsPathRooted($ProjectRoot) -and [IO.Path]::IsPathRooted($TurboRoot))){
 throw 'TURBO_RECOVERY_ABSOLUTE_PATHS_REQUIRED'
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 if($Elevated){throw 'TURBO_RECOVERY_ADMIN_REQUIRED'}
 # UAC is mandatory; never try to read admin-only journals without elevation.
 $thisScript=Full $PSCommandPath
 $literal={param([string]$v) "'" + $v.Replace("'","''") + "'"}
 $line='& '+(& $literal $thisScript)+' -ProjectRoot '+(& $literal $project)+
   ' -TurboRoot '+(& $literal $target)+' -Elevated; if(-not $?){ exit 2 }'
 $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($line))
 $powershell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
 Write-Host '[SmartAgent Turbo] Administrator approval is required for Native ACL recovery.'
 try{
  $child=Start-Process -FilePath $powershell -ArgumentList @(
   '-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded
  ) -Verb RunAs -Wait -PassThru -ErrorAction Stop
 }catch{
  throw ('TURBO_RECOVERY_ELEVATION_FAILED:'+$_.Exception.Message)
 }
 if([int]$child.ExitCode -ne 0){
  throw ('TURBO_RECOVERY_ELEVATED_EXIT_FAILED:'+ $child.ExitCode)
 }
 Write-Host 'TURBO_RECOVERY_ELEVATED_EXIT_PASS'
 return
}
$sec=Join-Path $project 'localdata\secure\windows_security'
$journalDir=Join-Path $sec 'turbo_root_activations'
$policyPath=Join-Path $sec 'workspace_access_policy.json'
$modePath=Join-Path $sec 'acl_mode.json'
$manifest=Join-Path $project 'config\protocol_manifest.json'
foreach($p in @($project,$sec,$journalDir,$policyPath,$modePath,$manifest)){
 Assert-NoStateReparse $p
}
# Unverified release changes and unreconciled Git upgrades are not allowed
# to perform administrative NTFS recovery.
$python=Join-Path $project '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw 'TURBO_RECOVERY_PYTHON_MISSING'}
$check='import sys;sys.path.insert(0,sys.argv[1]);from agent_core.protocol_manifest import require_protocol_manifest;from agent_core.windows_security import load_machine_authorization;require_protocol_manifest(sys.argv[2]);assert load_machine_authorization() is not None'
& $python -I -c $check (Join-Path $project 'source') $project
if($LASTEXITCODE -ne 0){throw 'TURBO_RECOVERY_SOURCE_OR_MACHINE_AUTHORIZATION_UNVERIFIED'}
$mode=Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
   -not (Same ([string]$mode.install_root) $project)){
 throw 'TURBO_RECOVERY_ACL_ON_REQUIRED'
}
$policy=Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
   $policy.turbo_requested -ne $true){
 throw 'TURBO_RECOVERY_POLICY_UNTRUSTED'
}
. (Join-Path $PSScriptRoot 'turbo_multi_root_paths.ps1')
$paths=Get-TurboRootJournalPaths $sec $target
foreach($p in @($paths.activation,$paths.import)){Assert-NoStateReparse $p}
if(Test-Path -LiteralPath $paths.activation -PathType Leaf){
 throw 'TURBO_RECOVERY_ACTIVATION_ALREADY_EXISTS'
}
if(-not (Test-Path -LiteralPath $paths.import -PathType Leaf)){
 Write-Host ('TURBO_RECOVERY_NOT_REQUIRED root='+$target)
 return
}
$journal=Get-Content -LiteralPath $paths.import -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if($journal.schema -ne 'SMARTAGENT_TURBO_EXISTING_IMPORT_V1' -or
   $journal.status -notin @('APPLYING','REVOKING','ACTIVE') -or
   -not (Same ([string]$journal.install_root) $project) -or
   -not (Same ([string]$journal.turbo_root) $target) -or
   [string]$journal.package_sid -notmatch '^S-1-15-2-' -or
   [string]$journal.snapshot_id -notmatch '^[0-9a-f]{32}$'){
 throw 'TURBO_RECOVERY_JOURNAL_UNTRUSTED'
}
$workspace=Full ([string]$journal.workspace_root)
if(-not $target.StartsWith($workspace+'\',[StringComparison]::OrdinalIgnoreCase) -or
   @($policy.writable_workspaces | Where-Object{Same ([string]$_) $workspace}).Count -ne 1){
 throw 'TURBO_RECOVERY_WORKSPACE_BINDING_UNVERIFIED'
}
$snapshot=Join-Path $sec ('turbo_existing_root_snapshots\snapshot-'+[string]$journal.snapshot_id+'.json')
Assert-NoStateReparse $snapshot
if(-not(Test-Path -LiteralPath $snapshot -PathType Leaf)){throw 'TURBO_RECOVERY_SNAPSHOT_MISSING'}
$original=Get-Content -LiteralPath $snapshot -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if($original.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1' -or
   -not(Same ([string]$original.turbo_root) $target)){
 throw 'TURBO_RECOVERY_SNAPSHOT_UNTRUSTED'
}
Write-Host ('[SmartAgent Turbo] Native Recovery target: '+$target)
Write-Host ('[SmartAgent Turbo] Previous transaction status: '+$journal.status)
Write-Host '[SmartAgent Turbo] Restoring the protected snapshot and removing only this Root SID.'
& (Join-Path $PSScriptRoot 'turbo_existing_root_import.ps1') -Action Recover -ProjectRoot $project -TurboRoot $target
if(-not $?){throw 'TURBO_RECOVERY_NATIVE_REVOKE_FAILED'}
if((Test-Path -LiteralPath $paths.import -PathType Leaf) -or
   (Test-Path -LiteralPath $paths.activation -PathType Leaf)){
 throw 'TURBO_RECOVERY_POSTCONDITION_FAILED'
}
Write-Host ('TURBO_PENDING_IMPORT_RECOVERY_PASS root='+$target)
