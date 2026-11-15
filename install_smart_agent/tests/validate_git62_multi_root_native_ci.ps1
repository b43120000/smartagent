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
$externalWritable=Join-Path $ws 'external_writable'
$externalReadOnly=Join-Path $fixture 'external_read_only'
$linkWritable=Join-Path $rootB 'node_modules'
$linkReadOnly=Join-Path $rootB 'read_only_link'
$sec=Join-Path $project 'localdata\secure\windows_security'
$activate=Join-Path $PSScriptRoot '..\turbo_existing_root_activate.ps1'
$revoke=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
$rebind=Join-Path $PSScriptRoot '..\turbo_multi_root_rebind.ps1'
$script:revision=62
. (Join-Path $PSScriptRoot '..\turbo_multi_root_paths.ps1')
$ja=Get-TurboRootJournalPaths $sec $rootA
$jb=Get-TurboRootJournalPaths $sec $rootB
$before=@{}
$hashes=@{}
try{
 New-Item -ItemType Directory -Force -Path $sec,$rootA,$rootB,$externalWritable,$externalReadOnly | Out-Null
 # A project may contain junctions to a writable sibling and a registered
 # read-only location. They must stay opaque, without target ACL modification.
 $outsideFiles=@((Join-Path $externalWritable 'package.txt'),
                 (Join-Path $externalReadOnly 'readme.txt'))
 foreach($f in $outsideFiles){
    [IO.File]::WriteAllText($f,'OUTSIDE_ORIGINAL_UNMODIFIED')
    $hashes[$f]=(Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash
 }
 New-Item -ItemType Junction -Path $linkWritable -Target $externalWritable -ErrorAction Stop | Out-Null
 New-Item -ItemType Junction -Path $linkReadOnly -Target $externalReadOnly -ErrorAction Stop | Out-Null
 foreach($link in @($linkWritable,$linkReadOnly)){
    Check ((Get-Item -LiteralPath $link -Force).Attributes -band
      [IO.FileAttributes]::ReparsePoint) ('GIT62_TEST_JUNCTION_MISSING:'+$link)
 }
 foreach($root in @($rootA,$rootB)){
  $f=Join-Path $root 'keep.txt'
  [IO.File]::WriteAllText($f,'PRESERVE_GIT62_USER_FILE')
  $hashes[$f]=(Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash
 }
 foreach($path in @($ws,$rootA,$rootB,$externalWritable,$externalReadOnly,(Join-Path $rootA 'keep.txt'),(Join-Path $rootB 'keep.txt'))+$outsideFiles){
  $before[$path]=(Get-Acl -LiteralPath $path -Audit -ErrorAction Stop).Sddl
 }
 $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
 $policy=[ordered]@{schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1';revision=62;writable_workspaces=@($ws);read_only_roots=@($externalReadOnly);denied_write_roots=@();turbo_roots=@($rootA,$rootB);turbo_requested=$true}
 $profile=[ordered]@{schema='SMARTAGENT_WINDOWS_SECURITY_V2';trusted_executables=@();executor_user='GIT62_CI_EXEC';executor_sid='S-1-5-21-100-200-300-400';executor_task_name='SmartAgent Restricted Executor';executor_endpoint=(Join-Path $sec 'executor_queue')}
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
 foreach($f in $outsideFiles){
  Check ((Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash -eq $hashes[$f]) 'GIT62_LINK_TARGET_CHANGED_ON_ENROLLMENT'
 }
 Write-Host 'GIT62_OPAQUE_NODE_MODULES_ENROLLED_PASS'
 # Git62 regression: isolate ProgramData so the machine-pointer test can
 # NEVER modify the Windows CI runner's real machine authorization.
 $savedProgramData=$env:ProgramData
 try{
  $env:ProgramData=Join-Path $fixture 'isolated_programdata'
  $machineDir=Join-Path $env:ProgramData 'SmartAgent'
  $configDir=Join-Path $project 'config'
  $sourceDir=Join-Path $project 'install_smart_agent'
  New-Item -ItemType Directory -Force -Path $machineDir,$configDir,$sourceDir | Out-Null
  $manifestPath=Join-Path $configDir 'protocol_manifest.json'
  [IO.File]::WriteAllText($manifestPath,'{"protocol_family":"SMARTAGENT_V9","protocol_version":9,"core_files":{}}')
  foreach($name in @('update.ps1','security_acl_policy.ps1')){
   Copy-Item -LiteralPath (Join-Path $PSScriptRoot ('..\'+$name)) -Destination (Join-Path $sourceDir $name) -Force -ErrorAction Stop
  }
  $protectedUpdater=Join-Path $machineDir 'protected_updater.ps1'
  Copy-Item -LiteralPath (Join-Path $sourceDir 'update.ps1') -Destination $protectedUpdater -ErrorAction Stop
  $updaterHash=(Get-FileHash -LiteralPath $protectedUpdater -Algorithm SHA256).Hash.ToLowerInvariant()
  $policyHash=(Get-FileHash -LiteralPath (Join-Path $sourceDir 'security_acl_policy.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()
  $machineFile=Join-Path $machineDir 'machine_authorization.json'
  $oldMachineHash=('0' * 64)
  $machine=[ordered]@{
   schema='SMARTAGENT_MACHINE_AUTHORIZATION_V1'
   protocol_family='SMARTAGENT_V9';protocol_version=9
   protocol_manifest_sha256=$oldMachineHash
   install_root=$project
   security_profile=(Join-Path $sec 'security_profile.json')
   executor_sid=$profile.executor_sid;executor_user=$profile.executor_user
   executor_task_name=$profile.executor_task_name
   executor_endpoint=$profile.executor_endpoint
   updater_path=$protectedUpdater;updater_sha256=$updaterHash
   updater_policy_sha256=$policyHash;updated_at=0
  }
  [IO.File]::WriteAllText($machineFile,($machine|ConvertTo-Json -Depth 16),[Text.UTF8Encoding]::new($false))
  & icacls.exe $machineFile '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' | Out-Null
  Check ($LASTEXITCODE -eq 0) 'GIT62_CI_MACHINE_POINTER_ACL_FAILED'
  $upgrade=Join-Path $PSScriptRoot '..\turbo_multi_root_upgrade_authorization.ps1'
  $holdPath=$jb.activation+'.hold'
  Move-Item -LiteralPath $jb.activation -Destination $holdPath -ErrorAction Stop
  try{
   $negative=''
   try{& $upgrade -ProjectRoot $project | Out-Null}
   catch{$negative=[string]$_.Exception.Message}
   Check ($negative -eq 'GIT62_UPGRADE_PARTIAL_ENROLLMENT') ('GIT62_CI_PARTIAL_ENROLLMENT_NOT_BLOCKED:'+$negative)
   $unchanged=Get-Content -LiteralPath $machineFile -Raw | ConvertFrom-Json
   Check ($unchanged.protocol_manifest_sha256 -eq $oldMachineHash) 'GIT62_CI_PARTIAL_ENROLLMENT_CHANGED_MACHINE_POINTER'
  }finally{Move-Item -LiteralPath $holdPath -Destination $jb.activation -ErrorAction Stop}
  $rootBefore=@{}
  foreach($r in @($rootA,$rootB)){
   $rootBefore[$r]=(Get-Acl -LiteralPath $r -Audit -ErrorAction Stop).Sddl
  }
  $journalBefore=@{}
  foreach($j in @($ja.activation,$jb.activation)){
   $journalBefore[$j]=(Get-FileHash -LiteralPath $j -Algorithm SHA256).Hash
  }
  & $upgrade -ProjectRoot $project
  $published=Get-Content -LiteralPath $machineFile -Raw -Encoding UTF8 | ConvertFrom-Json
  $expected=(Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
  Check ($published.protocol_manifest_sha256 -eq $expected) 'GIT62_CI_UPGRADE_MACHINE_DIGEST_NOT_REBOUND'
  Check ((Get-Acl -LiteralPath $machineFile).AreAccessRulesProtected) 'GIT62_CI_UPGRADE_MACHINE_ACL_NOT_PROTECTED'
  foreach($r in @($rootA,$rootB)){
   Check ((Get-Acl -LiteralPath $r -Audit -ErrorAction Stop).Sddl -ceq $rootBefore[$r]) 'GIT62_CI_UPGRADE_CHANGED_TURBO_ROOT_ACL'
  }
  foreach($j in @($ja.activation,$jb.activation)){
   Check ((Get-FileHash -LiteralPath $j -Algorithm SHA256).Hash -eq $journalBefore[$j]) 'GIT62_CI_UPGRADE_CHANGED_ENROLLMENT_JOURNAL'
  }
  & $upgrade -ProjectRoot $project
  Write-Host 'GIT62_MULTI_ROOT_MACHINE_UPGRADE_NATIVE_PASS'
 }finally{$env:ProgramData=$savedProgramData}
 function Scoped($record,[string]$cwd,[string]$id){
  $cmd='[IO.File]::WriteAllText("own.txt","ROOT_'+$id+'");"ROOT_'+$id+'"'
  $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cmd))
  $answer=& ([string]$record.broker_entry) -ProjectRoot $project -Workspace $ws -WorkingDirectory $cwd -SelectedTurboRoot $cwd -ExpectedRevision $script:revision -TaskId ('TASK-GIT62-'+$id) -ActionId ('ACT-GIT62-'+$id) -CommandBase64 $encoded -TimeoutSeconds 30
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
 $linkWriteProof=@'
try {
  $h=[IO.File]::Open('node_modules\escape.txt',[IO.FileMode]::CreateNew,
      [IO.FileAccess]::Write,[IO.FileShare]::None)
  $h.Dispose()
  exit 94
} catch [System.UnauthorizedAccessException] { exit 0 }
catch { exit 95 }
'@
 $linkProof=[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
    [string]$b.moniker,$linkWriteProof,$rootB,[uint32]30000) | ConvertFrom-Json
 Check ($linkProof.sandbox_identity_verified -eq $true -and
   [int]$linkProof.exit_code -eq 0 -and
   -not (Test-Path -LiteralPath (Join-Path $externalWritable 'escape.txt'))) 'GIT62_LINK_OUTSIDE_ROOT_WRITE_ESCAPE'
 Write-Host 'GIT62_JUNCTION_TARGET_KERNEL_WRITE_DENIED_PASS'
 # A Registry/Workspace update changes Policy revision. Independently verify
 # and rebind BOTH still-active Root SIDs before continuing native execution.
 $policy.revision=63
 [IO.File]::WriteAllText((Join-Path $sec 'workspace_access_policy.json'),
     ($policy | ConvertTo-Json -Depth 10))
 # Real Windows fixture for the user incident: old Rev 62 journal,
 # new Rev 63 policy, missing sibling negative sentinel.
 $sentinelB=[string]$b.negative_sentinel_path
 Check (Test-Path -LiteralPath $sentinelB -PathType Leaf) 'GIT62_CI_SENTINEL_ORIGIN_MISSING'
 [IO.File]::WriteAllText($sentinelB,'TAMPERED')
 $tamperBlocked=$false
 try{& $rebind -ProjectRoot $project -RepairMissingSentinels -SelectedTurboRoot $rootB | Out-Null}
 catch{$tamperBlocked=$true}
 Check $tamperBlocked 'GIT62_CI_TAMPERED_SENTINEL_WAS_OVERWRITTEN'
 Check ([IO.File]::ReadAllText($sentinelB) -ceq 'TAMPERED') 'GIT62_CI_UNTRUSTED_PROBE_MODIFIED'
 Remove-Item -LiteralPath $sentinelB -Force -ErrorAction Stop
 $missingBlocked=$false
 try{& $rebind -ProjectRoot $project -VerifyOnly | Out-Null}
 catch{$missingBlocked=$true}
 Check $missingBlocked 'GIT62_CI_VERIFY_ONLY_RECREATED_SENTINEL'
 Check (-not (Test-Path -LiteralPath $sentinelB)) 'GIT62_CI_VERIFY_ONLY_MUTATED_FILES'
 & $rebind -ProjectRoot $project -RepairMissingSentinels -SelectedTurboRoot $rootB
 Check (Test-Path -LiteralPath $sentinelB -PathType Leaf) 'GIT62_CI_RECOVERY_DID_NOT_CREATE_SENTINEL'
 Check ([IO.File]::ReadAllText($sentinelB) -ceq 'OUTSIDE_SENTINEL') 'GIT62_CI_RECOVERY_SENTINEL_BAD_CONTENT'
 $stillOld=Get-Content -LiteralPath $jb.activation -Raw -Encoding UTF8 | ConvertFrom-Json
 Check ([int]$stillOld.policy_revision -eq 62) 'GIT62_CI_RECOVERY_BYPASSED_REVISION_COMMIT'
 Write-Host 'GIT62_MISSING_SENTINEL_NATIVE_RECOVERY_PRECHECK_PASS'
 & $rebind -ProjectRoot $project
 $script:revision=63
 foreach($f in @($ja.activation,$jb.activation)){
  $updated=Get-Content -LiteralPath $f -Raw -Encoding UTF8 | ConvertFrom-Json
  Check ([int]$updated.policy_revision -eq 63) 'GIT62_REBIND_REVISION_NOT_DURABLE'
 }
 Scoped $a $rootA 'A'
 Scoped $b $rootB 'B'
 Write-Host 'GIT62_NATIVE_PER_ROOT_POLICY_REVISION_REBIND_PASS'
 # Three-Root regression: an unfinished new enrollment must not invalidate
 # the two existing AppContainer grants. The global CAS advances to 64 but
 # their immutable Root epochs stay 63, without any sibling Rebind.
 $rootC=Join-Path $ws 'gamma'
 New-Item -ItemType Directory -Path $rootC -Force | Out-Null
 [IO.File]::WriteAllText((Join-Path $rootC 'keep.txt'),'THIRD_ROOT_USER_DATA')
 $beforeC=(Get-Acl -LiteralPath $rootC -Audit).Sddl
 $jc=Get-TurboRootJournalPaths $sec $rootC
 $policy.revision=64
 $policy.turbo_roots=@($rootA,$rootB,$rootC)
 $policy | Add-Member -Force -NotePropertyName turbo_root_epochs -NotePropertyValue @(
   [ordered]@{root=$rootA;epoch=63},
   [ordered]@{root=$rootB;epoch=63},
   [ordered]@{root=$rootC;epoch=64}
 )
 [IO.File]::WriteAllText((Join-Path $sec 'workspace_access_policy.json'),
    ($policy | ConvertTo-Json -Depth 10))
 $script:revision=64
 & $rebind -ProjectRoot $project -VerifyOnly -AllowPendingEnrollments
 Check (-not (Test-Path -LiteralPath $jc.activation)) 'GIT62_CI_UNENROLLED_ROOT_PREMATURELY_ACTIVE'
 Scoped $a $rootA 'A'
 Scoped $b $rootB 'B'
 foreach($j in @($ja.activation,$jb.activation)){
    $kept=Get-Content -LiteralPath $j -Raw -Encoding UTF8 | ConvertFrom-Json
    Check ([int]$kept.policy_revision -eq 63) 'GIT62_CI_SIBLING_EPOCH_CHANGED_BY_ADD'
 }
 Write-Host 'GIT62_PENDING_THIRD_ROOT_PRESERVES_EXISTING_NATIVE_BROKERS_PASS'
 & $activate -Action Activate -ProjectRoot $project -Workspace $ws -TurboRoot $rootC -ConfirmTarget $rootC
 $c=Get-Content -LiteralPath $jc.activation -Raw -Encoding UTF8 | ConvertFrom-Json
 Check ([int]$c.policy_revision -eq 64) 'GIT62_CI_THIRD_ROOT_EPOCH_WRONG'
 Scoped $c $rootC 'C'
 Scoped $a $rootA 'A'
 Scoped $b $rootB 'B'
 Write-Host 'GIT62_THREE_INDEPENDENT_NATIVE_BROKERS_PASS'
 & $revoke -Action Revoke -ProjectRoot $project -TurboRoot $rootC
 Check (-not (Test-Path -LiteralPath $jc.activation)) 'GIT62_CI_THIRD_ROOT_NOT_REVOKED'
 Check ([IO.File]::ReadAllText((Join-Path $rootC 'keep.txt')) -ceq 'THIRD_ROOT_USER_DATA') 'GIT62_CI_THIRD_ROOT_USER_DATA_LOST'
 $policy.revision=65
 $policy.turbo_roots=@($rootA,$rootB)
 $policy.turbo_root_epochs=@(
   [ordered]@{root=$rootA;epoch=63},
   [ordered]@{root=$rootB;epoch=63}
 )
 [IO.File]::WriteAllText((Join-Path $sec 'workspace_access_policy.json'),
   ($policy | ConvertTo-Json -Depth 10))
 $script:revision=65
 Scoped $a $rootA 'A'
 Scoped $b $rootB 'B'
 Write-Host 'GIT62_REMOVE_THIRD_ROOT_NO_SIBLING_REBIND_PASS'
 & $revoke -Action Revoke -ProjectRoot $project -TurboRoot $rootA
 Check (-not (Test-Path -LiteralPath $ja.activation)) 'GIT62_A_REVOKE_FAILED'
 Check (Test-Path -LiteralPath $jb.activation) 'GIT62_B_REVOKED_WITH_A'
 Scoped $b $rootB 'B'
 Write-Host 'GIT62_INDEPENDENT_BROKER_REVOCATION_PASS'
 # Simulate a crashed import with an import journal but no Activation.
 # Native Recovery MUST restore the original DACL/SID without deleting files.
 $orphanReceipt=$jb.activation+'.ci-orphaned-activation'
 Move-Item -LiteralPath $jb.activation -Destination $orphanReceipt -ErrorAction Stop
 try{
  & (Join-Path $PSScriptRoot '..\turbo_existing_root_import.ps1') -Action Recover -ProjectRoot $project -TurboRoot $rootB
  Check (-not (Test-Path -LiteralPath $jb.import)) 'GIT62_NATIVE_RECOVER_LEFT_IMPORT_JOURNAL'
  Check (-not (Test-Path -LiteralPath $jb.activation)) 'GIT62_NATIVE_RECOVER_RESURRECTED_ACTIVATION'
  Write-Host 'GIT62_PENDING_IMPORT_NATIVE_RECOVERY_PASS'
 }finally{
  if(Test-Path -LiteralPath $orphanReceipt){Remove-Item -LiteralPath $orphanReceipt -Force -ErrorAction Stop}
 }
 foreach($path in @($ws,$rootA,$rootB,$externalWritable,$externalReadOnly,(Join-Path $rootA 'keep.txt'),(Join-Path $rootB 'keep.txt'))+$outsideFiles){
  $current=(Get-Acl -LiteralPath $path -Audit).Sddl
  $normalize = {param([string]$descriptor)
    $descriptor -creplace '([DS]:[A-Z]*?)AI(?=[A-Z]*(?:\(|S:|$))','$1'
  }
  if((& $normalize $current) -cne (& $normalize ([string]$before[$path]))){
    Write-Host ('GIT62_SDDL_BEFORE: '+$before[$path])
    Write-Host ('GIT62_SDDL_AFTER:  '+$current)
    throw ('GIT62_SDDL_RIGHTS_OR_OWNER_NOT_RESTORED:'+$path)
  }
  if($current -cne $before[$path]){
    Write-Host ('GIT62_WINDOWS_AUTO_INHERITED_MARKER_ONLY_CHANGED: '+$path)
  }
 }
 foreach($file in @($hashes.Keys)){
  Check ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $hashes[$file]) ('GIT62_CONTENT_CHANGED:'+$file)
 }
 foreach($link in @($linkWritable,$linkReadOnly)){
  Check ((Get-Item -LiteralPath $link -Force).Attributes -band
    [IO.FileAttributes]::ReparsePoint) ('GIT62_ORIGINAL_LINK_DELETED:'+$link)
 }
 Check (Test-Path -LiteralPath (Join-Path $rootA 'own.txt') -PathType Leaf) 'GIT62_A_USER_OUTPUT_DELETED'
 Check (Test-Path -LiteralPath (Join-Path $rootB 'own.txt') -PathType Leaf) 'GIT62_B_USER_OUTPUT_DELETED'
 Write-Host 'GIT62_DACL_ACE_OWNER_GROUP_AND_CONTENT_PRESERVATION_PASS'
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
