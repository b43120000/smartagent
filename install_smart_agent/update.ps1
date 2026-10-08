[CmdletBinding()]

param(

    [Parameter(Mandatory = $true)][string]$SourceRoot,

    [Parameter(Mandatory = $true)][string]$InstallRoot,

    [switch]$ValidateOnly

)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Normal update is intentionally non-elevated. This script never changes ACL mode,
# requests administrator elevation, or opens a protected-updater lease. ACL OFF is an external
# precondition and is verified fail-closed before mutex, staging, backup, or copy.

$AclSchema = 'SMARTAGENT_ACL_MODE_V1'
$AclStateRelativePath = 'localdata\secure\windows_security\acl_mode.json'
$MutexName = 'Local\SmartAgentUpdate'
# TEMPORARY Phase 1 development whitelist. Remove when ACL on/off provisioning is complete.
$DevelopmentAclBypassTargets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'remoteagent\release\SmartAgentv2_1')
)
$script:Mutex = $null
$script:MutexOwned = $false
$script:StageRoot = $null
$script:BackupRoot = $null
$script:CreatedTargets = New-Object System.Collections.Generic.List[string]
$script:BackedUpTargets = New-Object System.Collections.Generic.List[string]

function Fail([string]$Code, [string]$Message, [int]$ExitCode = 1) {
    Write-Error ("{0}: {1}" -f $Code, $Message)
    exit $ExitCode
}
function Canonical-ExistingRoot([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path)) { Fail 'UPDATE_PATH_INVALID' "$Label is empty." 2 }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { Fail 'UPDATE_PATH_NOT_FOUND' "$Label does not exist: $Path" 2 }
    return [System.IO.Path]::GetFullPath((Get-Item -LiteralPath $Path -Force).FullName).TrimEnd('\')
}
function Normalize-Path([string]$Path) { return [System.IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Test-IsSameOrChild([string]$Candidate, [string]$Parent) {
    $c = (Normalize-Path $Candidate) + '\'; $p = (Normalize-Path $Parent) + '\'
    return $c.StartsWith($p,[System.StringComparison]::OrdinalIgnoreCase)
}
function Assert-SafeRoots([string]$Source,[string]$Target) {
    if ($Source -ieq $Target) { Fail 'UPDATE_PATH_OVERLAP' 'SourceRoot and InstallRoot are identical.' 2 }
    if (Test-IsSameOrChild $Target $Source) { Fail 'UPDATE_PATH_OVERLAP' 'InstallRoot is inside SourceRoot.' 2 }
    if (Test-IsSameOrChild $Source $Target) { Fail 'UPDATE_PATH_OVERLAP' 'SourceRoot is inside InstallRoot.' 2 }
    foreach($p in @($Source,$Target)) { $root=[System.IO.Path]::GetPathRoot($p).TrimEnd('\'); if($p.TrimEnd('\') -ieq $root){ Fail 'UPDATE_PATH_UNSAFE' "Refusing disk-root deployment path: $p" 2 } }
}
function Assert-ExpectedLayout([string]$Source,[string]$Target) {
    foreach($rel in @('source\smart_agent.py','install_smart_agent\update.ps1','update.bat','config\protocol_manifest.json')) { if(-not(Test-Path -LiteralPath (Join-Path $Source $rel) -PathType Leaf)){ Fail 'UPDATE_SOURCE_INVALID' "Missing source marker: $rel" 2 } }
    foreach($rel in @('source','install_smart_agent','localdata')) { if(-not(Test-Path -LiteralPath (Join-Path $Target $rel))){ Fail 'UPDATE_TARGET_INVALID' "Missing installed-runtime marker: $rel" 2 } }
}
function Assert-NoReparseRoot([string]$Path,[string]$Label) { $item=Get-Item -LiteralPath $Path -Force; if(($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)-ne 0){ Fail 'UPDATE_REPARSE_BLOCKED' "$Label is a reparse point: $Path" 2 } }
function Get-JsonProperty($Object,[string]$Name) { $prop=$Object.PSObject.Properties[$Name]; if($null -eq $prop){return $null}; return $prop.Value }
function Assert-AclOff([string]$Target) {
    $statePath=Join-Path $Target $AclStateRelativePath
    if(-not(Test-Path -LiteralPath $statePath -PathType Leaf)){
        if($DevelopmentAclBypassTargets -icontains $Target){
            Write-Warning ("[SmartAgent Update] TEMPORARY DEV ACL bypass: state missing for whitelisted target: {0}" -f $Target)
            return
        }
        Fail 'UPDATE_BLOCKED_ACL_STATE_MISSING' "ACL state missing: $statePath" 10
    }
    try { $state=(Get-Content -LiteralPath $statePath -Raw -Encoding UTF8)|ConvertFrom-Json } catch { Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' ("ACL state cannot be parsed: "+$_.Exception.Message) 10 }
    $schema=Get-JsonProperty $state 'schema'; $mode=Get-JsonProperty $state 'mode'; $boundRoot=Get-JsonProperty $state 'install_root'
    if([string]::IsNullOrWhiteSpace([string]$schema) -or $schema -ne $AclSchema){ Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state schema is missing or unsupported.' 10 }
    if([string]::IsNullOrWhiteSpace([string]$boundRoot)){ Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state install_root is missing.' 10 }
    try{$boundCanonical=Normalize-Path ([string]$boundRoot)}catch{Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state install_root is invalid.' 10}
    if($boundCanonical -ine $Target){ Fail 'UPDATE_BLOCKED_ACL_FOREIGN_INSTALL' "ACL state belongs to another installation: $boundCanonical" 10 }
    if([string]::IsNullOrWhiteSpace([string]$mode)){ Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state mode is missing.' 10 }
    if(([string]$mode).ToLowerInvariant() -ne 'off'){ Fail 'UPDATE_BLOCKED_ACL_ON' "Update requires ACL mode OFF; current mode=$mode" 10 }
    Write-Host '[SmartAgent Update] ACL gate: OFF'
}
function Test-ExcludedRelativePath([string]$RelativePath) {
    $r=$RelativePath.Replace('/','\').TrimStart('\'); $segments=$r.Split('\')
    foreach($s in $segments){if($s -in @('.git','.venv','.agents','__pycache__')){return $true}}
    if($segments.Count -gt 0 -and $segments[0] -ieq 'localdata'){return $true}
    # TEMPORARY deployment blacklist: ACL/update redesign is not ready for C runtime yet.
    if($r -in @(
        'update.bat',
        'install_smart_agent\update.ps1',
        'source\agent_core\workspace_access.py',
        'source\agent_core\workspace_manager.py',
        'source\WebAgent\tests\validate_acl_off_workspace_manager.py'
    )){return $true}
    if($r -ieq 'config\debug_config.json' -or $r -ieq 'config\update_manifest.json'){return $true}
    if([System.IO.Path]::GetExtension($r) -in @('.pyc','.pyo','.lnk')){return $true}
    return $false
}
function Get-DeployableFiles([string]$Root) {
    $prefixLength=$Root.TrimEnd('\').Length+1; $result=New-Object System.Collections.Generic.List[object]
    Get-ChildItem -LiteralPath $Root -File -Recurse -Force | ForEach-Object { $relative=$_.FullName.Substring($prefixLength); if(-not(Test-ExcludedRelativePath $relative)){ $result.Add([pscustomobject]@{Relative=$relative;Full=$_.FullName}) } }
    return $result.ToArray()
}
function Get-Sha256([string]$Path){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
function Ensure-Parent([string]$Path){$parent=Split-Path -Parent $Path;if(-not(Test-Path -LiteralPath $parent -PathType Container)){New-Item -ItemType Directory -Path $parent -Force|Out-Null}}
function Copy-SourceSet([object[]]$Files,[string]$DestinationRoot){foreach($f in $Files){$dst=Join-Path $DestinationRoot $f.Relative;Ensure-Parent $dst;Copy-Item -LiteralPath $f.Full -Destination $dst -Force}}
function Assert-FileSet([object[]]$Files,[string]$DestinationRoot,[string]$Phase){foreach($f in $Files){$dst=Join-Path $DestinationRoot $f.Relative;if(-not(Test-Path -LiteralPath $dst -PathType Leaf)){throw "$Phase missing destination file: $($f.Relative)"};if((Get-Sha256 $f.Full) -ne (Get-Sha256 $dst)){throw "$Phase hash mismatch: $($f.Relative)"}}}
function Assert-TargetParentsSafe([object[]]$Files,[string]$TargetRoot){foreach($f in $Files){$parts=$f.Relative.Replace('/','\').Split('\');$cursor=$TargetRoot;for($i=0;$i -lt ($parts.Count-1);$i++){$cursor=Join-Path $cursor $parts[$i];if(Test-Path -LiteralPath $cursor){$item=Get-Item -LiteralPath $cursor -Force;if(($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)-ne 0){throw "Target reparse point blocked: $cursor"}}}}}
function Backup-TargetFiles([object[]]$Files,[string]$TargetRoot,[string]$BackupRoot){foreach($f in $Files){$targetFile=Join-Path $TargetRoot $f.Relative;if(Test-Path -LiteralPath $targetFile -PathType Leaf){$backup=Join-Path $BackupRoot $f.Relative;Ensure-Parent $backup;Copy-Item -LiteralPath $targetFile -Destination $backup -Force;$script:BackedUpTargets.Add($f.Relative)}else{$script:CreatedTargets.Add($f.Relative)}}}
function Commit-Stage([object[]]$Files,[string]$StageRoot,[string]$TargetRoot){foreach($f in $Files){$src=Join-Path $StageRoot $f.Relative;$dst=Join-Path $TargetRoot $f.Relative;Ensure-Parent $dst;Copy-Item -LiteralPath $src -Destination $dst -Force}}
function Rollback-Target([string]$TargetRoot,[string]$BackupRoot){foreach($relative in $script:CreatedTargets){$dst=Join-Path $TargetRoot $relative;if(Test-Path -LiteralPath $dst -PathType Leaf){Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue}};foreach($relative in $script:BackedUpTargets){$backup=Join-Path $BackupRoot $relative;$dst=Join-Path $TargetRoot $relative;if(Test-Path -LiteralPath $backup -PathType Leaf){Ensure-Parent $dst;Copy-Item -LiteralPath $backup -Destination $dst -Force}}}

$source=Canonical-ExistingRoot $SourceRoot 'SourceRoot'
$target=Canonical-ExistingRoot $InstallRoot 'InstallRoot'
Assert-SafeRoots $source $target
Assert-NoReparseRoot $source 'SourceRoot'
Assert-NoReparseRoot $target 'InstallRoot'
Assert-ExpectedLayout $source $target
# Security invariant: gate before mutex, staging, backup, copy, or target mutation.
Assert-AclOff $target
$files=@(Get-DeployableFiles $source)
if($files.Count -eq 0){Fail 'UPDATE_SOURCE_EMPTY' 'No deployable files found.' 2}
Assert-TargetParentsSafe $files $target
if($ValidateOnly){Write-Host ("[SmartAgent Update] VALIDATION_OK files={0}" -f $files.Count);exit 0}
try {
    $script:Mutex=New-Object System.Threading.Mutex($false,$MutexName)
    try{$script:MutexOwned=$script:Mutex.WaitOne([TimeSpan]::FromSeconds(15))}catch [System.Threading.AbandonedMutexException]{$script:MutexOwned=$true}
    if(-not $script:MutexOwned){throw 'Another SmartAgent update is already running.'}
    $txn=[Guid]::NewGuid().ToString('N');$tempBase=Join-Path ([System.IO.Path]::GetTempPath()) ('SmartAgentUpdate-'+$txn);$script:StageRoot=Join-Path $tempBase 'stage';$script:BackupRoot=Join-Path $tempBase 'backup'
    New-Item -ItemType Directory -Path $script:StageRoot -Force|Out-Null;New-Item -ItemType Directory -Path $script:BackupRoot -Force|Out-Null
    Write-Host ("[SmartAgent Update] Staging {0} files..." -f $files.Count)
    Copy-SourceSet $files $script:StageRoot;Assert-FileSet $files $script:StageRoot 'stage verification'
    Backup-TargetFiles $files $target $script:BackupRoot;Commit-Stage $files $script:StageRoot $target;Assert-FileSet $files $target 'target verification'
    Write-Host ("[SmartAgent Update] UPDATE_OK files={0}" -f $files.Count);exit 0
} catch {
    $failure=$_.Exception.Message
    if($null -ne $script:BackupRoot -and (Test-Path -LiteralPath $script:BackupRoot -PathType Container)){try{Rollback-Target $target $script:BackupRoot}catch{Write-Error ("UPDATE_ROLLBACK_FAILED: "+$_.Exception.Message)}}
    Write-Error ("UPDATE_FAILED_ROLLED_BACK: "+$failure);exit 20
} finally {
    if($null -ne $script:StageRoot){$tempBase=Split-Path -Parent $script:StageRoot;if(Test-Path -LiteralPath $tempBase){Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction SilentlyContinue}}
    if($script:MutexOwned -and $null -ne $script:Mutex){try{$script:Mutex.ReleaseMutex()}catch{}}
    if($null -ne $script:Mutex){$script:Mutex.Dispose()}
}
