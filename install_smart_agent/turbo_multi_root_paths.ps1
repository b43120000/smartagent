# Git62 root-index path helper for elevated Native Broker enrollment scripts.
# An index identifies a root; it is NOT a security credential or grant.
function Get-TurboRootKey([string]$Root) {
    if (-not [IO.Path]::IsPathRooted($Root)) {
        throw 'GIT62_NATIVE_ABSOLUTE_ROOT_REQUIRED'
    }
    $normalized=[IO.Path]::GetFullPath($Root).TrimEnd('\').ToUpperInvariant()
    $hash=[Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hash.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($normalized))).Replace('-','').ToLowerInvariant()
    } finally {$hash.Dispose()}
}
function Get-TurboRootJournalPaths([string]$SecurityRoot,[string]$Root) {
    $key=Get-TurboRootKey $Root
    $folder=Join-Path $SecurityRoot 'turbo_root_activations'
    return [pscustomobject]@{
        directory=$folder
        activation=(Join-Path $folder ('activation-'+$key+'.json'))
        import=(Join-Path $folder ('import-'+$key+'.json'))
        key=$key
    }
}
function Assert-TurboRootJournalDirectory([string]$SecurityRoot) {
    $folder=Join-Path $SecurityRoot 'turbo_root_activations'
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
        New-Item -ItemType Directory -Path $folder -ErrorAction Stop | Out-Null
    }
    if ((Get-Item -LiteralPath $folder -Force -ErrorAction Stop).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw 'GIT62_NATIVE_JOURNAL_DIR_REPARSE'
    }
    & icacls.exe $folder '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) {throw 'GIT62_NATIVE_JOURNAL_DIR_ACL_FAILED'}
    $acl=Get-Acl -LiteralPath $folder -ErrorAction Stop
    foreach($rule in @($acl.Access)) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin @('S-1-5-18','S-1-5-32-544')) {
                throw 'GIT62_NATIVE_JOURNAL_DIR_UNTRUSTED_ACE'
            }
        }
    }
}
