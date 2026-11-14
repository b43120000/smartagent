# Git62: disposable Windows-only two-Root Native AppContainer integration.
[CmdletBinding()] param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or -not $env:RUNNER_TEMP){throw 'GIT62_CI_ONLY'}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'GIT62_ADMIN_REQUIRED'}
function Check([bool]$ok,[string]$code){if(-not $ok){throw $code}}
$fixture=Join-Path ([IO.Path]::GetFullPath($env:RUNNER_TEMP)) ('Git62Native-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$ws=Join-Path $fixture 'workspace'
$rootA=Join-Path $ws 'alpha'
$rootB=Join-Path $ws 'beta'
$sec=Join-Path $project 'localdata\secure\windows_security'
$activate=Join-Path $PSScriptRoot '..\turbo_existing_root_activate.ps1'
$revoke=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
. (Join-Path $PSScriptRoot '..\turbo_multi_root_paths.ps1')
$ja=Get-TurboRootJournalPaths $sec $rootA
$jb=Get-TurboRootJournalPaths $sec $rootB
$before=@{}
$hashes=@{}
try{
 New-Item -ItemType Directory -Force -Path $sec,$rootA,$rootB | Out-Null
 foreach($root in @($rootA,$rootB)){
  $f=Join-Path $root 'keep.txt'
  [IO.File]::WriteAllText($f,'PRESERVE_GIT62_USER_FILE')
  $hashes[$f]=(Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash
 }
 foreach($path in @($ws,$rootA,$rootB,(Join-Path $rootA 'keep.txt'),(Join-Path $rootB 'keep.txt'))){
  $before[$path]=(Get-Acl -LiteralPath $path -Audit -ErrorAction Stop).Sddl
 }
 $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
 $policy=[ordered]@{schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1';revision=62;writable_workspaces=@($ws);read_only_roots=@();denied_write_roots=@();turbo_roots=@($rootA,$rootB);turbo_requested=$true}
 $profile=[ordered]@{schema='SMARTAGENT_WINDOWS_SECURITY_V2';trusted_executables=@()}
 [IO.File]::WriteAllText((Join-Path $sec 'acl_mode.json'),($mode|ConvertTo-Json -Depth 10))
 [IO.File]::WriteAllText((Join-Path $sec 'workspace_access_policy.json'),($policy|ConvertTo-Json -Depth 10))
 [IO.File]::WriteAllText((Join-Path $sec 'security_profile.json'),($profile|ConvertTo-Json -Depth 10))
 & $activate -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $rootA -ConfirmTarget $rootA
 Check (Test-Path -LiteralPath $ja.activation -PathType Leaf) 'GIT62_ROOT_A_ACTIVATION_MISSING'
 & $activate -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $rootB -ConfirmTarget $rootB
 Check (Test-Path -LiteralPath $jb.activation -PathType Leaf) 'GIT62_ROOT_B_ACTIVATION_MISSING'
 $a=Get-Content -LiteralPath $ja.activation -Raw -Encoding UTF8 | ConvertFrom-Json
 $b=Get-Content -LiteralPath $jb.activation -Raw -Encoding UTF8 | ConvertFrom-Json
 Check ($a.status -eq 'ACTIVE_PROVISIONED' -and $b.status -eq 'ACTIVE_PROVISIONED') 'GIT62_ACTIVATIONS_NOT_ACTIVE'
 Check ($a.existing_import_committed -eq $true -and $b.existing_import_committed -eq $true) 'GIT62_IMPORTS_NOT_COMMITTED'
 Check ([string]$a.package_sid -cne [string]$b.package_sid) 'GIT62_SAME_SID_UNSAFE'
 Check ([string]$a.moniker -cne [string]$b.moniker) 'GIT62_SAME_MONIKER_UNSAFE'
 Check (-not (Test-Path -LiteralPath (Join-Path $sec 'turbo_os_activation.json'))) 'GIT62_LEGACY_JOURNAL_UNEXPECTED'
 function Scoped($record,[string]$cwd,[string]$id){
  $cmd='[IO.File]::WriteAllText("own.txt","ROOT_'+$id+'");Write-Output "ROOT_'+$id+'"'
  $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
  $answer=& ([string]$record.broker_entry) -ProjectRoot $project -Workspace $ws -WorkingDirectory $cwd -SelectedTurboRoot $cwd -ExpectedRevision 62 -TaskId ('TASK-GIT62-'+$id) -ActionId ('ACT-GIT62-'+$id) -CommandBase64 $encoded -TimeoutSeconds 30
  $proof=$answer | Out-String | ConvertFrom-Json -ErrorAction Stop
  $diagnostic=@('sandbox_identity_verified','kernel_outside_read_denied',
    'kernel_outside_write_denied','command_exit_code','child_process_policy')
  $pairs=@($diagnostic | ForEach-Object {
    $prop=$proof.PSObject.Properties[$_]
    $_+'='+$(if($null -eq $prop){'MISSING'}else{[string]$prop.Value})
  })
  Write-Host ('GIT62_NATIVE_RECEIPT_DIAGNOSTIC root='+$id+' '+($pairs -join ' '))
  # The disposable command and fixture contain no user data; emit capped
  # native streams to distinguish command failure from OS isolation failure.
  foreach($streamName in @('stdout_base64','stderr_base64')){
    $prop=$proof.PSObject.Properties[$streamName]
    if($null -ne $prop -and $prop.Value){
      $decoded=[Text.Encoding]::UTF8.GetString(
          [Convert]::FromBase64String([string]$prop.Value))
      Write-Host ('GIT62_NATIVE_'+$streamName+'='+$decoded.Substring(0,[Math]::Min(800,$decoded.Length)))
    }
  }
  Check ($proof.sandbox_identity_verified -eq $true -and $proof.kernel_outside_read_denied -eq $true -and $proof.kernel_outside_write_denied -eq $true -and [int]$proof.command_exit_code -eq 0) ('GIT62_NATIVE_PROOF_FAILED_'+$id)
  Check ([IO.File]::ReadAllText((Join-Path $cwd 'own.txt')) -ceq ('ROOT_'+$id)) ('GIT62_ROOT_WRITE_FAILED_'+$id)
  $stdout=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$proof.stdout_base64))
  Check ($stdout.Contains('ROOT_'+$id)) ('GIT62_STDOUT_MISSING_'+$id)
 }
 Scoped $a $rootA 'A'
 Scoped $b $rootB 'B'
 Write-Host 'GIT62_TWO_SEPARATE_APP_CONTAINER_SIDS_AND_CROSS_ROOT_KERNEL_DENIAL_PASS'
 & $revoke -Action Revoke -ProjectRoot $project -TurboRoot $rootA
 Check (-not (Test-Path -LiteralPath $ja.activation)) 'GIT62_A_REVOKE_FAILED'
 Check (Test-Path -LiteralPath $jb.activation) 'GIT62_B_REVOKED_WITH_A'
 Scoped $b $rootB 'B'
 Write-Host 'GIT62_INDEPENDENT_BROKER_REVOCATION_PASS'
 & $revoke -Action Revoke -ProjectRoot $project
 Check (-not (Test-Path -LiteralPath $jb.activation)) 'GIT62_ALL_REVOKE_FAILED'
 foreach($path in @($ws,$rootA,$rootB,(Join-Path $rootA 'keep.txt'),(Join-Path $rootB 'keep.txt'))){
  Check ((Get-Acl -LiteralPath $path -Audit).Sddl -ceq $before[$path]) ('GIT62_SDDL_NOT_RESTORED:'+$path)
 }
 foreach($file in @($hashes.Keys)){
  Check ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hashes[$file]) ('GIT62_CONTENT_CHANGED:'+$file)
 }
 Check (Test-Path -LiteralPath (Join-Path $rootA 'own.txt') -PathType Leaf) 'GIT62_A_USER_OUTPUT_DELETED'
 Check (Test-Path -LiteralPath (Join-Path $rootB 'own.txt') -PathType Leaf) 'GIT62_B_USER_OUTPUT_DELETED'
 Write-Host 'GIT62_ALL_SDDL_AND_ORIGINAL_CONTENT_RESTORED_PASS'
} finally{
 foreach($root in @($rootA,$rootB)){
  $journal=Get-TurboRootJournalPaths $sec $root
  if(Test-Path -LiteralPath $journal.activation){
   try{& $revoke -Action Revoke -ProjectRoot $project -TurboRoot $root | Out-Null}
   catch{Write-Warning ('GIT62_EMERGENCY_REVOKE_FAILED:'+$_.Exception.Message)}
  }
 }
 if((Test-Path -LiteralPath $ja.activation) -or (Test-Path -LiteralPath $jb.activation)){
  Write-Warning ('GIT62_RETAIN_UNRECOVERED_FIXTURE:'+$fixture)
 }elseif(Test-Path -LiteralPath $fixture -PathType Container){
  Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
 }
}
