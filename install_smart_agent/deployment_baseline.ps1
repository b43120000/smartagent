# Independent deployment receipt. Not a signature or machine authorization.
# Publish only after the deployed file set has passed SHA-256 verification.
function Test-PortableDistributionPath([string]$Relative) {
    $value=$Relative.Replace('\','/').ToLowerInvariant()
    return $value -match '^\.venv/portable-runtime/(manifest\.json|requirements-lock\.json|licenses\.json|readme\.md|runtime\.zip(\.part[0-9]+)?)$'
}
function Test-PrivateDeploymentPath([string]$Relative) {
    if (Test-PortableDistributionPath $Relative) { return $false }
    $parts = $Relative.Replace('\', '/').ToLowerInvariant().Split('/')
    # Manual calibration receipts and site/account-specific selectors stay local.
    if ('dom_config.history' -in $parts -or $parts[-1] -eq 'dom_config.json') { return $true }
    if ([IO.Path]::GetExtension($Relative) -eq '.bak') { return $true }
    if (@($parts | Where-Object { $_ -in @('.git', '.venv', 'localdata', '.agents', '__pycache__', '.pytest_cache', 'browser_profile', '.codex', '.aws', '.ssh') }).Count) { return $true }
    if (@($parts | Where-Object { $_ -like '.env*' }).Count) { return $true }
    return [IO.Path]::GetExtension($Relative) -in @('.pyc', '.pyo', '.lnk', '.pem', '.key', '.pfx', '.p12', '.ppk')
}
function Resolve-BaselineChild([string]$Root, [string]$Relative) {
    $relativePath = $Relative.Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($relativePath) -or [IO.Path]::IsPathRooted($relativePath) -or
        $relativePath.Contains(':') -or @($relativePath.Split('\') | Where-Object { $_ -in @('', '.', '..') -or $_.EndsWith('.') -or $_.EndsWith(' ') }).Count) {
        throw "deployment_baseline_path_invalid:$Relative"
    }
    $cursor = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    # Reject junctions in ancestors as well as in children.
    $ancestor = $cursor
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "deployment_baseline_reparse:$ancestor" }
        }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
    foreach ($part in $relativePath.Split('\')) {
        $cursor = Join-Path $cursor $part
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "deployment_baseline_reparse:$cursor" }
        }
    }
    return $cursor
}
function Get-DeploymentBaselinePath([string]$Root) {
    return Resolve-BaselineChild $Root 'localdata/update/deployment_baseline.json'
}
function Get-BaselineSha([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Save-DeploymentBaseline([string]$InstallRoot, [string]$SourceRoot, $FileHashes, [string]$ReleaseId, [long]$ReleaseSequence) {
    $files = [ordered]@{}
    foreach ($relative in @($FileHashes.Keys | Sort-Object)) {
        if (Test-PrivateDeploymentPath $relative) { throw "deployment_baseline_private_path_forbidden:$relative" }
        $path = Resolve-BaselineChild $InstallRoot $relative
        $expected = [string]$FileHashes[$relative]
        if ($expected -notmatch '^[0-9a-f]{64}$' -or (Get-BaselineSha $path) -ne $expected) { throw "deployment_baseline_hash_mismatch:$relative" }
        $key = $relative.Replace('\', '/')
        if ($files.Contains($key)) { throw "deployment_baseline_duplicate:$key" }
        $files[$key] = $expected
    }
    if ($files.Count -eq 0) { throw 'deployment_baseline_empty' }
    $receipt = [ordered]@{
        schema='SMARTAGENT_DEPLOYMENT_BASELINE_V1'; install_root=[IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
        source_root=[IO.Path]::GetFullPath($SourceRoot).TrimEnd('\'); release_id=$ReleaseId
        release_sequence=$ReleaseSequence; deployed_at_utc=[DateTime]::UtcNow.ToString('o'); files=$files
    }
    $path = Get-DeploymentBaselinePath $InstallRoot
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    $temporary = $path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($receipt | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $path -PathType Leaf) { [IO.File]::Replace($temporary, $path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $path) }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
    }
}
