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

function Test-DisposableToolCompatibility(
    [string]$ToolName, [string]$Executable,
    [string]$Arguments, [string]$ExpectedOutput
) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        Write-Host ("GIT47_TOOL_NOT_VERIFIED:" + $ToolName + ":executable_missing")
        return
    }
    try {
        # This is capability discovery only; an observed application launch
        # never upgrades the production Turbo sandbox effective state.
        $raw = [SmartAgentGit47AppContainerProbe]::Launch(
            $moniker, $Executable, $Arguments, $turbo, [uint32]30000)
        $result = $raw | ConvertFrom-Json
        if (-not $result.sandbox_identity_verified) {
            throw 'child_token_not_verified'
        }
        if ([int]$result.exit_code -ne 0) {
            throw ("nonzero_exit_" + [string]$result.exit_code)
        }
        if ($ExpectedOutput) {
            $path = Join-Path $turbo $ExpectedOutput
            if (-not (Test-Path -LiteralPath $path)) {
                throw 'postcondition_missing'
            }
        }
        Write-Host ("PASS GIT47_TOOL_APPCONTAINER_LAUNCH:" + $ToolName)
    } catch {
        Write-Host ("GIT47_TOOL_NOT_VERIFIED:" + $ToolName + ":" + $_.Exception.GetType().Name + ":" + $_.Exception.Message.Substring(0, [Math]::Min(110, $_.Exception.Message.Length)))
    }
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
    Invoke-Probe 'spawn_read' $siblingFile 5 'child_process_outside_read_os_access_denied'
    Invoke-Probe 'spawn_read' (Join-Path $turbo 'inside.txt') 0 'child_process_inside_read'

    $insideDelete = Join-Path $turbo 'delete_inside.txt'
    [IO.File]::WriteAllText($insideDelete, 'DISPOSABLE-DELETE-ME')
    Invoke-Probe 'delete_file' $insideDelete 0 'inside_delete_file'
    if (Test-Path -LiteralPath $insideDelete) {
        throw 'GIT47_INSIDE_DELETE_POSTCONDITION_FAILED'
    }

    # Separate empty disposable root with identical non-delete root ACE. The
    # probe executable runs from turbo, and cannot delete the extra root.
    $protectedRoot = Join-Path $fixture 'empty_protected_root'
    New-Item -ItemType Directory -Path $protectedRoot | Out-Null
    Set-Acl -LiteralPath $protectedRoot -AclObject $acl -ErrorAction Stop
    & icacls.exe $protectedRoot '/setintegritylevel' '(OI)(CI)L' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GIT47_EMPTY_ROOT_IL_SETUP_FAILED' }
    Invoke-Probe 'delete_dir' $protectedRoot 5 'empty_turbo_root_delete_denied'
    if (-not (Test-Path -LiteralPath $protectedRoot)) {
        throw 'GIT47_PROTECTED_ROOT_MISSING_AFTER_DENIAL'
    }

    # NTFS hardlinks preserve the referenced file's own security descriptor.
    # A link in the allowed root must not make a protected sibling readable.
    $hardlink = Join-Path $turbo 'outside_hardlink.txt'
    New-Item -ItemType HardLink -Path $hardlink -Target $siblingFile -ErrorAction Stop | Out-Null
    Invoke-Probe 'read' $hardlink 5 'hardlink_outside_read_os_access_denied'

    # Junction escape to an ungranted sibling is still decided by Windows ACL.
    $junction = Join-Path $turbo 'outside_junction'
    $junctionCreated = $false
    try {
        New-Item -ItemType Junction -Path $junction -Target $sibling -ErrorAction Stop | Out-Null
        $junctionCreated = $true
    } catch {
        # Only an unavailable fixture primitive can be skipped. Actual OS
        # escape-probe failure MUST propagate and fail the Windows CI job.
        Write-Host ('GIT47_JUNCTION_FIXTURE_UNAVAILABLE:' + $_.Exception.GetType().Name)
    }
    try {
        if ($junctionCreated) {
            Invoke-Probe 'read' (Join-Path $junction 'sibling_secret.txt') 5 'junction_outside_read_os_access_denied'
        }
    } finally {
        if (Test-Path -LiteralPath $junction) {
            & cmd.exe /d /c ('rmdir "' + $junction + '"') | Out-Null
        }
    }

    # Windows desktop application compatibility discovery: no claim of
    # unrestricted/full Turbo, even if the application loads successfully.
    $cmdExe = Join-Path $env:SystemRoot 'System32\\cmd.exe'
    Test-DisposableToolCompatibility 'cmd' $cmdExe '/d /c echo GIT47_CMD_OK>cmd_probe.txt' 'cmd_probe.txt'

    $powershellExe = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'
    Test-DisposableToolCompatibility 'powershell' $powershellExe '-NoProfile -NonInteractive -Command "[IO.File]::WriteAllText(''ps_probe.txt'',''GIT47_PS_OK'')"' 'ps_probe.txt'

    $python = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        Test-DisposableToolCompatibility 'python' ([string]$python.Source) '-I -c "from pathlib import Path;Path(''python_probe.txt'').write_text(''GIT47_PY_OK'')"' 'python_probe.txt'
    } else {
        Write-Host 'GIT47_TOOL_NOT_VERIFIED:python:executable_missing'
    }
    $git = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($git) {
        Test-DisposableToolCompatibility 'git' ([string]$git.Source) 'init git_probe' 'git_probe'
    } else {
        Write-Host 'GIT47_TOOL_NOT_VERIFIED:git:executable_missing'
    }

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
