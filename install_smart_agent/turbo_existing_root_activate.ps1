# Git61: Explicit elevated import of a PRE-EXISTING Turbo Root.
# The target directory and user files are never created/replaced.
# A saved Turbo preference, Telegram message or model tool call is NOT a grant.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][ValidateSet('Activate','Status')][string]$Action,
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [string]$Workspace='',
 [string]$TurboRoot='',
 [string]$ConfirmTarget='',
 [ValidateRange(1,100000)][int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p){
 if([string]::IsNullOrWhiteSpace($p)){throw 'TURBO_GIT61_EMPTY_PATH'}
 return [IO.Path]::GetFullPath($p).TrimEnd('\')
}
function Same([string]$a,[string]$b){
 return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)
}
function Under([string]$a,[string]$b){
 return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)
}
function Read-State([string]$p){
 if(-not (Test-Path -LiteralPath $p -PathType Leaf)){throw ('TURBO_GIT61_STATE_MISSING:'+$p)}
 if((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){
  throw 'TURBO_GIT61_STATE_REPARSE'
 }
 return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
$project=Full $ProjectRoot
$sec=Join-Path $project 'localdata\secure\windows_security'
$activation=Join-Path $sec 'turbo_os_activation.json'
$importJournal=Join-Path $sec 'turbo_existing_root_import.json'
$switchJournal=Join-Path $sec 'turbo_existing_root_switch.json'
if($Action -eq 'Status'){
 Write-Host ('TURBO_GIT61_IMPORT_JOURNAL='+$(if(Test-Path -LiteralPath $importJournal){'PRESENT'}else{'ABSENT'}))
 Write-Host ('TURBO_GIT61_BROKER_JOURNAL='+$(if(Test-Path -LiteralPath $activation){'PRESENT'}else{'ABSENT'}))
 return
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 throw 'TURBO_GIT61_ELEVATED_ADMIN_REQUIRED'
}
$workspace=Full $Workspace
$root=Full $TurboRoot
if(-not (Same $root $ConfirmTarget)){throw 'TURBO_GIT61_EXACT_TARGET_CONFIRMATION_REQUIRED'}
if(-not (Under $root $workspace) -or (Under $root $project) -or
   (Under $project $workspace) -or (Under $workspace $project)){
 throw 'TURBO_GIT61_ROOT_SCOPE_INVALID'
}
if(-not (Test-Path -LiteralPath $root -PathType Container)){
 throw 'TURBO_GIT61_EXISTING_DIRECTORY_REQUIRED'
}
if(Test-Path -LiteralPath $activation){throw 'TURBO_GIT61_EXISTING_BROKER_REQUIRES_VERIFIED_SWITCH'}
if(Test-Path -LiteralPath $importJournal){throw 'TURBO_GIT61_PENDING_IMPORT_REQUIRES_RECOVERY'}
if(Test-Path -LiteralPath $switchJournal){throw 'TURBO_GIT61_PENDING_SWITCH_REQUIRES_RECOVERY'}
$mode=Read-State (Join-Path $sec 'acl_mode.json')
$policy=Read-State (Join-Path $sec 'workspace_access_policy.json')
if($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
   -not (Same $mode.install_root $project)){throw 'TURBO_GIT61_ACL_ON_REQUIRED'}
if($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
   $policy.turbo_requested -ne $true -or @($policy.turbo_roots).Count -ne 1 -or
   -not (Same ([string]$policy.turbo_roots[0]) $root) -or
   @($policy.writable_workspaces | Where-Object {Same ([string]$_) $workspace}).Count -ne 1){
 throw 'TURBO_GIT61_POLICY_OR_WORKSPACE_MISMATCH'
}
# Serialized with the same installation-scoped mutex used by Broker/import.
$sha=[Security.Cryptography.SHA256]::Create()
try{$digest=[BitConverter]::ToString($sha.ComputeHash(
 [Text.Encoding]::UTF8.GetBytes($project.ToUpperInvariant()))).Replace('-','')}
finally{$sha.Dispose()}
$mutex=[Threading.Mutex]::new($false,('Global\SmartAgentTurbo-'+$digest.Substring(0,32)))
$held=$false
try{
 try{$held=$mutex.WaitOne([TimeSpan]::FromSeconds(320))}
 catch [Threading.AbandonedMutexException]{$held=$true}
 if(-not $held){throw 'TURBO_GIT61_BROKER_MUTEX_TIMEOUT'}
 if((Test-Path -LiteralPath $activation) -or (Test-Path -LiteralPath $importJournal) -or
    (Test-Path -LiteralPath $switchJournal)){throw 'TURBO_GIT61_BROKER_STATE_CHANGED'}
 $writer=Join-Path $PSScriptRoot 'turbo_existing_root_snapshot.ps1'
 $verify=Join-Path $PSScriptRoot 'turbo_existing_root_prepare.ps1'
 $import=Join-Path $PSScriptRoot 'turbo_existing_root_import.ps1'
 $receipts=@(& $writer -ProjectRoot $project -Workspace $workspace -TurboRoot $root -MaxEntries $MaxEntries -CandidateOnly -EmitReceipt |
  Where-Object {$_.schema -eq 'SMARTAGENT_TURBO_B_SNAPSHOT_RECEIPT_V1'})
 if($receipts.Count -ne 1 -or $receipts[0].candidate_only -ne $true -or
    $receipts[0].authorization_to_mutate -ne $false -or
    [int]$receipts[0].policy_revision -ne [int]$policy.revision){
  throw 'TURBO_GIT61_SNAPSHOT_RECEIPT_INVALID'
 }
 $id=[string]$receipts[0].snapshot_id
 & $verify -Action Verify -ProjectRoot $project -Workspace $workspace -TurboRoot $root -SnapshotId $id -MaxEntries $MaxEntries
 if(-not $?){throw 'TURBO_GIT61_SNAPSHOT_VERIFY_FAILED'}
 & $import -Action Activate -ProjectRoot $project -Workspace $workspace -TurboRoot $root -SnapshotId $id -MaxEntries $MaxEntries -DirectOperatorConfirmation $ConfirmTarget
 if(-not $?){throw 'TURBO_GIT61_IMPORT_FAILED'}
 $active=Read-State $activation
 $journal=Read-State $importJournal
 if($active.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
    $active.status -ne 'ACTIVE_PROVISIONED' -or
    $active.existing_root_import -ne $true -or
    $active.existing_import_snapshot_id -cne $id -or
    [int]$active.policy_revision -ne [int]$policy.revision -or
    -not (Same $active.turbo_root $root) -or
    $journal.schema -ne 'SMARTAGENT_TURBO_EXISTING_IMPORT_V1' -or
    $journal.status -ne 'ACTIVE'){
  throw 'TURBO_GIT61_NATIVE_IMPORT_POSTCONDITION_FAILED'
 }
 Write-Host 'TURBO_GIT61_EXISTING_ROOT_NATIVE_ENROLLED_PASS'
 Write-Host ('TURBO_GIT61_SNAPSHOT_ID='+$id)
 Write-Host 'TURBO_GIT61_PER_ACTION_OS_ATTESTATION_REQUIRED'
} finally{
 if($held){try{$mutex.ReleaseMutex()}catch{}}
 $mutex.Dispose()
}
