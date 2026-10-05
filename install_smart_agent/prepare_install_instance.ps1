[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$LocalData = Join-Path $ProjectRoot 'localdata'
$CurrentControllerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Read-JsonObject([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "install_instance_state_invalid:$Path" }
}

function Resolve-StateRoot([object]$State, [string]$PropertyName) {
    if ($null -eq $State) { return '' }
    if (-not ($State.PSObject.Properties.Name -contains $PropertyName)) { return '' }
    $value = [string]$State.$PropertyName
    if ([string]::IsNullOrWhiteSpace($value)) { return '' }
    try { return [IO.Path]::GetFullPath($value).TrimEnd('\') }
    catch { throw "install_instance_bound_root_invalid:$PropertyName" }
}

function Write-JsonNoBom([string]$Path, [object]$Value) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.tmp"
    [IO.File]::WriteAllText(
        $temporary,
        ($Value | ConvertTo-Json -Depth 8),
        (New-Object Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '') }
        finally { $algorithm.Dispose() }
    } finally { $stream.Dispose() }
}

function Get-BoundRoots {
    $roots = @()
    $instance = Read-JsonObject (Join-Path $LocalData 'metadata\install_instance.json')
    $instanceRoot = Resolve-StateRoot $instance 'install_root'
    if ($instanceRoot) { $roots += $instanceRoot }
    $mode = Read-JsonObject (Join-Path $LocalData 'secure\windows_security\acl_mode.json')
    $modeRoot = Resolve-StateRoot $mode 'install_root'
    if ($modeRoot) { $roots += $modeRoot }
    $bootstrap = Read-JsonObject (Join-Path $LocalData 'metadata\bootstrap_state.json')
    $bootstrapRoot = Resolve-StateRoot $bootstrap 'project_root'
    if ($bootstrapRoot) { $roots += $bootstrapRoot }
    return @($roots | Select-Object -Unique)
}

function Resolve-WritableBackupBase {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace([string]$env:LOCALAPPDATA)) {
        $candidates += (Join-Path ([string]$env:LOCALAPPDATA) 'SmartAgent\install_backups')
    }
    $candidates += (Join-Path ([IO.Path]::GetTempPath()) 'SmartAgent\install_backups')
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        try {
            New-Item -ItemType Directory -Force -Path $candidate -ErrorAction Stop | Out-Null
            $probe = Join-Path $candidate ('.write-probe-' + [Guid]::NewGuid().ToString('N'))
            [IO.File]::WriteAllText($probe, 'probe', (New-Object Text.UTF8Encoding($false)))
            Remove-Item -LiteralPath $probe -Force
            return [IO.Path]::GetFullPath($candidate)
        } catch { }
    }
    throw 'install_instance_backup_location_unavailable'
}

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "install_instance_root_missing:$ProjectRoot"
}
if (Test-Path -LiteralPath $LocalData -PathType Container) {
    $item = Get-Item -LiteralPath $LocalData -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "install_instance_localdata_reparse_point_forbidden:$LocalData"
    }
    $nestedReparse = Get-ChildItem -LiteralPath $LocalData -Recurse -Force -ErrorAction Stop |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
        Select-Object -First 1
    if ($nestedReparse) {
        throw "install_instance_localdata_reparse_point_forbidden:$($nestedReparse.FullName)"
    }
}

$boundRoots = @(Get-BoundRoots)
$foreignRoots = @($boundRoots | Where-Object {
    -not $_.Equals($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)
})
$aclModeState = Read-JsonObject (Join-Path $LocalData 'secure\windows_security\acl_mode.json')
$previousControllerSid = if ($null -ne $aclModeState -and
    $aclModeState.PSObject.Properties.Name -contains 'controller_sid') {
    [string]$aclModeState.controller_sid
} else { '' }
$foreignController = -not [string]::IsNullOrWhiteSpace($previousControllerSid) -and
    -not $previousControllerSid.Equals($CurrentControllerSid, [StringComparison]::OrdinalIgnoreCase)
if ($ValidateOnly) {
    Write-Host ("[SmartAgent Install] Instance validation: foreign_roots={0}; foreign_controller={1}" -f $foreignRoots.Count,$foreignController)
    return
}
New-Item -ItemType Directory -Force -Path (Join-Path $LocalData 'metadata'),(Join-Path $LocalData 'logs') | Out-Null
if ($foreignRoots.Count -gt 0 -or $foreignController) {
    $backupBase = Resolve-WritableBackupBase
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupRoot = Join-Path $backupBase ("relocated-$stamp-" + [Guid]::NewGuid().ToString('N').Substring(0,8))
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    if (Test-Path -LiteralPath $LocalData -PathType Container) {
        $backupLocalData = Join-Path $backupRoot 'localdata'
        Copy-Item -LiteralPath $LocalData -Destination $backupLocalData -Recurse -Force
        $sourceFiles = @(Get-ChildItem -LiteralPath $LocalData -Recurse -File -Force)
        $backupFiles = @(Get-ChildItem -LiteralPath $backupLocalData -Recurse -File -Force)
        if ($sourceFiles.Count -ne $backupFiles.Count) {
            throw 'install_instance_backup_inventory_mismatch'
        }
        foreach ($sourceFile in $sourceFiles) {
            $relative = $sourceFile.FullName.Substring($LocalData.Length).TrimStart('\')
            $backupFile = Join-Path $backupLocalData $relative
            if (-not (Test-Path -LiteralPath $backupFile -PathType Leaf) -or
                (Get-Sha256 $sourceFile.FullName) -ne (Get-Sha256 $backupFile)) {
                throw "install_instance_backup_hash_mismatch:$relative"
            }
        }
        Remove-Item -LiteralPath $LocalData -Recurse -Force
    }
    $venv = [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.venv')).TrimEnd('\')
    if (-not $venv.StartsWith($ProjectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "install_instance_venv_outside_root:$venv"
    }
    if (Test-Path -LiteralPath $venv -PathType Container) {
        $venvItem = Get-Item -LiteralPath $venv -Force
        if ($venvItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "install_instance_venv_reparse_point_forbidden:$venv"
        }
        Remove-Item -LiteralPath $venv -Recurse -Force
        Write-Host '[SmartAgent Install] Removed the relocated virtual environment; it will be rebuilt locally.' -ForegroundColor Yellow
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $LocalData 'metadata'),(Join-Path $LocalData 'logs') | Out-Null
    Write-JsonNoBom (Join-Path $LocalData 'metadata\relocation_state.json') ([ordered]@{
        schema = 'SMARTAGENT_INSTALL_RELOCATION_V1'
        current_root = $ProjectRoot
        previous_roots = @($foreignRoots)
        previous_controller_sid = $previousControllerSid
        current_controller_sid = $CurrentControllerSid
        backup_root = $backupRoot
        relocated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    })
    Write-Host "[SmartAgent Install] Foreign instance state was isolated: $backupRoot" -ForegroundColor Yellow
}

Write-JsonNoBom (Join-Path $LocalData 'metadata\install_instance.json') ([ordered]@{
    schema = 'SMARTAGENT_INSTALL_INSTANCE_V1'
    install_root = $ProjectRoot
    controller_sid = $CurrentControllerSid
    status = 'PREPARED'
    updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
})
Write-Host "[SmartAgent Install] Instance prepared for: $ProjectRoot" -ForegroundColor Green
