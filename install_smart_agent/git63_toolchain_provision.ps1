# Git63 one-time admin provisioning; NOT an Agent tool or a per-install prompt.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProjectRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
 throw 'GIT63_PROVISION_ADMIN_REQUIRED'
}
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$store=Join-Path $root 'localdata\toolchains'
$capture=Join-Path $root 'localdata\runtime\toolchain_capture'
$secure=Join-Path $root 'localdata\secure\windows_security'
if(-not (Test-Path (Join-Path $secure 'security_profile.json') -PathType Leaf)){
 throw 'GIT63_PROVISION_SECURITY_PROFILE_MISSING'
}
if(-not ('SmartAgentGit47AppContainerProbe' -as [type])){
 Add-Type -Path (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -ErrorAction Stop
}
$hash=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -Algorithm SHA256).Hash
$protectedProbe=Join-Path $secure 'turbo_broker_code\turbo_appcontainer_probe.cs'
if(-not (Test-Path -LiteralPath $protectedProbe -PathType Leaf) -or
   (Get-FileHash -LiteralPath $protectedProbe -Algorithm SHA256).Hash -ine $hash){
 throw 'GIT63_PROVISION_PROTECTED_NATIVE_PROBE_MISMATCH'
}
function No-Reparse([string]$path){
 $p=[IO.Path]::GetFullPath($path).TrimEnd('\')
 while($p){
  $item=Get-Item -LiteralPath $p -Force -ErrorAction Stop
  if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){
   throw 'GIT63_PROVISION_REPARSE_FORBIDDEN'
  }
  $next=[IO.Path]::GetDirectoryName($p)
  if(-not $next -or $next -eq $p){break}
  $p=$next
 }
}
if(-not (Test-Path -LiteralPath $store)){
 New-Item -ItemType Directory -Path $store -Force -ErrorAction Stop | Out-Null
}
if(-not (Test-Path -LiteralPath $capture)){
 New-Item -ItemType Directory -Path $capture -Force -ErrorAction Stop | Out-Null
}
No-Reparse $store
No-Reparse $capture
$sha=[Security.Cryptography.SHA256]::Create()
try{$id=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToUpperInvariant()))).Replace('-','').Substring(0,24)}
finally{$sha.Dispose()}
$moniker='SmartAgent.Git63.Tools.'+$id
$sid=[SmartAgentGit47AppContainerProbe]::CreateProfile($moniker)
if($sid -notmatch '^S-1-15-2-[0-9-]+$'){throw 'GIT63_PROVISION_SID_INVALID'}
# Task AppContainers get RX only; the distinct installer SID gets Modify.
# Operator (current user) can create pinned I/O capture handles, not arbitrary
# task-root AppContainer processes. No grants to SmartAgent .venv or secure.
$userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $store '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*'+$userSid+':(OI)(CI)RX') ('*'+$sid+':(OI)(CI)M') '*S-1-15-2-1:(OI)(CI)RX' | Out-Null
if($LASTEXITCODE -ne 0){throw 'GIT63_PROVISION_STORE_ACL_FAILED'}
$userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $capture '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*'+$userSid+':(OI)(CI)M') ('*'+$sid+':(OI)(CI)M') | Out-Null
if($LASTEXITCODE -ne 0){throw 'GIT63_PROVISION_CAPTURE_ACL_FAILED'}
[ordered]@{schema='SMARTAGENT_GIT63_INSTALLER_PROVISION_V1';status='PROVISIONED';
 sandbox_backend='WIN32_APPCONTAINER';moniker=$moniker;installer_sid=$sid;
 toolchain_store=$store;capture_root=$capture;task_roots_can_write_store=$false} |
 ConvertTo-Json -Compress
