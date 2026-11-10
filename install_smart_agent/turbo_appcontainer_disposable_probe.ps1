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
$crossVolumeFixture = ''
$script:osBoundaryEvidence = @()
$script:toolCompatibilityEvidence = @()

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
    $script:osBoundaryEvidence += [ordered]@{
        schema='SMARTAGENT_GIT47_OS_TEST_RECEIPT_V1'
        action_id=('GIT47-' + $Label)
        requested_operation=$Op
        scope_verdict=$(if ($ExpectedError -eq 5) { 'TURBO_SCOPE_DENIED' } else { 'TURBO_SCOPE_ALLOWED' })
        os_execution_result=$(if ($ExpectedError -eq 5) { 'OS_ACCESS_DENIED' } else { 'OS_EXECUTION_SUCCEEDED' })
        win32_error_code=$ExpectedError
        os_access_denied=($ExpectedError -eq 5)
        sandbox_identity_verified=$true
        evidence_ref=('GIT47-OS-' + $Label)
        executed=$true
    }
    Write-Host "PASS $Label : actual Win32 CreateFileW error=$ExpectedError"
}

function Test-DisposableToolCompatibility(
    [string]$ToolName, [string]$Executable,
    [string]$Arguments, [string]$ExpectedOutput,
    [bool]$Required = $false
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
        if ([uint32]$result.exit_code -ne [uint32]0) {
            throw ("nonzero_exit_" + [string]$result.exit_code)
        }
        if ($ExpectedOutput) {
            $path = Join-Path $turbo $ExpectedOutput
            if (-not (Test-Path -LiteralPath $path)) {
                throw 'postcondition_missing'
            }
        }
        $script:toolCompatibilityEvidence += [ordered]@{
            tool=$ToolName; status='PASS'; sandbox_identity_verified=$true
            os_execution_result='OS_EXECUTION_SUCCEEDED'
        }
        Write-Host ("PASS GIT47_TOOL_APPCONTAINER_LAUNCH:" + $ToolName)
    } catch {
        $errorText = $_.Exception.GetType().Name
        $inner = $_.Exception.InnerException
        while ($inner) {
            if ($inner -is [System.ComponentModel.Win32Exception]) {
                $errorText = $errorText + ":win32_error=" + [string]$inner.NativeErrorCode
                break
            }
            $inner = $inner.InnerException
        }
        $script:toolCompatibilityEvidence += [ordered]@{
            tool=$ToolName; status='NOT_VERIFIED'; sandbox_identity_verified=$false
            os_execution_result='OS_EXECUTION_FAILED'; reason_code=$errorText
        }
        Write-Host ("GIT47_TOOL_NOT_VERIFIED:" + $ToolName + ":" + $errorText + ":" + $_.Exception.Message.Substring(0, [Math]::Min(100, $_.Exception.Message.Length)))
        if ($Required) {
            throw ("GIT47_REQUIRED_APP_CONTAINER_TOOL_FAILED:" + $ToolName + ":" + $errorText)
        }
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

    # Explicit parent Traverse without ListDirectory, read contents, or
    # write. Required to reach the *separate* RX dependency subtree.
    $fixtureAcl = Get-Acl -LiteralPath $fixture
    Add-Grant $fixtureAcl $packageSid ([Security.AccessControl.FileSystemRights]::Traverse) ([Security.AccessControl.InheritanceFlags]::None) ([Security.AccessControl.PropagationFlags]::None)
    Set-Acl -LiteralPath $fixture -AclObject $fixtureAcl -ErrorAction Stop

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
    # Git for Windows may require the OS NUL pseudo-device. Diagnose its
    # AppContainer kernel access before considering any *explicit* exception.
    # Never modify global device ACLs just to make Git launch.
    foreach ($device in @('NUL', '\\\\.\\NUL')) {
        $receipt = Join-Path $turbo ('.git47-device-' + [Guid]::NewGuid().ToString('N') + '.txt')
        $arguments = 'read "' + $device + '" "' + $receipt + '"'
        $null = [SmartAgentGit47AppContainerProbe]::Launch(
            $moniker, $probe, $arguments, $turbo, [uint32]15000)
        if (Test-Path -LiteralPath $receipt -PathType Leaf) {
            $code = [IO.File]::ReadAllText($receipt).Trim()
            Write-Host ('GIT47_OS_NUL_DEVICE_ACCESS:' + $device + ':' + $code)
            Remove-Item -LiteralPath $receipt -Force
        } else {
            Write-Host ('GIT47_OS_NUL_DEVICE_UNAVAILABLE:' + $device)
        }
    }
    Invoke-Probe 'write' (Join-Path $turbo 'created_inside.txt') 0 'inside_write'
    # Git50: execute an actual script via the OS-attested AppContainer,
    # rather than interpreting the command in the host/CI PowerShell.
    $commandScript = "[IO.File]::WriteAllText('git50_sandbox_command.txt','OS_ISOLATED');"
    $commandReceipt = [SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        $moniker, $commandScript, $turbo, [uint32]30000) | ConvertFrom-Json
    if (-not $commandReceipt.sandbox_identity_verified -or
        $commandReceipt.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
        $commandReceipt.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        [int]$commandReceipt.exit_code -ne 0 -or
        [IO.File]::ReadAllText((Join-Path $turbo 'git50_sandbox_command.txt')) -cne 'OS_ISOLATED') {
        throw 'GIT50_OS_SANDBOX_COMMAND_LAUNCH_FAILED'
    }
    Write-Host 'GIT50_OS_SANDBOX_COMMAND_EXECUTION_PASS'
    # The child script attempts a real external file write; only the OS
    # kernel may reject it. There is no parent-side path preflight substitute.
    $externalWriteTarget = Join-Path $sibling 'git50_illegal_write.txt'
    $externalLiteral = $externalWriteTarget.Replace("'", "''")
    $denialScript = @"
try {
    [IO.File]::WriteAllText('$externalLiteral','ESCAPE')
    exit 91
} catch [System.UnauthorizedAccessException] {
    [IO.File]::WriteAllText('git50_kernel_denied.txt','OS_DENIED')
    exit 0
}
"@
    $denialReceipt = [SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        $moniker, $denialScript, $turbo, [uint32]30000) | ConvertFrom-Json
    if (-not $denialReceipt.sandbox_identity_verified -or
        [int]$denialReceipt.exit_code -ne 0 -or
        (Test-Path -LiteralPath $externalWriteTarget -PathType Leaf) -or
        [IO.File]::ReadAllText((Join-Path $turbo 'git50_kernel_denied.txt')) -cne 'OS_DENIED') {
        throw 'GIT50_OS_SANDBOX_EXTERNAL_WRITE_NOT_DENIED'
    }
    Write-Host 'GIT50_OS_SANDBOX_COMMAND_OUTSIDE_DENIAL_PASS'
    # Git50 captured command output: native exit status is kernel-backed,
    # while output bytes are explicitly NOT authority. Host file handles stay
    # pinned so a sandbox-created junction cannot redirect their final read.
    $capturedRaw = [SmartAgentGit47AppContainerProbe]::LaunchPowerShellCaptured(
        $moniker, "'GIT50_CAPTURED_FROM_APP_CONTAINER'",
        $turbo, [uint32]30000, [int]1048576)
    $captured = $capturedRaw | ConvertFrom-Json -ErrorAction Stop
    $capturedText = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([string]$captured.stdout_base64)).Trim()
    Write-Host ('GIT50_CAPTURE_DIAGNOSTIC:exit=' + [string]$captured.exit_code +
                ';out_bytes=' + [string]$captured.stdout_bytes +
                ';err_bytes=' + [string]$captured.stderr_bytes +
                ';stdout_match=' + [string]($capturedText -ceq 'GIT50_CAPTURED_FROM_APP_CONTAINER') +
                ';error_class=' + [Text.Encoding]::UTF8.GetString(
                    [Convert]::FromBase64String([string]$captured.stderr_base64)).Split(':')[0])
    if ($captured.sandbox_identity_verified -ne $true -or
        $captured.os_process_started -ne $true -or
        [int]$captured.exit_code -ne 0 -or
        $capturedText -cne 'GIT50_CAPTURED_FROM_APP_CONTAINER' -or
        $captured.output_is_authoritative -ne $false) {
        throw 'GIT50_APP_CONTAINER_CAPTURED_OUTPUT_FAILED'
    }
    Write-Host 'GIT50_OS_SANDBOX_PINNED_OUTPUT_CAPTURE_PASS'

    # A failing native command must retain its nonzero exit status.
    $nonzero = [SmartAgentGit47AppContainerProbe]::LaunchPowerShellCaptured(
        $moniker, "throw 'GIT50_THROW_TEST'",
        $turbo, [uint32]30000, [int]1048576) | ConvertFrom-Json
    if ([int]$nonzero.exit_code -eq 0 -or
        -not [string]$nonzero.stderr_base64) {
        throw 'GIT50_APP_CONTAINER_NONZERO_STATUS_LOST'
    }
    Write-Host 'GIT50_OS_SANDBOX_COMMAND_FAILURE_REPORTED_PASS'
    Invoke-Probe 'read' $parentFile 5 'parent_read_os_access_denied'
    Invoke-Probe 'write' (Join-Path $fixture 'out_of_root_write.txt') 5 'parent_write_os_access_denied'
    Invoke-Probe 'read' $siblingFile 5 'sibling_read_os_access_denied'
    Invoke-Probe 'write' (Join-Path $sibling 'sibling_write.txt') 5 'sibling_write_os_access_denied'

    # A second disposable sentinel on a *different* volume validates actual
    # kernel denial without modifying existing user files or other projects.
    $otherTemp = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Temp' } else { '' }
    if ($otherTemp -and (Test-Path -LiteralPath $otherTemp -PathType Container) -and
        ([IO.Path]::GetPathRoot($otherTemp) -ine [IO.Path]::GetPathRoot($fixture))) {
        $crossVolumeFixture = Join-Path $otherTemp ('Git47-OtherVolume-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $crossVolumeFixture | Out-Null
        $externalSentinel = Join-Path $crossVolumeFixture 'external_private_data.txt'
        [IO.File]::WriteAllText($externalSentinel, 'DISPOSABLE_OTHER_VOLUME_SENTINEL')
        Invoke-Probe 'read' $externalSentinel 5 'other_volume_read_os_access_denied'
        Invoke-Probe 'write' (Join-Path $crossVolumeFixture 'external_write.txt') 5 'other_volume_write_os_access_denied'
    } else {
        Write-Host 'GIT47_OTHER_VOLUME_NOT_VERIFIED:separate_disposable_volume_unavailable'
    }
    Invoke-Probe 'spawn_read' $siblingFile 5 'child_process_outside_read_os_access_denied'
    Invoke-Probe 'spawn_read' (Join-Path $turbo 'inside.txt') 0 'child_process_inside_read'
    $env:GIT47_TEST_SECRET_NEVER_INHERIT = 'SECRET_FROM_UNRESTRICTED_HOST'
    try {
        Invoke-Probe 'check_env_absent' 'GIT47_TEST_SECRET_NEVER_INHERIT' 0 'sanitized_child_environment_no_host_secret'
    } finally {
        Remove-Item Env:GIT47_TEST_SECRET_NEVER_INHERIT -ErrorAction SilentlyContinue
    }

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
    Test-DisposableToolCompatibility 'cmd' $cmdExe '/d /c echo GIT47_CMD_OK>cmd_probe.txt' 'cmd_probe.txt' $true

    $powershellExe = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'
    Test-DisposableToolCompatibility 'powershell' $powershellExe '-NoProfile -NonInteractive -Command "[IO.File]::WriteAllText(''ps_probe.txt'',''GIT47_PS_OK'')"' 'ps_probe.txt' $true
    # Avoid host PowerShell script-file lookup inconsistencies by passing a
    # static UTF-16LE EncodedCommand. This is still actual PowerShell running
    # under the AppContainer process token, not a host-side software guard.
    $psTargetLiteral = $siblingFile.Replace("'", "''")
    foreach ($operation in @('read', 'write')) {
        $marker = 'ps_' + $operation + '_denied_ok.txt'
        $operationScript = if ($operation -eq 'read') {
            "[IO.File]::ReadAllText('$psTargetLiteral') | Out-Null"
        } else {
            "[IO.File]::WriteAllText('$psTargetLiteral','SCOPE_ESCAPE')"
        }
        $sourceScript = "try { $operationScript; exit 98 } catch [System.UnauthorizedAccessException] { [IO.File]::WriteAllText('$marker','OS_ACCESS_DENIED'); exit 0 }"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($sourceScript))
        $psArgs = '-NoProfile -NonInteractive -EncodedCommand ' + $encoded
        Test-DisposableToolCompatibility ('powershell_outside_' + $operation + '_os_denied') $powershellExe $psArgs $marker $true
    }
    if ([IO.File]::ReadAllText($siblingFile) -cne 'DISPOSABLE-SIBLING-SENTINEL') {
        throw 'GIT47_POWERSHELL_EXTERNAL_SENTINEL_CORRUPTED'
    }

    $cmdProbe = Join-Path $turbo 'cmd_os_denial.cmd'
    $cmdStdout = Join-Path $turbo 'cmd_denial_stdout.txt'
    $cmdStderr = Join-Path $turbo 'cmd_denial_stderr.txt'
    $cmdMarker = Join-Path $turbo 'cmd_denied_ok.txt'
    $cmdCode = '@echo off' + "`r`n" +
        'type "' + $siblingFile + '" 1>"' + $cmdStdout + '" 2>"' + $cmdStderr + '"' + "`r`n" +
        'if not errorlevel 1 exit /b 99' + "`r`n" +
        'echo OS_DENIED>"' + $cmdMarker + '"' + "`r`n" +
        'exit /b 0' + "`r`n"
    [IO.File]::WriteAllText($cmdProbe, $cmdCode)
    # Batch script lives in the already attested working directory;
    # explicit .cmd avoids shell-quoted absolute-path reparsing.
    Test-DisposableToolCompatibility 'cmd_absolute_outside_read_denied' $cmdExe '/d /c cmd_os_denial.cmd' 'cmd_denied_ok.txt' $true
    if (-not (Test-Path -LiteralPath $cmdStderr -PathType Leaf) -or
        ([IO.File]::ReadAllText($cmdStderr) -notmatch '(?i)access is denied')) {
        throw 'GIT47_CMD_OUTSIDE_OS_DENIAL_NOT_CONFIRMED'
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        # Copy only into a disposable read/execute dependency fixture; never
        # alter ACLs on an installed Python or unrelated user workspace.
        $dependencyRoot = Join-Path $fixture 'trusted_tool_dependencies'
        $localPython = Join-Path $dependencyRoot 'python'
        New-Item -ItemType Directory -Force -Path $localPython | Out-Null
        $originalPythonRoot = Split-Path -Parent ([string]$python.Source)
        foreach ($item in @(Get-ChildItem -LiteralPath $originalPythonRoot -File -ErrorAction Stop)) {
            if ($item.Name -match '^(python.*\.(exe|dll)|vcruntime.*\.dll)$') {
                Copy-Item -LiteralPath $item.FullName -Destination $localPython -Force
            }
        }
        foreach ($dirname in @('Lib','DLLs')) {
            $sourceDir = Join-Path $originalPythonRoot $dirname
            if (Test-Path -LiteralPath $sourceDir -PathType Container) {
                Copy-Item -LiteralPath $sourceDir -Destination (Join-Path $localPython $dirname) -Recurse -Force
            }
        }
        # Optional Git-compatible Python implementation is installed only on
        # CI host and copied to the disposable RX dependency subtree.
        $siteDir = Join-Path $localPython 'Lib\\site-packages'
        New-Item -ItemType Directory -Force -Path $siteDir | Out-Null
        foreach ($moduleName in @('dulwich','urllib3','certifi','idna','charset_normalizer')) {
            $query = "import importlib.util,pathlib; x=importlib.util.find_spec('$moduleName'); print(pathlib.Path(x.origin).parent if x and x.origin else '')"
            $location = & ([string]$python.Source) -c $query 2>$null |
                Select-Object -First 1
            if ($LASTEXITCODE -eq 0 -and $location -and
                (Test-Path -LiteralPath ([string]$location) -PathType Container)) {
                Copy-Item -LiteralPath ([string]$location) -Destination (Join-Path $siteDir $moduleName) -Recurse -Force
                Write-Host ('GIT47_PYTHON_PACKAGE_RX_DEPENDENCY_COPIED:' + $moduleName)
            }
        }
        $depAcl = Get-Acl -LiteralPath $dependencyRoot
        $depAcl.SetAccessRuleProtection($true, $false)
        # Preserve normal fixture owner, SYSTEM and Administrators access;
        # package SID receives only Read/Execute, inherited by all files.
        foreach ($fullSid in @($userSid,
            (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')),
            (New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))) {
            Add-Grant $depAcl $fullSid ([Security.AccessControl.FileSystemRights]::FullControl) $both $none
        }
        Add-Grant $depAcl $packageSid ([Security.AccessControl.FileSystemRights]::ReadAndExecute) $both $none
        Set-Acl -LiteralPath $dependencyRoot -AclObject $depAcl -ErrorAction Stop
        Test-DisposableToolCompatibility 'python' ([string]$python.Source) '-I -c "from pathlib import Path;Path(''python_probe.txt'').write_text(''GIT47_PY_OK'')"' 'python_probe.txt'
        Test-DisposableToolCompatibility 'python_fixture_rx_dependency' (Join-Path $localPython 'python.exe') '-I -c "from pathlib import Path;Path(''python_fixture_ok.txt'').write_text(''GIT47_PY_OK'')"' 'python_fixture_ok.txt' $true
        # Python's own open() must encounter a kernel-enforced permission
        # error, not a Python wrapper/Software Guard rejection. The script
        # writes its success marker only after PermissionError with WinError 5.
        $pyProbe = Join-Path $turbo 'python_negative_probe.py'
        $pyCode = @'
import pathlib
import subprocess
import shutil
import sys

def child():
    try:
        pathlib.Path(sys.argv[2]).read_bytes()
    except OSError as exc:
        # Python may translate Win32 5 into POSIX errno=EACCES.
        winerror = getattr(exc, "winerror", None)
        errnum = getattr(exc, "errno", None)
        pathlib.Path("python_denial_diagnostic.txt").write_text(
            type(exc).__name__ + ";winerror=" + str(winerror) +
            ";errno=" + str(errnum))
        if isinstance(exc, PermissionError) and (
            winerror == 5 or errnum == 13
        ):
            pathlib.Path("python_child_denied_ok.txt").write_text("OS_ACCESS_DENIED")
            return 0
    return 91

if sys.argv[1] == "overwrite":
    target = pathlib.Path(sys.argv[2])
    target.write_text("GIT47_OVERWRITE_PASS")
    if target.read_text() != "GIT47_OVERWRITE_PASS":
        raise SystemExit(94)
    pathlib.Path("python_overwrite_ok.txt").write_text("PASS")
    raise SystemExit(0)
if sys.argv[1] == "remove_tree":
    target = pathlib.Path(sys.argv[2])
    shutil.rmtree(target)
    if target.exists():
        raise SystemExit(95)
    pathlib.Path("python_recursive_delete_ok.txt").write_text("PASS")
    raise SystemExit(0)
if sys.argv[1] == "write_outside":
    try:
        pathlib.Path(sys.argv[2]).write_text("ESCAPED_SCOPE")
    except PermissionError as exc:
        if getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) == 13:
            pathlib.Path("python_outside_write_denied_ok.txt").write_text("OS_ACCESS_DENIED")
            raise SystemExit(0)
    raise SystemExit(96)
if sys.argv[1] == "child":
    raise SystemExit(child())
if sys.argv[1] == "parent":
    result = subprocess.run([sys.executable, "-I", __file__, "child", sys.argv[2]],
                            check=False)
    if result.returncode != 0:
        raise SystemExit(92)
    pathlib.Path("python_spawn_denied_ok.txt").write_text("CHILD_OS_ACCESS_DENIED")
    raise SystemExit(0)
raise SystemExit(93)
'@
        [IO.File]::WriteAllText($pyProbe, $pyCode)
        $pyExecutable = Join-Path $localPython 'python.exe'
        $pyArgs = '-I "' + $pyProbe + '" child "' + $siblingFile + '"'
        Test-DisposableToolCompatibility 'python_fixture_open_outside_os_denied' $pyExecutable $pyArgs 'python_child_denied_ok.txt' $true
        $pyArgs = '-I "' + $pyProbe + '" parent "' + $siblingFile + '"'
        Test-DisposableToolCompatibility 'python_fixture_subprocess_os_denied' $pyExecutable $pyArgs 'python_spawn_denied_ok.txt' $true
        $overwrite = Join-Path $turbo 'python_overwrite_target.txt'
        [IO.File]::WriteAllText($overwrite, 'BEFORE_OVERWRITE')
        $pyArgs = '-I "' + $pyProbe + '" overwrite "' + $overwrite + '"'
        Test-DisposableToolCompatibility 'python_in_root_overwrite' $pyExecutable $pyArgs 'python_overwrite_ok.txt' $true
        if ([IO.File]::ReadAllText($overwrite) -cne 'GIT47_OVERWRITE_PASS') {
            throw 'GIT47_PYTHON_OVERWRITE_POSTCONDITION_FAILED'
        }

        $nested = Join-Path $turbo 'recursive_delete_fixture'
        $nestedChild = Join-Path $nested 'nested'
        New-Item -ItemType Directory -Force -Path $nestedChild | Out-Null
        [IO.File]::WriteAllText((Join-Path $nestedChild 'child.txt'), 'DELETE_ME')
        $pyArgs = '-I "' + $pyProbe + '" remove_tree "' + $nested + '"'
        Test-DisposableToolCompatibility 'python_in_root_recursive_delete' $pyExecutable $pyArgs 'python_recursive_delete_ok.txt' $true
        if (Test-Path -LiteralPath $nested) { throw 'GIT47_PYTHON_TREE_DELETE_POSTCONDITION_FAILED' }

        $pyArgs = '-I "' + $pyProbe + '" write_outside "' + $siblingFile + '"'
        Test-DisposableToolCompatibility 'python_outside_write_os_denied' $pyExecutable $pyArgs 'python_outside_write_denied_ok.txt' $true
        if ([IO.File]::ReadAllText($siblingFile) -cne 'DISPOSABLE-SIBLING-SENTINEL') {
            throw 'GIT47_PYTHON_EXTERNAL_SENTINEL_CORRUPTED'
        }

        $git = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $git) { throw 'GIT47_DISPOSABLE_GIT_SEED_TOOL_MISSING' }
        $seedRepo = Join-Path $turbo 'local_git_seed'
        New-Item -ItemType Directory -Path $seedRepo | Out-Null
        [IO.File]::WriteAllText((Join-Path $seedRepo 'README.txt'), 'GIT47_LOCAL_SEED')
        & ([string]$git.Source) -C $seedRepo init -q 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'GIT47_LOCAL_GIT_SEED_INIT_FAILED' }
        & ([string]$git.Source) -C $seedRepo add README.txt 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'GIT47_LOCAL_GIT_SEED_ADD_FAILED' }
        & ([string]$git.Source) -C $seedRepo -c 'user.name=Git47Fixture' -c 'user.email=fixture@example.invalid' commit -q -m seed 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'GIT47_LOCAL_GIT_SEED_COMMIT_FAILED' }
        $dulwichScript = Join-Path $turbo 'git47_dulwich_clone.py'
        $dulwichCode = @'
from pathlib import Path
import sys
try:
    from dulwich import porcelain
    porcelain.clone(sys.argv[1], sys.argv[2], checkout=True)
    if Path(sys.argv[2], "README.txt").read_text() != "GIT47_LOCAL_SEED":
        raise RuntimeError("CLONE_POSTCONDITION_MISMATCH")
    Path("dulwich_clone_verified.txt").write_text("GIT47_CLONE_PASS")
except Exception as exc:
    Path("dulwich_failure_class.txt").write_text(type(exc).__name__ + ":" + str(exc)[:120])
    raise SystemExit(51)
'@
        [IO.File]::WriteAllText($dulwichScript, $dulwichCode)
        $dulwichDest = Join-Path $turbo 'git47_dulwich_clone_target'
        $dulwichArgs = '-I "' + $dulwichScript + '" "' + $seedRepo + '" "' + $dulwichDest + '"'
        Test-DisposableToolCompatibility 'python_dulwich_local_git_clone' $pyExecutable $dulwichArgs 'dulwich_clone_verified.txt'
        $dulwichFailure = Join-Path $turbo 'dulwich_failure_class.txt'
        if (Test-Path -LiteralPath $dulwichFailure -PathType Leaf) {
            $failureClass = [IO.File]::ReadAllText($dulwichFailure)
            $failureClass = $failureClass -replace '[A-Za-z]:[\\/][^\\s''"]+', '<redacted>'
            Write-Host ('GIT47_DULWICH_FAILURE_CLASS:' + $failureClass)
        }
    } else {
        Write-Host 'GIT47_TOOL_NOT_VERIFIED:python:executable_missing'
    }
    $git = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($git) {
        Test-DisposableToolCompatibility 'git' ([string]$git.Source) 'init git_probe' 'git_probe'
        # The cmd\git.exe launcher often forwards to mingw64\bin\git.exe,
        # which is not an AppContainer-authorized external dependency. Copy
        # a self-contained minimal Git executable/DLL subtree instead.
        $gitOriginalDir = Split-Path -Parent ([string]$git.Source)
        $gitDirLeaf = Split-Path -Leaf $gitOriginalDir
        $gitRoot = if ($gitDirLeaf -in @('bin','cmd')) {
            Split-Path -Parent $gitOriginalDir
        } else { $gitOriginalDir }
        $gitMingw = Join-Path $gitRoot 'mingw64'
        $gitUcrt = Join-Path $gitRoot 'ucrt64'
        $gitVariant = if (Test-Path -LiteralPath (Join-Path $gitUcrt 'bin\\git.exe') -PathType Leaf) {
            $gitUcrt
        } else { $gitMingw }
        $gitExecPath = (& ([string]$git.Source) --exec-path 2>$null | Select-Object -First 1)
        $exeCandidates = @(Get-ChildItem -LiteralPath $gitRoot -Recurse -Filter git.exe -File -ErrorAction SilentlyContinue | Select-Object -First 12)
        $relativeCandidates = @($exeCandidates | ForEach-Object {
            $_.FullName.Substring($gitRoot.Length).TrimStart([char]92,[char]47)
        })
        Write-Host ('GIT47_GIT_LAYOUT:git_root=' + $gitRoot +
                    ';exec_path=' + [string]$gitExecPath +
                    ';executables=' + ($relativeCandidates -join ','))
        $candidates = @(
            (Join-Path $gitUcrt 'bin'),
            (Join-Path $gitMingw 'bin'),
            (Join-Path $gitRoot 'bin'),
            $gitOriginalDir
        )
        $sourceBin = @($candidates | Where-Object {
            Test-Path -LiteralPath (Join-Path $_ 'git.exe') -PathType Leaf
        } | Select-Object -First 1)
        $sourceBin = if ($sourceBin.Count) { [string]$sourceBin[0] } else { '' }
        Write-Host ("GIT47_GIT_DISCOVERY:source_dir_leaf=" + $gitDirLeaf +
          ";bin_folder_leaf=" + (Split-Path -Leaf $sourceBin) +
          ";mingw_bin_present=" +
          (Test-Path -LiteralPath (Join-Path $gitMingw 'bin\\git.exe') -PathType Leaf))
        if ($sourceBin -and (Test-Path -LiteralPath (Join-Path $sourceBin 'git.exe') -PathType Leaf)) {
            $gitDeps = Join-Path $dependencyRoot 'git'
            $gitBin = Join-Path $gitDeps 'bin'
            New-Item -ItemType Directory -Force -Path $gitBin | Out-Null
            Copy-Item -LiteralPath (Join-Path $sourceBin 'git.exe') -Destination $gitBin -Force
            foreach ($dll in Get-ChildItem -LiteralPath $sourceBin -Filter '*.dll' -File) {
                Copy-Item -LiteralPath $dll.FullName -Destination $gitBin -Force
            }
            # Git templates and helpers are read/execute dependencies only.
            $libexec = Join-Path $gitVariant 'libexec\\git-core'
            if (Test-Path -LiteralPath $libexec -PathType Container) {
                New-Item -ItemType Directory -Force -Path (Join-Path $gitDeps 'libexec') | Out-Null
                Copy-Item -LiteralPath $libexec -Destination (Join-Path $gitDeps 'libexec\\git-core') -Recurse -Force
            }
            $template = Join-Path $gitVariant 'share\\git-core'
            if (Test-Path -LiteralPath $template -PathType Container) {
                New-Item -ItemType Directory -Force -Path (Join-Path $gitDeps 'share') | Out-Null
                Copy-Item -LiteralPath $template -Destination (Join-Path $gitDeps 'share\\git-core') -Recurse -Force
            }
            $gitNative = Join-Path $gitBin 'git.exe'
            Test-DisposableToolCompatibility 'git_fixture_version' $gitNative '--version' ''
            Test-DisposableToolCompatibility 'git_fixture_rx_dependency' $gitNative 'init git_fixture_probe' 'git_fixture_probe'
            # Capture Git's actual Windows stderr inside this disposable Root;
            # emit only recognized failure class, never complete command data.
            $debugArg = '/d /c ""' + $gitNative + '" init git_cmd_probe 1>git_debug.txt 2>&1"'
            Test-DisposableToolCompatibility 'git_fixture_cmd_child' $cmdExe $debugArg 'git_cmd_probe'
            $gitDebug = Join-Path $turbo 'git_debug.txt'
            if (Test-Path -LiteralPath $gitDebug -PathType Leaf) {
                $text = [IO.File]::ReadAllText($gitDebug)
                $preview = $text.Substring(0, [Math]::Min($text.Length, 180))
                $preview = $preview -replace '[A-Za-z]:[\\/][^\\s''"]+', '<redacted-path>'
                Write-Host ('GIT47_GIT_STDERR_CLASS:' + ($preview -replace '[\\r\\n]+',' '))
                if ($text -match '(?im)^(fatal|error):\\s*([^\\r\\n]{1,120})') {
                    $reason = [string]$matches[2]
                    $reason = $reason -replace '[A-Z]:[^\\s]+', '<redacted-path>'
                    Write-Host ('GIT47_GIT_FAILURE_CLASS:' + $reason)
                }
            }
        } else {
            Write-Host 'GIT47_TOOL_NOT_VERIFIED:git_fixture_rx_dependency:mingw_git_executable_missing'
        }
    } else {
        Write-Host 'GIT47_TOOL_NOT_VERIFIED:git:executable_missing'
    }

    $ciEvidence = [ordered]@{
        schema='SMARTAGENT_GIT47_WINDOWS_DISPOSABLE_SANDBOX_EVIDENCE_V1'
        sandbox_backend='WIN32_APPCONTAINER_DISPOSABLE_FIXTURE'
        sandbox_status='TEST_ONLY'
        production_turbo_effective='OFF'
        full_access_attested=$false
        file_boundary_verdict='PASS'
        os_boundary_tests=@($script:osBoundaryEvidence)
        tool_compatibility=@($script:toolCompatibilityEvidence)
        git_for_windows_compatibility='NOT_VERIFIED_NUL_DEVICE_ACCESS_DENIED'
        network_capability='NOT_ATTESTED'
        runtime_broker='NOT_IMPLEMENTED'
    }
    [IO.File]::WriteAllText(
        (Join-Path $base 'git47_sandbox_ci_evidence.json'),
        ($ciEvidence | ConvertTo-Json -Depth 12),
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host 'GIT47_DISPOSABLE_WIN32_APPCONTAINER_FILE_BOUNDARY_PASS'
    Write-Host 'NOTE: Disposable fixture only. Production broker, tool compatibility, unsafe IPC, reparse races and revocation are not fully attested.'
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
    if ($crossVolumeFixture -and (Test-Path -LiteralPath $crossVolumeFixture)) {
        Remove-Item -LiteralPath $crossVolumeFixture -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Optional tool probes can leave LASTEXITCODE nonzero. All required OS
# assertions above throw on failure; do not confuse tool compatibility with
# the strict native boundary fixture verdict.
exit 0
