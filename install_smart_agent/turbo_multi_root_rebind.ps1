# Git62: verified administrative policy-revision rebind for ALREADY enrolled
# independent AppContainer roots. A registry addition does not become a grant
# until each existing enrollment independently passes this native OS check.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ProjectRoot
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
 # Phase 1: verify ALL active journals and their OS child-token isolation
 # BEFORE modifying any journal revisions.
 $verified=@()
 foreach($file in $entries){
   $a=Read-Json $file.FullName
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
   $ws=[IO.Path]::GetFullPath([string]$a.workspace_root).TrimEnd('\')
   if(-not $root.StartsWith($ws+'\',[StringComparison]::OrdinalIgnoreCase)){
      throw 'GIT62_REBIND_ROOT_OUTSIDE_WORKSPACE'
   }
   $actualSid=[SmartAgentGit47AppContainerProbe]::DeriveSid([string]$a.moniker)
   if(-not $actualSid.Equals([string]$a.package_sid,[StringComparison]::OrdinalIgnoreCase)){
      throw 'GIT62_REBIND_SID_MISMATCH'
   }
   $sentinel=[IO.Path]::GetFullPath([string]$a.negative_sentinel_path).TrimEnd('\')
   if(-not ([IO.Path]::GetDirectoryName($sentinel)).Equals(
       [IO.Path]::GetDirectoryName($root),[StringComparison]::OrdinalIgnoreCase) -or
      [IO.Path]::GetFileName($sentinel) -notlike '.git50-os-denial-*' -or
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
      throw 'GIT62_REBIND_KERNEL_ATTESTATION_FAILED'
   }
   # Verify protected Broker executable identity remains identical.
   $entry=[IO.Path]::GetFullPath([string]$a.broker_entry).TrimEnd('\')
   $protectedEntry=Join-Path $sec 'turbo_broker_code\turbo_os_execute.ps1'
   if(-not (Equal $entry $protectedEntry) -or
      (Get-FileHash -LiteralPath $entry -Algorithm SHA256).Hash -ine
       [string]$a.broker_source_sha256){
      throw 'GIT62_REBIND_BROKER_HASH_CHANGED'
   }
   $verified+=,[pscustomobject]@{path=$file.FullName;record=$a}
 }
 # Phase 2: independent, protected, replace-atomic journal writes. A crash
 # before every Root is rebased leaves that Root fail-closed (stale revision).
 foreach($item in $verified){
   $a=$item.record
   if([int]$a.policy_revision -eq [int]$policy.revision){continue}
   $a.policy_revision=[int]$policy.revision
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
