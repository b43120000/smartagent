# Git47 — Windows OS AppContainer *disposable fixture* test.
# This script never touches user E: roots and does not enable production Turbo.
[CmdletBinding()]
param([string]$FixtureParent = $env:RUNNER_TEMP)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
    throw 'GIT47_WINDOWS_ONLY'
}
if (-not $FixtureParent) { $FixtureParent = [IO.Path]::GetTempPath() }
if (-not (Test-Path -LiteralPath $FixtureParent -PathType Container)) {
    throw 'GIT47_DISPOSABLE_FIXTURE_PARENT_NOT_FOUND'
}
$base = [IO.Path]::GetFullPath($FixtureParent)
$fixture = Join-Path $base ('Git47-AppContainer-Fixture-' + [Guid]::NewGuid().ToString('N'))
$turbo = Join-Path $fixture 'turbo'
$sibling = Join-Path $fixture 'sibling'
$moniker = 'SmartAgentGit47Test_' + [Guid]::NewGuid().ToString('N')
$profileCreated = $false

function Add-Grant(
    [Security.AccessControl.DirectorySecurity]$Acl,
    [Security.Principal.SecurityIdentifier]$Sid,
    [Security.AccessControl.FileSystemRights]$Rights,
    [Security.AccessControl.InheritanceFlags]$Inheritance,
    [Security.AccessControl.PropagationFlags]$Propagation
) {
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $Sid, $Rights, $Inheritance, $Propagation,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$Acl.AddAccessRule($rule)
}

function Invoke-Probe([string]$Op, [string]$Target,
                      [int]$ExpectedError, [string]$Label) {
    $receipt = Join-Path $turbo ('.git47-probe-' + [Guid]::NewGuid().ToString('N') + '.txt')
    $arguments = $Op + ' "' + $Target + '" "' + $receipt + '"'
    $raw = [SmartAgentGit47AppContainerProbe]::Launch(
        $moniker, $probe, $arguments, $turbo, [uint32]30000)
    $process = $raw | ConvertFrom-Json
    if (-not $process.sandbox_identity_verified -or
        $process.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
        $process.child_process_policy -ne 'JOB_KILL_ON_CLOSE') {
        throw "GIT47_PROBE_TOKEN_ATTESTATION_FAILED:$Label"
    }
    if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) {
        throw "GIT47_PROBE_NO_OS_RESULT:$Label"
    }
    $text = [IO.File]::ReadAllText($receipt).Trim()
    if ($text -cne "WIN32_ERROR=$ExpectedError") {
        throw "GIT47_OS_ERROR_MISMATCH:expected=$ExpectedError,actual=$text"
    }
    if ($ExpectedError -eq 5 -and [int]$process.exit_code -ne 10) {
        throw "GIT47_DENIED_PROCESS_EXIT_MISMATCH:$Label"
    }
    Remove-Item -LiteralPath $receipt -Force
    Write-Host "PASS $Label : actual Win32 CreateFileW error=$ExpectedError"
}

try {
    New-Item -ItemType Directory -Force -Path $fixture,$turbo,$sibling | Out-Null
    $parentFile = Join-Path $fixture 'parent_secret.txt'
    $siblingFile = Join-Path $sibling 'sibling_secret.txt'
    [IO.File]::WriteAllText($parentFile, 'DISPOSABLE-PARENT-SENTINEL')
    [IO.File]::WriteAllText($siblingFile, 'DISPOSABLE-SIBLING-SENTINEL')

    $source = Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs'
    Add-Type -Path $source -ErrorAction Stop
    $packageSidText = [SmartAgentGit47AppContainerProbe]::CreateProfile($moniker)
    $profileCreated = $true
    if ($packageSidText -notmatch '^S-1-15-2-') {
        throw 'GIT47_INVALID_APPCONTAINER_PACKAGE_SID'
    }
    $packageSid = New-Object Security.Principal.SecurityIdentifier($packageSidText)
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User

    # All DACL mutations are limited to this newly created temporary root.
    # No parent / sibling / user's real project is changed.
    $acl = Get-Acl -LiteralPath $turbo
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($userSid)
    $both = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $plain = [Security.AccessControl.InheritanceFlags]::None
    $none = [Security.AccessControl.PropagationFlags]::None
    $childrenOnly = [Security.AccessControl.PropagationFlags]::InheritOnly
    foreach ($fullSid in @($userSid,
        (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')),
        (New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))) {
        Add-Grant $acl $fullSid ([Security.AccessControl.FileSystemRights]::FullControl) $both $none
    }
    $rootRights = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
                  [Security.AccessControl.FileSystemRights]::WriteData -bor
                  [Security.AccessControl.FileSystemRights]::AppendData -bor
                  [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
                  [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
                  [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
    Add-Grant $acl $packageSid $rootRights $plain $none
    Add-Grant $acl $packageSid ([Security.AccessControl.FileSystemRights]::Modify) $both $childrenOnly
    Set-Acl -LiteralPath $turbo -AclObject $acl -ErrorAction Stop

    # Low-integrity AppContainer cannot write a medium-IL directory even when
    # DACL grants Modify. Set only our disposable root and descendants low-IL.
    & icacls.exe $turbo '/setintegritylevel' '(OI)(CI)L' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "GIT47_DISPOSABLE_LOW_INTEGRITY_LABEL_FAILED:$LASTEXITCODE"
    }

    $probe = Join-Path $turbo 'git47_native_file_probe.exe'
    Add-Type -Path (Join-Path $PSScriptRoot 'turbo_win32_file_probe.cs') -OutputAssembly $probe -OutputType ConsoleApplication -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) {
        throw 'GIT47_NATIVE_PROBE_COMPILATION_FAILED'
    }
    $inside = Join-Path $turbo 'inside.txt'
    [IO.File]::WriteAllText($inside, 'DISPOSABLE-INSIDE-SENTINEL')
    Invoke-Probe 'read' $inside 0 'inside_read'
    Invoke-Probe 'write' (Join-Path $turbo 'created_inside.txt') 0 'inside_write'
    Invoke-Probe 'read' $parentFile 5 'parent_read_os_access_denied'
    Invoke-Probe 'write' (Join-Path $fixture 'out_of_root_write.txt') 5 'parent_write_os_access_denied'
    Invoke-Probe 'read' $siblingFile 5 'sibling_read_os_access_denied'
    Invoke-Probe 'write' (Join-Path $sibling 'sibling_write.txt') 5 'sibling_write_os_access_denied'

    Write-Host 'GIT47_DISPOSABLE_WIN32_APPCONTAINER_FILE_BOUNDARY_PASS'
    Write-Host 'NOTE: This does NOT attest Python/Git/PowerShell, child inheritance, hardlinks, reparse points, root delete, reboot or production lease revocation.'
}
finally {
    # Cleanup is performed by the normal fixture owner, never from the sandbox.
    if ($profileCreated) {
        try { [SmartAgentGit47AppContainerProbe]::DeleteProfile($moniker) }
        catch { Write-Warning "Temporary AppContainer profile cleanup failed: $($_.Exception.Message)" }
    }
    if (Test-Path -LiteralPath $fixture) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction SilentlyContinue
    }
}
