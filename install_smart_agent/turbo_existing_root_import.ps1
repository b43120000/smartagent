# P3-D3 experimental production importer. Explicit elevated invocation only.
# Existing root admission is NOT implicit from Turbo ON. The immutable
# Prepare/Verify snapshot must be passed as a separate operator token.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][ValidateSet('Activate','Revoke','Recover','Status')][string]$Action,
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [string]$Workspace='', [string]$TurboRoot='', [string]$SnapshotId='',
 [ValidateRange(1,100000)][int]$MaxEntries=5000
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function Full([string]$p){return [IO.Path]::GetFullPath($p).TrimEnd('\')}
function Same([string]$a,[string]$b){return (Full $a).Equals((Full $b),[StringComparison]::OrdinalIgnoreCase)}
function Under([string]$a,[string]$b){return (Full $a).StartsWith((Full $b)+'\',[StringComparison]::OrdinalIgnoreCase)}
function Load([string]$p) {
 if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {throw ('TURBO_D3_STATE_MISSING:'+$p)}
 if ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'TURBO_D3_REPARSE_STATE'}
 return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
function Sddl([string]$p) {return [string](Get-Acl -LiteralPath $p -Audit -ErrorAction Stop).Sddl}
function Save-Atomic([string]$p,[object]$object,[bool]$userReadable) {
 $tmp=$p+'.tmp'
 $bytes=[Text.Encoding]::UTF8.GetBytes(($object | ConvertTo-Json -Depth 16))
 $stream=[IO.FileStream]::new($tmp,[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::None)
 try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
 $grants=@('*S-1-5-18:F','*S-1-5-32-544:F')
 if($userReadable){$grants+=,'*S-1-5-32-545:R'}
 & icacls.exe $tmp '/inheritance:r' '/grant:r' $grants | Out-Null
 if($LASTEXITCODE -ne 0){throw 'TURBO_D3_PROTECTED_STATE_WRITE_FAILED'}
 Move-Item -LiteralPath $tmp -Destination $p -Force -ErrorAction Stop
}
function Check-Identity([object]$entry) {
 $p=Full ([string]$entry.path)
 if(-not ((Same $p $script:root) -or (Under $p $script:root))){throw 'TURBO_D3_PATH_ESCAPE'}
 $item=Get-Item -LiteralPath $p -Force -ErrorAction Stop
 if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'TURBO_D3_REPARSE_TREE'}
 if([bool]$item.PSIsContainer -ne [bool]$entry.directory){throw 'TURBO_D3_FILE_TYPE_CHANGED'}
 if([SmartAgentTurboBFileId]::Check($p,[bool]$entry.directory) -cne [string]$entry.identity){
  throw ('TURBO_D3_FILE_ID_CHANGED:'+$p)
 }
}
function Restore-Exact([string]$p,[string]$s) {
 $acl=Get-Acl -LiteralPath $p -ErrorAction Stop
 $acl.SetSecurityDescriptorSddlForm($s,[Security.AccessControl.AccessControlSections]::Access)
 Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
 [SmartAgentTurboExistingSecurity]::RestoreMandatoryLabel($p,$s)
 if((Sddl $p) -cne $s){throw ('TURBO_D3_EXACT_SDDL_RESTORE_FAILED:'+$p)}
}
function Revoke-Tree([object]$j) {
 $script:root=Full ([string]$j.turbo_root)
 $ws=Full ([string]$j.workspace_root)
 if(-not (Under $script:root $ws)){throw 'TURBO_D3_JOURNAL_SCOPE_INVALID'}
 $snap=Load (Join-Path $script:snapshotDir ('snapshot-'+[string]$j.snapshot_id+'.json'))
 if($snap.schema -ne 'SMARTAGENT_TURBO_EXISTING_ROOT_SNAPSHOT_V1' -or
    -not (Same $snap.turbo_root $script:root) -or
    [int]$snap.entries_count -ne @($snap.entries).Count){throw 'TURBO_D3_SNAPSHOT_INVALID'}
 $items=@(& (Join-Path $PSScriptRoot 'turbo_existing_root_preflight.ps1') -ProjectRoot $script:project -Workspace $ws -TurboRoot $script:root -IncludeSensitiveInventory -MaxEntries $MaxEntries |
     Where-Object {$_ -is [System.Collections.Specialized.OrderedDictionary]})
 if($items.Count -ne 1){throw 'TURBO_D3_REVOKE_ENUM_FAILED'}
 $original=@{}
 foreach($entry in @($snap.entries)){$original[(Full ([string]$entry.path))]=$entry}
 # New files may have been legitimately produced by the AppContainer.
 # Never delete them. Remove the imported Package SID, raise their
 # low-integrity labels to the Windows default Medium only.
 foreach($item in @($items[0].entries | Sort-Object {([string]$_.path).Length} -Descending)){
   $p=Full ([string]$item.path)
   $prior=$original[$p]
   if($null -eq $prior -or [string]$prior.identity -cne [string]$item.identity){
      & icacls.exe $p '/remove:g' ('*'+[string]$j.package_sid) | Out-Null
      if($LASTEXITCODE -ne 0){throw ('TURBO_D3_NEW_ITEM_ACE_REVOKE_FAILED:'+$p)}
      & icacls.exe $p '/setintegritylevel' $(if([bool]$item.directory){'(OI)(CI)M'}else{'M'}) | Out-Null
      if($LASTEXITCODE -ne 0){throw ('TURBO_D3_NEW_ITEM_IL_RESET_FAILED:'+$p)}
   }
 }
 # Restore recorded originals parent-first, children last.
 foreach($entry in @($snap.entries | Sort-Object {([string]$_.path).Length})){
   $p=Full ([string]$entry.path)
   if(-not (Test-Path -LiteralPath $p)){continue} # intentionally deleted
   $current=Get-Item -LiteralPath $p -Force
   if([bool]$current.PSIsContainer -ne [bool]$entry.directory){continue}
   if([SmartAgentTurboBFileId]::Check($p,[bool]$entry.directory) -cne [string]$entry.identity){continue}
   Restore-Exact $p ([string]$entry.sddl)
 }
 foreach($ancestor in @($j.ancestors | Sort-Object {([string]$_.path).Length})){
   $p=Full ([string]$ancestor.path)
   if(-not ((Same $p $ws) -or (Under $p $ws))){throw 'TURBO_D3_ANCESTOR_ESCAPE'}
   if([SmartAgentTurboBFileId]::Check($p,$true) -cne [string]$ancestor.identity){throw 'TURBO_D3_ANCESTOR_CHANGED'}
   Restore-Exact $p ([string]$ancestor.sddl)
 }
 # A successful rollback must not leave this Package SID on any item.
 $after=@(& (Join-Path $PSScriptRoot 'turbo_existing_root_preflight.ps1') -ProjectRoot $script:project -Workspace $ws -TurboRoot $script:root -IncludeSensitiveInventory -MaxEntries $MaxEntries |
     Where-Object {$_ -is [System.Collections.Specialized.OrderedDictionary]})
 foreach($item in @($after[0].entries)){
    if([string]$item.sddl -match [Regex]::Escape([string]$j.package_sid)){
       throw ('TURBO_D3_PACKAGE_SID_REMAINING:'+$item.path)
    }
 }
 [SmartAgentGit47AppContainerProbe]::DeleteProfile([string]$j.moniker)
 if(Test-Path -LiteralPath $script:activation){Remove-Item -LiteralPath $script:activation -Force -ErrorAction Stop}
 Remove-Item -LiteralPath $script:journal -Force -ErrorAction Stop
 Write-Host 'TURBO_D3_REVOKE_EXACT_SDDL_PASS'
}
$script:project=Full $ProjectRoot
$sec=Join-Path $script:project 'localdata\secure\windows_security'
$script:activation=Join-Path $sec 'turbo_os_activation.json'
$script:journal=Join-Path $sec 'turbo_existing_root_import.json'
$script:snapshotDir=Join-Path $sec 'turbo_existing_root_snapshots'
if($Action -eq 'Status'){
 if(Test-Path -LiteralPath $script:journal){Write-Host ('TURBO_D3_STATUS='+(Load $script:journal).status)}
 else{Write-Host 'TURBO_D3_STATUS=NOT_CONFIGURED'}
 return
}
$admin=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'TURBO_D3_ADMIN_REQUIRED'}
if(-not (Test-Path -LiteralPath $script:snapshotDir -PathType Container)){throw 'TURBO_D3_SNAPSHOT_DIR_MISSING'}
if(-not ('SmartAgentTurboExistingSecurity' -as [type])) {
 Add-Type -Path (Join-Path $PSScriptRoot 'turbo_existing_security.cs') -ErrorAction Stop
}
if(-not ('SmartAgentGit47AppContainerProbe' -as [type])) {
 Add-Type -Path (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -ErrorAction Stop
}
if(-not ('SmartAgentTurboBFileId' -as [type])) {
 # Read-only preflight provides the stable NTFS File-ID API.
 $preWs=if($Action -eq 'Activate'){$Workspace}else{[string](Load $script:journal).workspace_root}
 $preRoot=if($Action -eq 'Activate'){$TurboRoot}else{[string](Load $script:journal).turbo_root}
 & (Join-Path $PSScriptRoot 'turbo_existing_root_preflight.ps1') -ProjectRoot $script:project -Workspace $preWs -TurboRoot $preRoot -MaxEntries $MaxEntries | Out-Null
}
$hash=[Security.Cryptography.SHA256]::Create()
try{$digest=[BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:project.ToUpperInvariant()))).Replace('-','')}
finally{$hash.Dispose()}
$mutex=[Threading.Mutex]::new($false,('Global\SmartAgentTurbo-'+$digest.Substring(0,32)))
$held=$false
try {
 try{$held=$mutex.WaitOne([TimeSpan]::FromSeconds(320))}
 catch [Threading.AbandonedMutexException]{$held=$true}
 if(-not $held){throw 'TURBO_D3_BROKER_MUTEX_TIMEOUT'}
 if($Action -in @('Revoke','Recover')){
    if(-not (Test-Path -LiteralPath $script:journal -PathType Leaf)){
      if($Action -eq 'Recover'){Write-Host 'TURBO_D3_NOTHING_TO_RECOVER';return}
      throw 'TURBO_D3_REVOKE_JOURNAL_MISSING'
    }
    $j=Load $script:journal
    if($j.schema -ne 'SMARTAGENT_TURBO_EXISTING_IMPORT_V1' -or
       -not (Same $j.install_root $script:project) -or
       $j.status -notin @('APPLYING','ACTIVE','REVOKING')){
      throw 'TURBO_D3_REVOKE_JOURNAL_UNTRUSTED'
    }
    $j.status='REVOKING'
    Save-Atomic $script:journal $j $false
    Revoke-Tree $j
    return
 }
 if($SnapshotId -cnotmatch '^[0-9a-f]{32}$'){throw 'TURBO_D3_SNAPSHOT_ID_INVALID'}
 if(Test-Path -LiteralPath $script:activation){throw 'TURBO_D3_OLD_BROKER_REQUIRES_VERIFIED_REVOCATION'}
 if(Test-Path -LiteralPath $script:journal){throw 'TURBO_D3_PENDING_IMPORT_REQUIRES_RECOVERY'}
 $ws=Full $Workspace
 $script:root=Full $TurboRoot
 if(-not (Under $script:root $ws) -or (Under $ws $script:project) -or (Under $script:project $ws)){throw 'TURBO_D3_SCOPE_INVALID'}
 $mode=Load (Join-Path $sec 'acl_mode.json')
 $policy=Load (Join-Path $sec 'workspace_access_policy.json')
 if($mode.mode -ne 'on' -or -not (Same $mode.install_root $script:project) -or
    $policy.turbo_requested -ne $true -or @($policy.turbo_roots).Count -ne 1 -or
    -not (Same $policy.turbo_roots[0] $script:root) -or
    @($policy.writable_workspaces | Where-Object {Same ([string]$_) $ws}).Count -ne 1){
    throw 'TURBO_D3_ACL_ON_TURBO_POLICY_REQUIRED'
 }
 & (Join-Path $PSScriptRoot 'turbo_existing_root_prepare.ps1') -Action Verify -ProjectRoot $script:project -Workspace $ws -TurboRoot $script:root -SnapshotId $SnapshotId -MaxEntries $MaxEntries
 $snap=Load (Join-Path $script:snapshotDir ('snapshot-'+$SnapshotId+'.json'))
 $ancestors=@()
 $cur=[IO.Path]::GetDirectoryName($script:root)
 while($cur -and ((Same $cur $ws) -or (Under $cur $ws))){
    $ancestors+=,[ordered]@{path=$cur;identity=[SmartAgentTurboBFileId]::Check($cur,$true);sddl=Sddl $cur}
    if(Same $cur $ws){break}
    $cur=[IO.Path]::GetDirectoryName($cur)
 }
 if($ancestors.Count -lt 1 -or -not (Same $ancestors[-1].path $ws)){throw 'TURBO_D3_ANCESTOR_INCOMPLETE'}
 $moniker='SmartAgentTurbo_'+[Guid]::NewGuid().ToString('N')
 $sid=[SmartAgentGit47AppContainerProbe]::CreateProfile($moniker)
 if($sid -notmatch '^S-1-15-2-'){throw 'TURBO_D3_PROFILE_SID_INVALID'}
 $j=[ordered]@{
    schema='SMARTAGENT_TURBO_EXISTING_IMPORT_V1';status='APPLYING'
    install_root=$script:project;workspace_root=$ws;turbo_root=$script:root
    snapshot_id=$SnapshotId;moniker=$moniker;package_sid=$sid
    policy_revision=[int]$policy.revision;ancestors=@($ancestors)
 }
 Save-Atomic $script:journal $j $false
 try {
   $sidObj=[Security.Principal.SecurityIdentifier]::new($sid)
   foreach($item in $ancestors){
      $p=[string]$item.path
      if((Sddl $p) -cne [string]$item.sddl){throw 'TURBO_D3_PARENT_CHANGED'}
      $acl=Get-Acl -LiteralPath $p
      $rule=[Security.AccessControl.FileSystemAccessRule]::new(
         $sidObj,[Security.AccessControl.FileSystemRights]::Traverse,
         [Security.AccessControl.InheritanceFlags]::None,
         [Security.AccessControl.PropagationFlags]::None,
         [Security.AccessControl.AccessControlType]::Allow)
      [void]$acl.AddAccessRule($rule)
      Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
   }
   foreach($entry in @($snap.entries | Sort-Object {([string]$_.path).Length} -Descending)){
      Check-Identity $entry
      $p=[string]$entry.path
      if((Sddl $p) -cne [string]$entry.sddl){throw 'TURBO_D3_SNAPSHOT_STALE'}
      $acl=Get-Acl -LiteralPath $p
      $flags=if([bool]$entry.directory){[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{[Security.AccessControl.InheritanceFlags]::None}
      $rule=[Security.AccessControl.FileSystemAccessRule]::new(
           $sidObj,[Security.AccessControl.FileSystemRights]::Modify,$flags,
           [Security.AccessControl.PropagationFlags]::None,
           [Security.AccessControl.AccessControlType]::Allow)
      [void]$acl.AddAccessRule($rule)
      Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
      & icacls.exe $p '/setintegritylevel' $(if([bool]$entry.directory){'(OI)(CI)L'}else{'L'}) | Out-Null
      if($LASTEXITCODE -ne 0){throw 'TURBO_D3_LOW_IL_FAILED'}
   }
   $j.status='ACTIVE'
   Save-Atomic $script:journal $j $false
   Write-Host 'TURBO_D3_EXISTING_ROOT_PERMISSIONS_STAGED'
   Write-Host 'TURBO_D3_EXECUTION_BROKER_NOT_YET_ACTIVATED'
 } catch {
   $err=$_
   try{Revoke-Tree $j}catch{Write-Warning ('TURBO_D3_ROLLBACK_FAILED:'+ $_.Exception.Message)}
   throw $err
 }
}
finally{if($held){try{$mutex.ReleaseMutex()}catch{}};$mutex.Dispose()}
