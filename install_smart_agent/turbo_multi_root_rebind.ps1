# Git62: verified administrative policy-revision rebind for ALREADY enrolled
# independent AppContainer roots. A registry addition does not become a grant
# until each existing enrollment independently passes this native OS check.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [switch]$VerifyOnly,
 [switch]$RepairMissingSentinels,
 [switch]$AllowPendingEnrollments,
 [switch]$MigrateRootEpochs,
 [string]$SelectedTurboRoot=''
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$project=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$sec=Join-Path $project 'localdata\secure\windows_security'
$dir=Join-Path $sec 'turbo_root_activations'
if(-not (Test-Path -LiteralPath $dir -PathType Container)){return}
. (Join-Path $PSScriptRoot 'turbo_multi_root_paths.ps1')
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 throw 'GIT62_REBIND_ADMIN_REQUIRED'
}
$sha=[Security.Cryptography.SHA256]::Create()
try {$digest=[BitConverter]::ToString($sha.ComputeHash(
 [Text.Encoding]::UTF8.GetBytes($project.ToUpperInvariant()))).Replace('-','')}
finally {$sha.Dispose()}
$mutex=[Threading.Mutex]::new($false,('Global\SmartAgentTurbo-'+$digest.Substring(0,32)))
$held=$false
$restored=0
# VerifyOnly must remain read-only; explicit repair never publishes a revision.
if(($VerifyOnly -and ($RepairMissingSentinels -or $MigrateRootEpochs)) -or
   ($RepairMissingSentinels -and $MigrateRootEpochs)){
 throw 'GIT62_REBIND_REPAIR_AND_VERIFY_CONFLICT'
}
if($SelectedTurboRoot -and -not ($VerifyOnly -or $RepairMissingSentinels)){
 throw 'GIT62_REBIND_PARTIAL_COMMIT_FORBIDDEN'
}
function Read-Json([string]$path){
 if((Get-Item -LiteralPath $path -Force -ErrorAction Stop).Attributes -band
    [IO.FileAttributes]::ReparsePoint){throw 'GIT62_REBIND_REPARSE_STATE'}
 return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
function Equal([string]$a,[string]$b){
 return ([IO.Path]::GetFullPath($a).TrimEnd('\')).Equals(
  [IO.Path]::GetFullPath($b).TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase)
}
try{
 try{$held=$mutex.WaitOne([TimeSpan]::FromSeconds(320))}
 catch [Threading.AbandonedMutexException]{$held=$true}
 if(-not $held){throw 'GIT62_REBIND_MUTEX_TIMEOUT'}
 if((Get-Item -LiteralPath $dir -Force).Attributes -band
   [IO.FileAttributes]::ReparsePoint){throw 'GIT62_REBIND_JOURNAL_DIR_REPARSE'}
 $mode=Read-Json (Join-Path $sec 'acl_mode.json')
 $policy=Read-Json (Join-Path $sec 'workspace_access_policy.json')
 if($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'on' -or
    -not (Equal $mode.install_root $project) -or
    $policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
    $policy.turbo_requested -ne $true -or [int]$policy.revision -lt 1){
   throw 'GIT62_REBIND_POLICY_UNTRUSTED'
 }
 if(Test-Path -LiteralPath (Join-Path $sec 'turbo_existing_root_switch.json')){
   throw 'GIT62_REBIND_SWITCH_PENDING'
 }
 if(Test-Path -LiteralPath (Join-Path $sec 'turbo_os_activation.json')){
   throw 'GIT62_REBIND_LEGACY_BROKER_REQUIRES_REVOKE'
 }
 if(-not ('SmartAgentGit47AppContainerProbe' -as [type])){
   Add-Type -Path (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -ErrorAction Stop
 }
 $entries=@(Get-ChildItem -LiteralPath $dir -File -Filter 'activation-*.json' -ErrorAction Stop)
 if(($AllowPendingEnrollments -and $entries.Count -gt @($policy.turbo_roots).Count) -or
    (-not $AllowPendingEnrollments -and $entries.Count -ne @($policy.turbo_roots).Count)){
   throw 'GIT62_REBIND_JOURNAL_COUNT_MISMATCH'
 }
 if($SelectedTurboRoot){
   $selected=[IO.Path]::GetFullPath($SelectedTurboRoot).TrimEnd('\')
   if(@($policy.turbo_roots | Where-Object { Equal ([string]$_) $selected }).Count -ne 1){
     throw 'GIT62_REBIND_SELECTED_ROOT_NOT_REGISTERED'
   }
 }
 # Phase 1: verify ALL active journals and their OS child-token isolation
 # BEFORE modifying any journal revisions.
 $verified=@()
 $seenRoots=@{}
 foreach($file in $entries){
   $a=Read-Json $file.FullName
   if($SelectedTurboRoot -and -not (Equal ([string]$a.turbo_root) $selected)){continue}
   if($a.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
      $a.status -ne 'ACTIVE_PROVISIONED' -or
      [string]$a.install_root -ne $project -or
      -not (Test-Path -LiteralPath ([string]$a.turbo_root) -PathType Container) -or
      [string]$a.package_sid -notmatch '^S-1-15-2-'){
     throw 'GIT62_REBIND_ACTIVATION_UNTRUSTED'
   }
   $j=Get-TurboRootJournalPaths $sec ([string]$a.turbo_root)
   if(-not $j.activation.Equals($file.FullName,[StringComparison]::OrdinalIgnoreCase)){
     throw 'GIT62_REBIND_INDEX_MISMATCH'
   }
   if(@($policy.turbo_roots | Where-Object { Equal ([string]$_) ([string]$a.turbo_root) }).Count -ne 1 -or
      @($policy.writable_workspaces | Where-Object { Equal ([string]$_) ([string]$a.workspace_root) }).Count -ne 1){
      throw 'GIT62_REBIND_ROOT_NOT_AUTHORIZED'
   }
   $root=[IO.Path]::GetFullPath([string]$a.turbo_root).TrimEnd('\')
   $rootKey=$root.ToUpperInvariant()
   if($seenRoots.ContainsKey($rootKey)){throw 'GIT62_REBIND_DUPLICATE_ROOT'}
   $seenRoots[$rootKey]=$true
   $ws=[IO.Path]::GetFullPath([string]$a.workspace_root).TrimEnd('\')
   if(-not $root.StartsWith($ws+'\',[StringComparison]::OrdinalIgnoreCase)){
      throw 'GIT62_REBIND_ROOT_OUTSIDE_WORKSPACE'
   }
   $actualSid=[SmartAgentGit47AppContainerProbe]::DeriveSid([string]$a.moniker)
   if(-not $actualSid.Equals([string]$a.package_sid,[StringComparison]::OrdinalIgnoreCase)){
      throw 'GIT62_REBIND_SID_MISMATCH'
   }
   # Protected Broker hash and recorded SID must be trusted BEFORE repair.
   $entry=[IO.Path]::GetFullPath([string]$a.broker_entry).TrimEnd('\')
   $protectedEntry=Join-Path $sec 'turbo_broker_code\turbo_os_execute.ps1'
   if(-not (Equal $entry $protectedEntry) -or
      (Get-FileHash -LiteralPath $entry -Algorithm SHA256).Hash -ine
       [string]$a.broker_source_sha256){
     throw 'GIT62_REBIND_BROKER_HASH_CHANGED'
   }
   $sentinel=[IO.Path]::GetFullPath([string]$a.negative_sentinel_path).TrimEnd('\')
   $sentinelParent=[IO.Path]::GetDirectoryName($root)
   if(-not ([IO.Path]::GetDirectoryName($sentinel)).Equals(
       $sentinelParent,[StringComparison]::OrdinalIgnoreCase) -or
       [IO.Path]::GetFileName($sentinel) -cnotmatch '^\.git50-os-denial-[0-9a-fA-F]{32}$'){
     throw 'GIT62_REBIND_SENTINEL_PATH_UNTRUSTED'
   }
   # A trusted journal may name only this Root's sibling. No path component
   # may be a junction or symlink. CreateNew never overwrites user data.
   $cursor=$sentinelParent
   while($cursor){
     $parentItem=Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
     if(($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){
       throw 'GIT62_REBIND_SENTINEL_PARENT_REPARSE'
     }
     $next=[IO.Path]::GetDirectoryName($cursor)
     if(-not $next -or $next -eq $cursor){break}
     $cursor=$next
   }
   if(-not [IO.File]::Exists($sentinel)){
     if(-not $RepairMissingSentinels){
       throw ('GIT62_REBIND_SENTINEL_MISSING:root='+$root)
     }
     $bytes=[Text.Encoding]::UTF8.GetBytes('OUTSIDE_SENTINEL')
     $stream=[IO.FileStream]::new($sentinel,[IO.FileMode]::CreateNew,
       [IO.FileAccess]::Write,[IO.FileShare]::None)
     try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}
     finally{$stream.Dispose()}
     & icacls.exe $sentinel '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
     if($LASTEXITCODE -ne 0){throw 'GIT62_REBIND_SENTINEL_PROTECTION_FAILED'}
     $restored++
     Write-Host ('GIT62_SENTINEL_RECREATED_PENDING_ATTESTATION root='+$root)
   }
   $sentinelItem=Get-Item -LiteralPath $sentinel -Force -ErrorAction Stop
   if(($sentinelItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      $sentinelItem.PSIsContainer -or
      [IO.File]::ReadAllText($sentinel) -cne 'OUTSIDE_SENTINEL'){
     throw 'GIT62_REBIND_SENTINEL_UNTRUSTED'
   }
   $literal=$sentinel.Replace("'","''")
   $script=@"
try { [void][IO.File]::ReadAllText('$literal'); exit 89 }
catch [System.UnauthorizedAccessException] { exit 0 }
"@
   $os=[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
       [string]$a.moniker,$script,$root,[uint32]30000) | ConvertFrom-Json -ErrorAction Stop
   if($os.sandbox_identity_verified -ne $true -or
      $os.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
      $os.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
      [int]$os.exit_code -ne 0){
      throw ('GIT62_REBIND_KERNEL_ATTESTATION_FAILED:root='+$root+';exit_code='+[int]$os.exit_code+';identity='+[string]$os.sandbox_identity_verified+';backend='+[string]$os.sandbox_backend+';child_policy='+[string]$os.child_process_policy)
   }
   $verified+=,[pscustomobject]@{path=$file.FullName;record=$a}
 }
 if($SelectedTurboRoot -and $verified.Count -ne 1){
   throw 'GIT62_REBIND_SELECTED_JOURNAL_MISSING'
 }
 $epochMap=@{}
 if($policy.PSObject.Properties.Name -contains 'turbo_root_epochs'){
   foreach($row in @($policy.turbo_root_epochs)){
     $key=[IO.Path]::GetFullPath([string]$row.root).TrimEnd('\').ToUpperInvariant()
     if($epochMap.ContainsKey($key) -or [int]$row.epoch -lt 1){
       throw 'GIT62_REBIND_ROOT_EPOCH_UNTRUSTED'
     }
     $epochMap[$key]=[int]$row.epoch
   }
   if($epochMap.Count -ne @($policy.turbo_roots).Count){
     throw 'GIT62_REBIND_ROOT_EPOCH_COUNT_MISMATCH'
   }
   foreach($rootItem in @($policy.turbo_roots)){
     if(-not $epochMap.ContainsKey(([IO.Path]::GetFullPath([string]$rootItem).TrimEnd('\').ToUpperInvariant()))){
       throw 'GIT62_REBIND_ROOT_EPOCH_MISSING'
     }
   }
 }
 if($MigrateRootEpochs){
   # Explicit admin-only, OS-attested legacy recovery; partial registrations
   # remain pending and get the current policy epoch when enrolled.
   if($epochMap.Count -eq 0){
     if($SelectedTurboRoot){throw 'GIT62_ROOT_EPOCH_PARTIAL_MIGRATION_FORBIDDEN'}
     $rows=@()
     foreach($rootItem in @($policy.turbo_roots)){
       $key=[IO.Path]::GetFullPath([string]$rootItem).TrimEnd('\').ToUpperInvariant()
       $record=@($verified | Where-Object {
         ([IO.Path]::GetFullPath([string]$_.record.turbo_root).TrimEnd('\').ToUpperInvariant()) -eq $key
       })
       $epoch=if($record.Count -eq 1){[int]$record[0].record.policy_revision}else{[int]$policy.revision}
       if($epoch -lt 1 -or $epoch -gt [int]$policy.revision){throw 'GIT62_ROOT_EPOCH_MIGRATION_INVALID'}
       $rows+=,[ordered]@{root=[string]$rootItem;epoch=$epoch}
     }
     $policy | Add-Member -NotePropertyName turbo_root_epochs -NotePropertyValue $rows -Force
     $policyPath=Join-Path $sec 'workspace_access_policy.json'
     $before=Get-FileHash -LiteralPath $policyPath -Algorithm SHA256
     $tmp=Join-Path $sec ('workspace_access_policy.git62-'+[Guid]::NewGuid().ToString('N')+'.tmp')
     try {
       $bytes=[Text.Encoding]::UTF8.GetBytes(($policy | ConvertTo-Json -Depth 18))
       $stream=[IO.FileStream]::new($tmp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
       try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}
       finally{$stream.Dispose()}
       Set-Acl -LiteralPath $tmp -AclObject (Get-Acl -LiteralPath $policyPath) -ErrorAction Stop
       if((Get-FileHash -LiteralPath $policyPath -Algorithm SHA256).Hash -ne $before.Hash){
         throw 'GIT62_ROOT_EPOCH_POLICY_CONCURRENT_UPDATE'
       }
       Move-Item -LiteralPath $tmp -Destination $policyPath -Force -ErrorAction Stop
     } finally {Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
     Write-Host ('GIT62_ROOT_EPOCH_MIGRATION_PASS roots='+$rows.Count+' enrolled='+$verified.Count)
   } else {
     Write-Host ('GIT62_ROOT_EPOCH_ALREADY_MIGRATED roots='+$epochMap.Count)
   }
   return
 }
 if($VerifyOnly){
   Write-Host ('GIT62_PER_ROOT_PRECHECK_PASS roots='+$verified.Count)
   return
 }
 if($RepairMissingSentinels){
   # No revision is written: this operation only repairs absent probes and
   # attests every selected AppContainer using genuine kernel denial.
   Write-Host ('GIT62_SENTINEL_RECOVERY_PRECHECK_PASS roots='+$verified.Count+
     ' restored='+$restored)
   return
 }
 # Phase 2: independent, protected, replace-atomic journal writes. A crash
 # before every Root is rebased leaves that Root fail-closed (stale revision).
 foreach($item in $verified){
   $a=$item.record
   $key=[IO.Path]::GetFullPath([string]$a.turbo_root).TrimEnd('\').ToUpperInvariant()
   $epoch=if($epochMap.Count){[int]$epochMap[$key]}else{[int]$policy.revision}
   if([int]$a.policy_revision -eq $epoch){continue}
   $a.policy_revision=$epoch
   $tmp=[string]$item.path+'.tmp'
   try{
     $data=[Text.Encoding]::UTF8.GetBytes(($a | ConvertTo-Json -Depth 18))
     $stream=[IO.FileStream]::new($tmp,[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::None)
     try{$stream.Write($data,0,$data.Length);$stream.Flush($true)}
     finally{$stream.Dispose()}
     & icacls.exe $tmp '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' | Out-Null
     if($LASTEXITCODE -ne 0){throw 'GIT62_REBIND_PROTECTION_FAILED'}
     Move-Item -LiteralPath $tmp -Destination $item.path -Force -ErrorAction Stop
   } finally {Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
 }
 Write-Host ('GIT62_PER_ROOT_REBIND_PASS roots='+$verified.Count)
} finally{
 if($held){try{$mutex.ReleaseMutex()}catch{}}
 $mutex.Dispose()
}
