[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [switch]$ValidateOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deployment_baseline.ps1')

$ManifestSchema = 'SMARTAGENT_UPDATE_MANIFEST_V1'
$AclSchema = 'SMARTAGENT_ACL_MODE_V1'
$AclStateRelativePath = 'localdata\secure\windows_security\acl_mode.json'
$UpdateManifestRelativePath = 'config\update_manifest.json'
$MutexName = 'Local\SmartAgentManifestUpdate'
$PreservedDirectoryNames = @(
    '.git', '.venv', '.agents', '__pycache__', '.pytest_cache', 'localdata'
)
$PreservedFileExtensions = @('.pyc', '.pyo', '.lnk')
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
    $candidatePath = (Normalize-Path $Candidate) + '\'
    $parentPath = (Normalize-Path $Parent) + '\'
    return $candidatePath.StartsWith($parentPath, [System.StringComparison]::OrdinalIgnoreCase)
}
function Assert-SafeRoots([string]$Source, [string]$Target) {
    if ($Source -ieq $Target) { Fail 'UPDATE_PATH_OVERLAP' 'SourceRoot and InstallRoot are identical.' 2 }
    if (Test-IsSameOrChild $Target $Source) { Fail 'UPDATE_PATH_OVERLAP' 'InstallRoot is inside SourceRoot.' 2 }
    if (Test-IsSameOrChild $Source $Target) { Fail 'UPDATE_PATH_OVERLAP' 'SourceRoot is inside InstallRoot.' 2 }
    foreach ($path in @($Source, $Target)) {
        $diskRoot = [System.IO.Path]::GetPathRoot($path).TrimEnd('\')
        if ($path.TrimEnd('\') -ieq $diskRoot) { Fail 'UPDATE_PATH_UNSAFE' "Refusing disk-root deployment path: $path" 2 }
    }
}
function Assert-ExpectedLayout([string]$Source, [string]$Target) {
    foreach ($relative in @(
        'source\smart_agent.py', 'install_smart_agent\manifest_update.ps1',
        'update.bat', 'config\protocol_manifest.json', $UpdateManifestRelativePath
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $Source $relative) -PathType Leaf)) {
            Fail 'UPDATE_SOURCE_INVALID' "Missing source marker: $relative" 2
        }
    }
    foreach ($relative in @('source', 'install_smart_agent', 'localdata')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Target $relative))) {
            Fail 'UPDATE_TARGET_INVALID' "Missing installed-runtime marker: $relative" 2
        }
    }
}
function Assert-NoReparseRoot([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail 'UPDATE_REPARSE_BLOCKED' "$Label is a reparse point: $Path" 2
    }
}
function Get-JsonProperty($Object, [string]$Name) {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}
function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (($algorithm.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}
function Ensure-Parent([string]$Path) {
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}
function Convert-RelativePath([string]$RelativePath) {
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "update_manifest_relative_path_invalid:$RelativePath"
    }
    $normalized = $RelativePath.Replace('/', '\').TrimStart('\')
    $segments = @($normalized.Split('\'))
    if ($segments.Count -eq 0 -or $segments -contains '' -or $segments -contains '.' -or $segments -contains '..') {
        throw "update_manifest_relative_path_invalid:$RelativePath"
    }
    if ($normalized.Contains(':')) { throw "update_manifest_ads_path_forbidden:$RelativePath" }
    return $normalized
}
function Test-PreservedDirectory([string]$RelativePath) {
    $portable=$RelativePath.Replace('\','/').ToLowerInvariant()
    if ($portable -in @('.venv','.venv/portable-runtime','.venv/portable-git')) { return $false }
    foreach ($segment in @($RelativePath.Replace('/', '\').TrimStart('\').Split('\'))) {
        if ($PreservedDirectoryNames -icontains $segment) { return $true }
    }
    return $false
}
function Test-PreservedFile([string]$RelativePath) {
    if (Test-PortableDistributionPath $RelativePath) { return $false }
    if (Test-PrivateDeploymentPath $RelativePath) { return $true }
    $relative = $RelativePath.Replace('/', '\').TrimStart('\')
    if (Test-PreservedDirectory $relative) { return $true }
    if ($relative -ieq 'config\debug_config.json') { return $true }
    if ($PreservedFileExtensions -icontains [System.IO.Path]::GetExtension($relative)) { return $true }
    return $false
}
function Test-SourceIgnored([string]$RelativePath) {
    $relative = $RelativePath.Replace('/', '\').TrimStart('\')
    if (Test-PreservedFile $relative) { return $true }
    if ($relative -ieq $UpdateManifestRelativePath) { return $true }
    $first = @($relative.Split('\'))[0]
    if ($first.StartsWith('.update_', [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $false
}
function Assert-AclOff([string]$Target) {
    $statePath = Join-Path $Target $AclStateRelativePath
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        Fail 'UPDATE_BLOCKED_ACL_STATE_MISSING' "ACL state missing: $statePath" 10
    }
    try { $state = (Get-Content -LiteralPath $statePath -Raw -Encoding UTF8) | ConvertFrom-Json }
    catch { Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' ("ACL state cannot be parsed: " + $_.Exception.Message) 10 }
    $schema = Get-JsonProperty $state 'schema'
    $mode = Get-JsonProperty $state 'mode'
    $boundRoot = Get-JsonProperty $state 'install_root'
    if ([string]::IsNullOrWhiteSpace([string]$schema) -or $schema -ne $AclSchema) { Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state schema is missing or unsupported.' 10 }
    if ([string]::IsNullOrWhiteSpace([string]$boundRoot)) { Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state install_root is missing.' 10 }
    try { $boundCanonical = Normalize-Path ([string]$boundRoot) }
    catch { Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state install_root is invalid.' 10 }
    if ($boundCanonical -ine $Target) { Fail 'UPDATE_BLOCKED_ACL_FOREIGN_INSTALL' "ACL state belongs to another installation: $boundCanonical" 10 }
    if ([string]::IsNullOrWhiteSpace([string]$mode)) { Fail 'UPDATE_BLOCKED_ACL_STATE_INVALID' 'ACL state mode is missing.' 10 }
    if (([string]$mode).ToLowerInvariant() -ne 'off') { Fail 'UPDATE_BLOCKED_ACL_ON' "Update requires ACL mode OFF; current mode=$mode" 10 }
    Write-Host '[SmartAgent Update] ACL gate: OFF'
}
function Get-SafeTreeFiles([string]$Root, [switch]$SourceInventory) {
    $rootPath = Normalize-Path $Root
    $result = New-Object System.Collections.Generic.List[object]
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($rootPath)
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force)) {
            $relative = $item.FullName.Substring($rootPath.Length + 1).Replace('/', '\')
            if ($item.PSIsContainer) {
                if (Test-PreservedDirectory $relative) { continue }
                $first = @($relative.Split('\'))[0]
                if ($SourceInventory -and $first.StartsWith('.update_', [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "update_reparse_point_forbidden:$relative" }
                $stack.Push($item.FullName)
                continue
            }
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "update_reparse_point_forbidden:$relative" }
            if ($SourceInventory) {
                if (Test-SourceIgnored $relative) { continue }
            } elseif ((Test-PreservedFile $relative) -or $relative -ieq $UpdateManifestRelativePath) {
                continue
            }
            $result.Add([pscustomobject]@{ Relative = $relative; Full = $item.FullName })
        }
    }
    return @($result.ToArray() | Sort-Object Relative)
}
function Get-ManifestFiles([string]$Source) {
    $manifestPath = Join-Path $Source $UpdateManifestRelativePath
    try { $manifest = (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8) | ConvertFrom-Json }
    catch { throw "update_manifest_unreadable:$($_.Exception.Message)" }
    if ((Get-JsonProperty $manifest 'schema') -ne $ManifestSchema) { throw 'update_manifest_schema_mismatch' }
    if ([string]::IsNullOrWhiteSpace([string](Get-JsonProperty $manifest 'release_id')) -or [long](Get-JsonProperty $manifest 'release_sequence') -le 0) { throw 'update_manifest_release_identity_invalid' }
    $fileMap = Get-JsonProperty $manifest 'files'
    if ($null -eq $fileMap) { throw 'update_manifest_files_missing' }
    $protocolPath = Join-Path $Source 'config\protocol_manifest.json'
    try { $protocol = (Get-Content -LiteralPath $protocolPath -Raw -Encoding UTF8) | ConvertFrom-Json } catch { throw 'protocol_manifest_unreadable' }
    if ((Get-JsonProperty $protocol 'schema') -ne 'SMARTAGENT_PROTOCOL_MANIFEST_V1' -or [int](Get-JsonProperty $protocol 'protocol_version') -le 0) { throw 'protocol_manifest_schema_invalid' }
    $protocolCore = Get-JsonProperty $protocol 'core_files'
    if ($null -eq $protocolCore -or @($protocolCore.PSObject.Properties).Count -eq 0) { throw 'protocol_manifest_core_files_missing' }
    $protocolHash = Get-Sha256 $protocolPath
    if (([string](Get-JsonProperty $fileMap 'config/protocol_manifest.json')).ToLowerInvariant() -ne $protocolHash) { throw 'update_manifest_protocol_manifest_hash_mismatch' }
    $provenancePath = Join-Path $Source 'config\source_build_provenance.json'
    try { $provenance = (Get-Content -LiteralPath $provenancePath -Raw -Encoding UTF8) | ConvertFrom-Json } catch { throw 'source_build_provenance_unreadable' }
    $provenanceHash = Get-Sha256 $provenancePath
    if ((Get-JsonProperty $provenance 'schema') -ne 'SMARTAGENT_SOURCE_BUILD_PROVENANCE_V1') { throw 'source_build_provenance_schema_invalid' }
    if ((Get-JsonProperty $provenance 'source_tree_clean_at_build') -ne $true) { throw 'source_build_tree_not_clean' }
    $sourceCommit = [string](Get-JsonProperty $provenance 'source_commit')
    $sourceTreeSha = [string](Get-JsonProperty $provenance 'source_tree_sha')
    if ($sourceCommit -notmatch '^[0-9a-fA-F]{40}$' -or $sourceTreeSha -notmatch '^[0-9a-fA-F]{40}$') { throw 'source_build_provenance_git_identity_invalid' }
    if (([string](Get-JsonProperty $provenance 'protocol_manifest_sha256')).ToLowerInvariant() -ne $protocolHash) { throw 'source_build_provenance_protocol_hash_mismatch' }
    if ([long](Get-JsonProperty $provenance 'release_sequence') -ne [long](Get-JsonProperty $manifest 'release_sequence')) { throw 'source_build_provenance_sequence_mismatch' }
    if (([string](Get-JsonProperty $fileMap 'config/source_build_provenance.json')).ToLowerInvariant() -ne $provenanceHash) { throw 'update_manifest_source_provenance_hash_mismatch' }
    $seen = @{}
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($property in @($fileMap.PSObject.Properties)) {
        $relative = Convert-RelativePath ([string]$property.Name)
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "update_manifest_duplicate_path:$relative" }
        $seen[$key] = $true
        if (Test-SourceIgnored $relative) { throw "update_manifest_preserved_path_forbidden:$relative" }
        $expected = ([string]$property.Value).ToLowerInvariant()
        if ($expected -notmatch '^[0-9a-f]{64}$') { throw "update_manifest_hash_invalid:$relative" }
        $full = [System.IO.Path]::GetFullPath((Join-Path $Source $relative))
        if (-not (Test-IsSameOrChild $full $Source)) { throw "update_manifest_path_escape:$relative" }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "update_manifest_source_missing:$relative" }
        if ((Get-Sha256 $full) -ne $expected) { throw "update_manifest_source_hash_mismatch:$relative" }
        $result.Add([pscustomobject]@{ Relative = $relative; Full = $full; Sha256 = $expected })
    }
    if ($result.Count -eq 0) { throw 'update_manifest_empty' }
    foreach ($property in @($protocolCore.PSObject.Properties)) {
        $relative = Convert-RelativePath ([string]$property.Name)
        $expectedCoreSha = ([string]$property.Value).ToLowerInvariant()
        if ($expectedCoreSha -notmatch '^[0-9a-f]{64}$') { throw "protocol_manifest_core_hash_invalid:$relative" }
        $corePath = Join-Path $Source $relative
        if (-not (Test-Path -LiteralPath $corePath -PathType Leaf)) { throw "protocol_manifest_core_file_missing:$relative" }
        if ((Get-Sha256 $corePath) -ne $expectedCoreSha) { throw "protocol_manifest_core_hash_mismatch:$relative" }
        if (([string](Get-JsonProperty $fileMap $relative)).ToLowerInvariant() -ne $expectedCoreSha) { throw "update_manifest_core_hash_disagreement:$relative" }
    }
    $inventory = @(Get-SafeTreeFiles $Source -SourceInventory)
    $unlisted = @($inventory | Where-Object { -not $seen.ContainsKey($_.Relative.ToLowerInvariant()) } | ForEach-Object { $_.Relative })
    $inventoryKeys = @{}; foreach ($item in $inventory) { $inventoryKeys[$item.Relative.ToLowerInvariant()] = $true }
    $missing = @($result | Where-Object { -not $inventoryKeys.ContainsKey($_.Relative.ToLowerInvariant()) } | ForEach-Object { $_.Relative })
    if ($unlisted.Count -gt 0 -or $missing.Count -gt 0) {
        throw ("update_manifest_inventory_mismatch:unlisted={0};missing={1}" -f ($unlisted -join ','), ($missing -join ','))
    }
    return [pscustomobject]@{
        Manifest = $manifest
        ManifestPath = $manifestPath
        Files = @($result.ToArray() | Sort-Object Relative)
        Keys = $seen
    }
}
function Assert-TargetParentsSafe([object[]]$Files, [string]$TargetRoot) {
    foreach ($file in $Files) {
        $parts = $file.Relative.Replace('/', '\').Split('\'); $cursor = $TargetRoot
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $cursor = Join-Path $cursor $parts[$index]
            if (Test-Path -LiteralPath $cursor) {
                $item = Get-Item -LiteralPath $cursor -Force
                if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "update_target_reparse_point_forbidden:$cursor" }
            }
        }
    }
}
function Copy-SourceSet([object[]]$Files, [string]$DestinationRoot) {
    foreach ($file in $Files) {
        $destination = Join-Path $DestinationRoot $file.Relative
        Ensure-Parent $destination
        Copy-Item -LiteralPath $file.Full -Destination $destination -Force
    }
}
function Assert-FileSet([object[]]$Files, [string]$DestinationRoot, [string]$Phase) {
    foreach ($file in $Files) {
        $destination = Join-Path $DestinationRoot $file.Relative
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) { throw "$Phase missing destination file: $($file.Relative)" }
        if ($file.Sha256 -ne (Get-Sha256 $destination)) { throw "$Phase hash mismatch: $($file.Relative)" }
    }
}
function Backup-TargetFiles([object[]]$Files, [string]$TargetRoot, [string]$BackupRoot) {
    foreach ($file in $Files) {
        $targetFile = Join-Path $TargetRoot $file.Relative
        if (Test-Path -LiteralPath $targetFile -PathType Leaf) {
            $backup = Join-Path $BackupRoot $file.Relative
            Ensure-Parent $backup
            Copy-Item -LiteralPath $targetFile -Destination $backup -Force
            if (-not $script:BackedUpTargets.Contains($file.Relative)) { $script:BackedUpTargets.Add($file.Relative) }
        } elseif (-not $script:CreatedTargets.Contains($file.Relative)) {
            $script:CreatedTargets.Add($file.Relative)
        }
    }
}
function Commit-Stage([object[]]$Files, [string]$StageRoot, [string]$TargetRoot) {
    foreach ($file in $Files) {
        $sourceFile = Join-Path $StageRoot $file.Relative; $destination = Join-Path $TargetRoot $file.Relative
        Ensure-Parent $destination
        Copy-Item -LiteralPath $sourceFile -Destination $destination -Force
    }
}
function Remove-StaleFiles([object[]]$Files, [string]$TargetRoot) {
    foreach ($file in $Files) {
        $targetFile = Join-Path $TargetRoot $file.Relative
        if (-not (Test-IsSameOrChild $targetFile $TargetRoot)) { throw "update_stale_path_escape:$($file.Relative)" }
        if (Test-Path -LiteralPath $targetFile -PathType Leaf) {
            if ($file.Sha256 -and (Get-Sha256 $targetFile) -ne $file.Sha256) { throw "update_stale_file_changed:$($file.Relative)" }
            Remove-Item -LiteralPath $targetFile -Force
        }
    }
}
function Rollback-Target([string]$TargetRoot, [string]$BackupRoot) {
    foreach ($relative in $script:CreatedTargets) {
        $destination = Join-Path $TargetRoot $relative
        if (Test-Path -LiteralPath $destination -PathType Leaf) { Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue }
    }
    foreach ($relative in $script:BackedUpTargets) {
        $backup = Join-Path $BackupRoot $relative; $destination = Join-Path $TargetRoot $relative
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            Ensure-Parent $destination
            Copy-Item -LiteralPath $backup -Destination $destination -Force
        }
    }
}

$source = Canonical-ExistingRoot $SourceRoot 'SourceRoot'
$target = Canonical-ExistingRoot $InstallRoot 'InstallRoot'
Assert-SafeRoots $source $target
Assert-NoReparseRoot $source 'SourceRoot'
Assert-NoReparseRoot $target 'InstallRoot'
Assert-ExpectedLayout $source $target
Assert-AclOff $target
$release = Get-ManifestFiles $source
$files = @($release.Files)
$targetInventory = @(Get-SafeTreeFiles $target)
$staleFiles = @($targetInventory | Where-Object { -not $release.Keys.ContainsKey($_.Relative.ToLowerInvariant()) })
$manifestTransfer = [pscustomobject]@{
    Relative = $UpdateManifestRelativePath
    Full = $release.ManifestPath
    Sha256 = Get-Sha256 $release.ManifestPath
}
$transferFiles = @($files + $manifestTransfer)
Assert-TargetParentsSafe $transferFiles $target
if ($ValidateOnly) {
    Write-Host ("[SmartAgent Update] VALIDATION_OK files={0} stale={1} release_id={2}" -f $files.Count, $staleFiles.Count, (Get-JsonProperty $release.Manifest 'release_id'))
    exit 0
}
try {
    $script:Mutex = New-Object System.Threading.Mutex($false, $MutexName)
    try { $script:MutexOwned = $script:Mutex.WaitOne([TimeSpan]::FromSeconds(15)) }
    catch [System.Threading.AbandonedMutexException] { $script:MutexOwned = $true }
    if (-not $script:MutexOwned) { throw 'Another SmartAgent update is already running.' }
    $transactionId = [Guid]::NewGuid().ToString('N')
    $receiptRelative = 'localdata\update\deployment_baseline.json'
    $null = Get-DeploymentBaselinePath $target
    $tempBase = Join-Path ([System.IO.Path]::GetTempPath()) ('SmartAgentUpdate-' + $transactionId)
    $script:StageRoot = Join-Path $tempBase 'stage'; $script:BackupRoot = Join-Path $tempBase 'backup'
    New-Item -ItemType Directory -Path $script:StageRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $script:BackupRoot -Force | Out-Null
    Write-Host ("[SmartAgent Update] Staging manifest release {0}: files={1}, stale={2}" -f (Get-JsonProperty $release.Manifest 'release_id'), $files.Count, $staleFiles.Count)
    Copy-SourceSet $transferFiles $script:StageRoot
    Assert-FileSet $transferFiles $script:StageRoot 'stage verification'
    Backup-TargetFiles $transferFiles $target $script:BackupRoot
    Backup-TargetFiles @([pscustomobject]@{Relative=$receiptRelative}) $target $script:BackupRoot
    Backup-TargetFiles $staleFiles $target $script:BackupRoot
    Commit-Stage $transferFiles $script:StageRoot $target
    Remove-StaleFiles $staleFiles $target
    Assert-FileSet $transferFiles $target 'target verification'
    $remainingStale = @(Get-SafeTreeFiles $target | Where-Object { -not $release.Keys.ContainsKey($_.Relative.ToLowerInvariant()) })
    if ($remainingStale.Count -gt 0) { throw ('target verification stale files remain: ' + (($remainingStale | ForEach-Object { $_.Relative }) -join ',')) }
    $baselineHashes = @{}
    foreach ($file in $files) { $baselineHashes[$file.Relative] = $file.Sha256 }
    Save-DeploymentBaseline $target $source $baselineHashes ([string](Get-JsonProperty $release.Manifest 'release_id')) ([long](Get-JsonProperty $release.Manifest 'release_sequence'))
    Write-Host ("[SmartAgent Update] UPDATE_OK files={0} removed={1} release_id={2}" -f $files.Count, $staleFiles.Count, (Get-JsonProperty $release.Manifest 'release_id'))
    exit 0
} catch {
    $failure = $_.Exception.Message
    if ($null -ne $script:BackupRoot -and (Test-Path -LiteralPath $script:BackupRoot -PathType Container)) {
        try { Rollback-Target $target $script:BackupRoot }
        catch { Write-Error ('UPDATE_ROLLBACK_FAILED: ' + $_.Exception.Message) }
    }
    Write-Error ('UPDATE_FAILED_ROLLED_BACK: ' + $failure)
    exit 20
} finally {
    if ($null -ne $script:StageRoot) {
        $tempBase = Split-Path -Parent $script:StageRoot
        if (Test-Path -LiteralPath $tempBase) { Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction SilentlyContinue }
    }
    if ($script:MutexOwned -and $null -ne $script:Mutex) { try { $script:Mutex.ReleaseMutex() } catch {} }
    if ($null -ne $script:Mutex) { $script:Mutex.Dispose() }
}
