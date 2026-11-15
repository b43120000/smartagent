# Git63: Auto install with no per-tool UAC. This *host* controller does NOT
# download, unpack or run untrusted binaries. A distinct AppContainer does.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ProjectRoot,
 [Parameter(Mandatory=$true)][string]$Tool,
 [Parameter(Mandatory=$true)][string]$Version,
 [string]$SourceSpecBase64=''
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
if($Tool -cnotmatch '^[a-z][a-z0-9_-]{0,31}$' -or
   $Version -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$'){
 throw 'GIT63_INSTALL_SELECTION_INVALID'
}
$store=Join-Path $root 'localdata\toolchains'
$capture=Join-Path $root 'localdata\runtime\toolchain_capture'
$secure=Join-Path $root 'localdata\secure\windows_security'
$catalogFile=Join-Path $root 'config\managed_toolchains_catalog.json'
$manifestFile=Join-Path $root 'config\protocol_manifest.json'
$manifest=Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
$expected=[string]$manifest.core_files.'config/managed_toolchains_catalog.json'
if($expected -notmatch '^[0-9a-fA-F]{64}$' -or
   (Get-FileHash -LiteralPath $catalogFile -Algorithm SHA256).Hash -ine $expected){
 throw 'GIT63_CATALOG_MANIFEST_PIN_MISMATCH'
}
$catalog=Get-Content -LiteralPath $catalogFile -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
if($catalog.schema -ne 'SMARTAGENT_GIT63_CATALOG_V1'){throw 'GIT63_CATALOG_SCHEMA_INVALID'}
if($SourceSpecBase64){
 if($SourceSpecBase64.Length -gt 16384 -or
    $SourceSpecBase64 -notmatch '^[A-Za-z0-9+/=]+$'){throw 'GIT63_SOURCE_SPEC_INVALID'}
 try{
  $entry=[Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($SourceSpecBase64)) | ConvertFrom-Json -ErrorAction Stop
 }catch{throw 'GIT63_SOURCE_SPEC_INVALID'}
 $fieldNames=(@($entry.PSObject.Properties.Name | Sort-Object) -join ',')
 if($fieldNames -cne 'archive_sha256,archive_type,checksum_url,executables,url'){
  throw 'GIT63_SOURCE_SPEC_FIELDS_INVALID'
 }
}else{
 $selected=@($catalog.packages | Where-Object {$_.tool -ceq $Tool -and $_.version -ceq $Version})
 if($selected.Count -ne 1){throw 'GIT63_DYNAMIC_SOURCE_REQUIRED'}
 $entry=$selected[0]
}
if($entry.archive_type -cne 'zip' -or [string]$entry.archive_sha256 -notmatch '^[0-9a-fA-F]{64}$'){
 throw 'GIT63_SOURCE_HASH_UNTRUSTED'
}
$url=[Uri]([string]$entry.url)
$allow=@('github.com','api.adoptium.net','dl.google.com','storage.googleapis.com','services.gradle.org','download.eclipse.org')
if($url.Scheme -cne 'https' -or $url.Host.ToLowerInvariant() -notin $allow -or
   $url.UserInfo -or $url.Fragment -or ([string]$url).Length -gt 2048){
 throw 'GIT63_SOURCE_URL_NOT_TRUSTED'
}
$checksumUrl=''
if($SourceSpecBase64){
 $check=[Uri]([string]$entry.checksum_url)
 if($check.Scheme -cne 'https' -or $check.Host.ToLowerInvariant() -ne $url.Host.ToLowerInvariant() -or
    $check.UserInfo -or $check.Fragment -or ([string]$check).Length -gt 2048){
  throw 'GIT63_SOURCE_CHECKSUM_ORIGIN_UNTRUSTED'
 }
 $checksumUrl=[string]$check
}
$aliases=@()
foreach($property in $entry.executables.PSObject.Properties){
 $alias=[string]$property.Name
 $relative=[string]$property.Value
 if($alias -cnotmatch '^[a-z][a-z0-9_-]{0,31}$' -or
   $relative -notmatch '^[A-Za-z0-9_.+-]+(?:[\\/][A-Za-z0-9_.+-]+)*\(?:exe|bat|cmd)$' -or
   $relative -match '(?:^|[\\/])\.\.(?:[\\/]|$)'){
  throw 'GIT63_CATALOG_EXECUTABLE_UNTRUSTED'
 }
 $aliases+=,[ordered]@{alias=$alias;relative=$relative}
}
if($aliases.Count -lt 1 -or $aliases.Count -gt 32){throw 'GIT63_EXECUTABLE_LIST_INVALID'}
$sourceProbe=Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs'
$protectedProbe=Join-Path $secure 'turbo_broker_code\turbo_appcontainer_probe.cs'
if((Get-FileHash -LiteralPath $sourceProbe -Algorithm SHA256).Hash -ine
   (Get-FileHash -LiteralPath $protectedProbe -Algorithm SHA256).Hash){
 throw 'GIT63_PROTECTED_NATIVE_PROBE_MISMATCH'
}
if(-not ('SmartAgentGit47AppContainerProbe' -as [type])){
 Add-Type -Path $protectedProbe -ErrorAction Stop
}
$sha=[Security.Cryptography.SHA256]::Create()
try{$id=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToUpperInvariant()))).Replace('-','').Substring(0,24)}
finally{$sha.Dispose()}
$moniker='SmartAgent.Git63.Tools.'+$id
$sid=[SmartAgentGit47AppContainerProbe]::DeriveSid($moniker)
if($sid -notmatch '^S-1-15-2-[0-9-]+$'){throw 'GIT63_INSTALLER_SID_UNAVAILABLE'}
if(-not (Test-Path -LiteralPath $store -PathType Container) -or
   -not (Test-Path -LiteralPath $capture -PathType Container)){
 throw 'GIT63_INSTALLER_NOT_PROVISIONED'
}
function Quote([string]$value){return "'"+$value.Replace("'","''")+"'";}
$aliasesJson=(ConvertTo-Json -InputObject @($aliases) -Compress -Depth 5)
$aliasesB64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($aliasesJson))
$denial=Join-Path $secure ('.git63-denial-'+[Guid]::NewGuid().ToString('N'))
$settings=@(
 ('$StoreRoot='+(Quote $store)),
 ('$Tool='+(Quote $Tool)),
 ('$Version='+(Quote $Version)),
 ('$SourceUrl='+(Quote ([string]$url))),
 ('$ArchiveSHA256='+(Quote ([string]$entry.archive_sha256))),
 ('$ChecksumUrl='+(Quote $checksumUrl)),
 ('$AliasesJsonBase64='+(Quote $aliasesB64)),
 ('$OutsideDenialProbe='+(Quote $denial))
)
$child=([string]::Join([Environment]::NewLine,[string[]]$settings))+
       [Environment]::NewLine+
       [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'git63_toolchain_install_child.ps1'))
$native=[SmartAgentGit47AppContainerProbe]::LaunchPowerShellCaptured(
 $moniker,$child,$capture,[uint32]300000,[int]262144,'') | ConvertFrom-Json -ErrorAction Stop
if($native.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
   $native.sandbox_identity_verified -ne $true -or
   $native.internet_client_token_verified -ne $true -or
   $native.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
   [int]$native.exit_code -ne 0){
 throw ('GIT63_INSTALLER_NATIVE_PROCESS_FAILED:exit='+[int]$native.exit_code)
}
$stdout=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$native.stdout_base64))
$receipt=$stdout.Trim() | ConvertFrom-Json -ErrorAction Stop
if($receipt.schema -ne 'SMARTAGENT_GIT63_INSTALL_RECEIPT_V1' -or
   $receipt.status -ne 'INSTALLED' -or
   $receipt.kernel_outside_write_denied -ne $true -or
   $receipt.tool -cne $Tool -or $receipt.version -cne $Version){
 throw 'GIT63_INSTALLER_RECEIPT_UNTRUSTED'
}
$receipt | Add-Member -NotePropertyName sandbox_backend -NotePropertyValue 'WIN32_APPCONTAINER'
$receipt | Add-Member -NotePropertyName sandbox_identity_verified -NotePropertyValue $true
$receipt | ConvertTo-Json -Compress -Depth 5
