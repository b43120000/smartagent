param(
    [string]$InstalledRoot = "",
    [string]$DestinationRoot = "",
    [switch]$DryRun,
    [switch]$NonInteractive,
    [switch]$LibraryOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Get-FileSha256 {
    param([Parameter(Mandatory=$true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Read-JsonFile {
    param([Parameter(Mandatory=$true)][string]$Path)
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Resolve-SyncRoot {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)][string]$Label)
    $resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path).TrimEnd('\')
    $item = Get-Item -LiteralPath $resolved -Force
    if (-not $item.PSIsContainer) { throw "sync_back_${Label}_not_directory:$resolved" }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "sync_back_${Label}_reparse_point_forbidden:$resolved" }
    if ([IO.Path]::GetPathRoot($resolved).TrimEnd('\').Equals($resolved, [StringComparison]::OrdinalIgnoreCase)) {
        throw "sync_back_${Label}_disk_root_forbidden:$resolved"
    }
    return $resolved
}

function Test-PathOverlap {
    param([string]$Left, [string]$Right)
    $a = [IO.Path]::GetFullPath($Left).TrimEnd('\')
    $b = [IO.Path]::GetFullPath($Right).TrimEnd('\')
    return $a.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or
        $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $b.StartsWith($a + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-ChildPath {
    param([string]$Root, [string]$Relative, [string]$Label)
    $normalized = ([string]$Relative).Replace('/', '\').TrimStart('\')
    $candidate = [IO.Path]::GetFullPath((Join-Path $Root $normalized))
    if (-not $candidate.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "sync_back_${Label}_path_escape:$Relative"
    }
    return $candidate
}

function Test-ExcludedRelativePath {
    param([string]$Relative)
    $value = ([string]$Relative).Replace('\', '/').TrimStart('/').ToLowerInvariant()
    if ($value -in @(
        'config/debug_config.json', 'config/protocol_manifest.json',
        'config/update_manifest.json'
    )) { return $true }
    $segments = $value.Split('/')
    if (@($segments | Where-Object { $_ -in @(
        '.git', '.venv', 'localdata', '.agents', '__pycache__',
        '.pytest_cache', 'browser_profile'
    ) }).Count) { return $true }
    return [IO.Path]::GetExtension($value) -in @('.pyc', '.pyo', '.lnk')
}

function Test-NewFileAllowed {
    param([string]$Relative)
    $value = ([string]$Relative).Replace('\', '/').TrimStart('/')
    if (Test-ExcludedRelativePath $value) { return $false }
    $lower = $value.ToLowerInvariant()
    if ($lower.StartsWith('source/') -or $lower.StartsWith('doc/') -or $lower.StartsWith('install_smart_agent/')) {
        return $true
    }
    return -not $lower.Contains('/') -and [IO.Path]::GetExtension($lower) -in @('.bat', '.ps1', '.py', '.md')
}

function Get-DeployableRelativeFiles {
    param([string]$Root)
    $output = New-Object System.Collections.Generic.List[string]
    $excludedDirectories = @(
        '.git', '.venv', 'localdata', '.agents', '__pycache__',
        '.pytest_cache', 'browser_profile'
    )
    $pending = New-Object 'System.Collections.Generic.Queue[string]'
    $pending.Enqueue($Root)
    while ($pending.Count) {
        $directory = $pending.Dequeue()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if ($item.PSIsContainer) {
                if ($item.Name.ToLowerInvariant() -in $excludedDirectories) { continue }
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
                $pending.Enqueue($item.FullName)
                continue
            }
            $relative = $item.FullName.Substring($Root.Length).TrimStart('\').Replace('\', '/')
            if (-not (Test-ExcludedRelativePath $relative)) { [void]$output.Add($relative) }
        }
    }
    return @($output | Sort-Object -Unique)
}

function Get-SyncBackPlan {
    param([string]$SourceRoot, [string]$DestinationRoot)
    $manifestPath = Join-Path $SourceRoot 'config\update_manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'sync_back_baseline_manifest_missing' }
    $manifest = Read-JsonFile $manifestPath
    if ([string]$manifest.schema -ne 'SMARTAGENT_UPDATE_MANIFEST_V1') { throw 'sync_back_baseline_manifest_invalid' }

    $expected = @{}
    $changes = New-Object System.Collections.Generic.List[object]
    $conflicts = New-Object System.Collections.Generic.List[object]
    $deletions = New-Object System.Collections.Generic.List[string]
    foreach ($property in $manifest.files.PSObject.Properties) {
        $relative = ([string]$property.Name).Replace('\', '/')
        $expected[$relative.ToLowerInvariant()] = ([string]$property.Value).ToLowerInvariant()
        if (Test-ExcludedRelativePath $relative) { continue }
        $sourcePath = Resolve-ChildPath $SourceRoot $relative 'installed'
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            [void]$deletions.Add($relative)
            continue
        }
        $baseHash = $expected[$relative.ToLowerInvariant()]
        $sourceHash = Get-FileSha256 $sourcePath
        if ($sourceHash -eq $baseHash) { continue }
        $destinationPath = Resolve-ChildPath $DestinationRoot $relative 'destination'
        $destinationHash = Get-FileSha256 $destinationPath
        $row = [pscustomobject]@{
            kind='MODIFIED'; relative=$relative; base_hash=$baseHash
            source_hash=$sourceHash; destination_hash=$destinationHash
        }
        if ($destinationHash -and $destinationHash -ne $baseHash -and $destinationHash -ne $sourceHash) {
            [void]$conflicts.Add($row)
        } elseif ($destinationHash -ne $sourceHash) {
            [void]$changes.Add($row)
        }
    }

    foreach ($relative in Get-DeployableRelativeFiles $SourceRoot) {
        if ($expected.ContainsKey($relative.ToLowerInvariant())) { continue }
        if (-not (Test-NewFileAllowed $relative)) { continue }
        $sourcePath = Resolve-ChildPath $SourceRoot $relative 'installed'
        $sourceHash = Get-FileSha256 $sourcePath
        $destinationPath = Resolve-ChildPath $DestinationRoot $relative 'destination'
        $destinationHash = Get-FileSha256 $destinationPath
        $row = [pscustomobject]@{
            kind='ADDED'; relative=$relative; base_hash=''
            source_hash=$sourceHash; destination_hash=$destinationHash
        }
        if ($destinationHash -and $destinationHash -ne $sourceHash) {
            [void]$conflicts.Add($row)
        } elseif ($destinationHash -ne $sourceHash) {
            [void]$changes.Add($row)
        }
    }
    return [pscustomobject]@{
        manifest=$manifest; changes=$changes.ToArray(); conflicts=$conflicts.ToArray()
        deletions=$deletions.ToArray()
    }
}

function Write-AtomicBytes {
    param([string]$Path, [byte[]]$Bytes)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.$PID.syncback.tmp"
    [IO.File]::WriteAllBytes($temporary, $Bytes)
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Assert-ManifestIntegrity {
    param([string]$Root)
    $protocolPath = Join-Path $Root 'config\protocol_manifest.json'
    $protocol = Read-JsonFile $protocolPath
    foreach ($property in $protocol.core_files.PSObject.Properties) {
        $candidate = Resolve-ChildPath $Root ([string]$property.Name) 'protocol'
        if ((Get-FileSha256 $candidate) -ne ([string]$property.Value).ToLowerInvariant()) {
            throw "sync_back_protocol_manifest_hash_mismatch:$($property.Name)"
        }
    }
    $updatePath = Join-Path $Root 'config\update_manifest.json'
    $update = Read-JsonFile $updatePath
    foreach ($property in $update.files.PSObject.Properties) {
        $candidate = Resolve-ChildPath $Root ([string]$property.Name) 'update'
        if ((Get-FileSha256 $candidate) -ne ([string]$property.Value).ToLowerInvariant()) {
            throw "sync_back_update_manifest_hash_mismatch:$($property.Name)"
        }
    }
    return $update
}

function Invoke-ReleaseFinalization {
    param([string]$InstalledRoot, [string]$DestinationRoot, [long]$BaselineSequence)
    $builder = Join-Path $DestinationRoot 'install_smart_agent\build_update_manifest.py'
    $protocolModule = Join-Path $DestinationRoot 'source\agent_core\protocol_manifest.py'
    if (-not (Test-Path -LiteralPath $builder -PathType Leaf) -or -not (Test-Path -LiteralPath $protocolModule -PathType Leaf)) {
        throw 'sync_back_destination_release_tools_missing'
    }
    $builderText = [IO.File]::ReadAllText($builder)
    $matches = [regex]::Matches($builderText, '(?m)^RELEASE_SEQUENCE = (\d+)\s*$')
    if ($matches.Count -ne 1) { throw 'sync_back_release_sequence_declaration_invalid' }
    $currentSequence = [long]$matches[0].Groups[1].Value
    $nextSequence = [Math]::Max($currentSequence, $BaselineSequence) + 1
    $updatedBuilder = [regex]::Replace(
        $builderText, '(?m)^RELEASE_SEQUENCE = \d+\s*$',
        "RELEASE_SEQUENCE = $nextSequence", 1
    )
    Write-AtomicBytes $builder ((New-Object Text.UTF8Encoding($false)).GetBytes($updatedBuilder))

    $python = Join-Path $DestinationRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $python = Join-Path $InstalledRoot '.venv\Scripts\python.exe'
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'sync_back_python_missing' }
    $oldPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
    $oldNoBytecode = [Environment]::GetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', 'Process')
    try {
        $env:PYTHONPATH = Join-Path $DestinationRoot 'source'
        $env:PYTHONDONTWRITEBYTECODE = '1'
        $protocolCommand = 'from pathlib import Path; from agent_core.protocol_manifest import write_protocol_manifest; write_protocol_manifest(Path.cwd())'
        Push-Location $DestinationRoot
        try {
            & $python -B -c $protocolCommand | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "sync_back_protocol_manifest_build_failed:exit=$LASTEXITCODE" }
            $releaseId = 'smartagent-v9-sync-back-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
            & $python -B $builder '--root' $DestinationRoot '--release-id' $releaseId | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "sync_back_update_manifest_build_failed:exit=$LASTEXITCODE" }
        } finally {
            Pop-Location
        }
    } finally {
        [Environment]::SetEnvironmentVariable('PYTHONPATH', $oldPythonPath, 'Process')
        [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', $oldNoBytecode, 'Process')
    }
    $validated = Assert-ManifestIntegrity $DestinationRoot
    if ([long]$validated.release_sequence -ne $nextSequence) { throw 'sync_back_release_sequence_finalize_mismatch' }
    return $validated
}

function Invoke-SyncBack {
    param([string]$InstalledRoot, [string]$DestinationRoot, [switch]$DryRun, [switch]$NonInteractive)
    $source = Resolve-SyncRoot $InstalledRoot 'installed'
    $destination = Resolve-SyncRoot $DestinationRoot 'destination'
    if (Test-PathOverlap $source $destination) { throw 'sync_back_roots_must_be_separate' }
    $plan = Get-SyncBackPlan $source $destination

    Write-Host "[SmartAgent Sync Back] Installed   : $source"
    Write-Host "[SmartAgent Sync Back] Destination : $destination"
    Write-Host "[SmartAgent Sync Back] Baseline    : $($plan.manifest.release_id) / $($plan.manifest.release_sequence)"
    foreach ($row in $plan.changes) { Write-Host ("  [{0}] {1}" -f $row.kind, $row.relative) }
    foreach ($row in $plan.conflicts) { Write-Host ("  [CONFLICT] {0}" -f $row.relative) -ForegroundColor Red }
    foreach ($relative in $plan.deletions) { Write-Host "  [DELETION-BLOCKED] $relative" -ForegroundColor Yellow }
    if (@($plan.conflicts).Count) { throw "sync_back_conflicts_detected:$(@($plan.conflicts).Count)" }
    if (@($plan.deletions).Count) { throw "sync_back_deletions_require_manual_review:$(@($plan.deletions).Count)" }
    if (-not @($plan.changes).Count) {
        Write-Host '[SmartAgent Sync Back] No installed-package code changes detected.'
        return [pscustomobject]@{ status='NO_CHANGES'; changed=0 }
    }
    if ($DryRun) {
        Write-Host '[SmartAgent Sync Back] DRY RUN only; no files were written.'
        return [pscustomobject]@{ status='DRY_RUN'; changed=@($plan.changes).Count }
    }
    if ($NonInteractive) { throw 'sync_back_confirmation_requires_interactive_session' }
    $confirmation = Read-Host '確認差異後，輸入 SYNC_BACK 才會寫回開發來源'
    if ($confirmation -cne 'SYNC_BACK') { throw 'sync_back_cancelled' }

    $transaction = Join-Path ([IO.Path]::GetTempPath()) ('smartagent-sync-back-' + [Guid]::NewGuid().ToString('N'))
    $stage = Join-Path $transaction 'stage'
    $backup = Join-Path $transaction 'backup'
    New-Item -ItemType Directory -Force -Path $stage,$backup | Out-Null
    $records = New-Object System.Collections.Generic.List[object]
    $recorded = @{}
    try {
        foreach ($row in $plan.changes) {
            $from = Resolve-ChildPath $source $row.relative 'installed'
            $staged = Resolve-ChildPath $stage $row.relative 'stage'
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $staged) | Out-Null
            Copy-Item -LiteralPath $from -Destination $staged -Force
            if ((Get-FileSha256 $staged) -ne $row.source_hash) { throw "sync_back_stage_hash_mismatch:$($row.relative)" }
        }
        $special = @(
            'install_smart_agent/build_update_manifest.py',
            'config/protocol_manifest.json', 'config/update_manifest.json'
        )
        foreach ($relative in @($plan.changes.relative) + $special) {
            $key = $relative.ToLowerInvariant()
            if ($recorded.ContainsKey($key)) { continue }
            $recorded[$key] = $true
            $target = Resolve-ChildPath $destination $relative 'destination'
            $exists = Test-Path -LiteralPath $target -PathType Leaf
            $backupPath = Resolve-ChildPath $backup $relative 'backup'
            if ($exists) {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupPath) | Out-Null
                Copy-Item -LiteralPath $target -Destination $backupPath -Force
            }
            [void]$records.Add([pscustomobject]@{ relative=$relative; existed=$exists; backup=$backupPath })
        }
        foreach ($row in $plan.changes) {
            $staged = Resolve-ChildPath $stage $row.relative 'stage'
            $target = Resolve-ChildPath $destination $row.relative 'destination'
            Write-AtomicBytes $target ([IO.File]::ReadAllBytes($staged))
            if ((Get-FileSha256 $target) -ne $row.source_hash) { throw "sync_back_destination_hash_mismatch:$($row.relative)" }
        }
        $release = Invoke-ReleaseFinalization $source $destination ([long]$plan.manifest.release_sequence)
        Write-Host "[SmartAgent Sync Back] Release finalized: $($release.release_id) / $($release.release_sequence)"
        return [pscustomobject]@{
            status='SYNCED'; changed=@($plan.changes).Count
            release_id=[string]$release.release_id; release_sequence=[long]$release.release_sequence
        }
    } catch {
        $failure = $_.Exception.Message
        $rollbackRecords = @($records.ToArray())
        [array]::Reverse($rollbackRecords)
        foreach ($record in $rollbackRecords) {
            $target = Resolve-ChildPath $destination $record.relative 'rollback'
            if ($record.existed) {
                Write-AtomicBytes $target ([IO.File]::ReadAllBytes([string]$record.backup))
            } elseif (Test-Path -LiteralPath $target -PathType Leaf) {
                Remove-Item -LiteralPath $target -Force
            }
        }
        throw "sync_back_failed_rolled_back:$failure"
    } finally {
        if (Test-Path -LiteralPath $transaction) { Remove-Item -LiteralPath $transaction -Recurse -Force }
    }
}

if ($LibraryOnly) { return }
if ([string]::IsNullOrWhiteSpace($InstalledRoot)) { $InstalledRoot = Join-Path $PSScriptRoot '..' }
if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    if ($NonInteractive) { throw 'sync_back_destination_required' }
    $DestinationRoot = Read-Host '請貼上要接收修改的開發來源 SmartAgentv1 完整路徑'
}
try {
    $result = Invoke-SyncBack -InstalledRoot $InstalledRoot -DestinationRoot ($DestinationRoot.Trim().Trim('"')) -DryRun:$DryRun -NonInteractive:$NonInteractive
    Write-Host "SMARTAGENT_SYNC_BACK_$($result.status) changed=$($result.changed)"
    exit 0
} catch {
    Write-Error $_
    exit 1
}
