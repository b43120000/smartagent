# Git61 disposable Windows NTFS end-to-end: existing project -> native Turbo.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or -not $env:RUNNER_TEMP){
 throw 'TURBO_GIT61_CI_ONLY'
}
function Check([bool]$condition,[string]$code){if(-not $condition){throw $code}}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 throw 'TURBO_GIT61_CI_ADMIN_REQUIRED'
}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('TurboGit61-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$ws=Join-Path $fixture 'workspace'
$root=Join-Path $ws 'existing_project'
$sub=Join-Path $root 'source'
$file=Join-Path $sub 'keep.txt'
$secure=Join-Path $project 'localdata\secure\windows_security'
$script=Join-Path $PSScriptRoot '..\turbo_existing_root_activate.ps1'
$import=Join-Path $PSScriptRoot '..\turbo_existing_root_import.ps1'
$native=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
$activePath=Join-Path $secure 'turbo_os_activation.json'
$importPath=Join-Path $secure 'turbo_existing_root_import.json'
$baseline=@{}
try{
 New-Item -ItemType Directory -Force -Path $secure,$sub | Out-Null
 [IO.File]::WriteAllText($file,'GIT61_USER_BYTES_NEVER_CHANGE')
 $hash=(Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
 foreach($p in @($ws,$root,$sub,$file)){
  $baseline[$p]=(Get-Acl -LiteralPath $p -Audit -ErrorAction Stop).Sddl
 }
 $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
 $policy=[ordered]@{
  schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1';revision=5
  writable_workspaces=@($ws);read_only_roots=@();denied_write_roots=@()
  turbo_roots=@($root);turbo_requested=$true
 }
 [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),($mode | ConvertTo-Json -Depth 8))
 [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),($policy | ConvertTo-Json -Depth 8))
 $bad=$false
 try{
  & $script -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $root -ConfirmTarget $ws | Out-Null
 } catch{$bad=$_.Exception.Message -match 'TURBO_GIT61_EXACT_TARGET_CONFIRMATION_REQUIRED'}
 Check $bad 'TURBO_GIT61_BAD_OPERATOR_CONFIRMATION_ACCEPTED'
 Check (-not (Test-Path -LiteralPath $activePath)) 'TURBO_GIT61_BAD_CONFIRMATION_MUTATED_BROKER'
 # Import directly by admin without an existing immutable snapshot is forbidden.
 $bad=$false
 try{
  & $import -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $root -SnapshotId '00000000000000000000000000000000' | Out-Null
 } catch{$bad=$_.Exception.Message -match 'TURBO_D3_PRODUCTION_ACTIVATION_REQUIRES_VERIFIED_SWITCH'}
 Check $bad 'TURBO_GIT61_UNCONFIRMED_IMPORT_ACCEPTED'
 & $script -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $root -ConfirmTarget $root
 Check (Test-Path -LiteralPath $activePath -PathType Leaf) 'TURBO_GIT61_BROKER_JOURNAL_MISSING'
 Check (Test-Path -LiteralPath $importPath -PathType Leaf) 'TURBO_GIT61_IMPORT_JOURNAL_MISSING'
 $active=Get-Content -LiteralPath $activePath -Raw -Encoding UTF8 | ConvertFrom-Json
 Check ($active.status -eq 'ACTIVE_PROVISIONED' -and $active.existing_root_import -eq $true -and
  [string]$active.turbo_root -ieq $root) 'TURBO_GIT61_WRONG_NATIVE_BINDING'
 Check ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hash) 'TURBO_GIT61_USER_CONTENT_CHANGED'
 $bad=$false
 try{
  & $script -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $root -ConfirmTarget $root | Out-Null
 } catch{$bad=$_.Exception.Message -match 'TURBO_GIT61_EXISTING_BROKER_REQUIRES_VERIFIED_SWITCH'}
 Check $bad 'TURBO_GIT61_DUPLICATE_BROKER_ENROLLMENT_ACCEPTED'
 & $native -Action Revoke -ProjectRoot $project
 Check (-not (Test-Path -LiteralPath $activePath)) 'TURBO_GIT61_BROKER_REVOCATION_INCOMPLETE'
 Check (-not (Test-Path -LiteralPath $importPath)) 'TURBO_GIT61_IMPORT_REVOCATION_INCOMPLETE'
 foreach($p in @($ws,$root,$sub,$file)){
  Check ((Get-Acl -LiteralPath $p -Audit).Sddl -ceq $baseline[$p]) ('TURBO_GIT61_SDDL_MISMATCH:'+$p)
 }
 Check ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hash) 'TURBO_GIT61_USER_HASH_MISMATCH'
 Check (Test-Path -LiteralPath $root -PathType Container) 'TURBO_GIT61_ORIGINAL_ROOT_DELETED'
 Write-Host 'TURBO_GIT61_EXISTING_PROJECT_NATIVE_IMPORT_AND_EXACT_ROLLBACK_PASS'
} finally{
 if(Test-Path -LiteralPath $activePath -PathType Leaf){
  try{& $native -Action Revoke -ProjectRoot $project | Out-Null}
  catch{Write-Warning ('TURBO_GIT61_CI_REVOKE_FAILED:'+$_.Exception.Message)}
 }
 if(Test-Path -LiteralPath $importPath -PathType Leaf){
  try{& $import -Action Recover -ProjectRoot $project | Out-Null}
  catch{Write-Warning ('TURBO_GIT61_CI_RECOVER_FAILED:'+$_.Exception.Message)}
 }
 if((Test-Path -LiteralPath $activePath) -or (Test-Path -LiteralPath $importPath)){
  Write-Warning ('TURBO_GIT61_UNRECOVERED_FIXTURE:'+ $fixture)
 }elseif(Test-Path -LiteralPath $fixture -PathType Container){
  Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
 }
}
