# Git50 P2 real-Windows provision/revoke of a disposable *new* Turbo root.
# This is not a user E: or a pre-existing project. Never run against real data.
[CmdletBinding()]
param([string]$FixtureParent = $env:RUNNER_TEMP)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $FixtureParent) { $FixtureParent=[IO.Path]::GetTempPath() }
$fixture = Join-Path ([IO.Path]::GetFullPath($FixtureParent)) ('Git50ActivationIT-' + [Guid]::NewGuid().ToString('N'))
$project = Join-Path $fixture 'project'
$secure = Join-Path $project 'localdata\secure\windows_security'
$workspace = Join-Path $fixture 'workspace'
$root = Join-Path $workspace 'turbo_new'
$activation = Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
$autoActivation = Join-Path $PSScriptRoot '..\turbo_auto_activate.ps1'
try {
    New-Item -ItemType Directory -Force -Path $secure,$workspace | Out-Null
    $mode=[ordered]@{
        schema='SMARTAGENT_ACL_MODE_V1'; mode='on'; install_root=$project
    }
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),
        ($mode | ConvertTo-Json), [Text.Encoding]::UTF8)
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=77
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($root)
        turbo_requested=$true
    }
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),
        ($policy | ConvertTo-Json), [Text.Encoding]::UTF8)
    if (Test-Path -LiteralPath $root) { throw 'GIT50_FIXTURE_NEW_ROOT_EXISTS' }

    # Exercise the production ACL ON / Workspace Apply auto-enrollment entrypoint.
    & $autoActivation -ProjectRoot $project -Action Apply
    $journalPath = Join-Path $secure 'turbo_os_activation.json'
    if (-not (Test-Path -LiteralPath $root -PathType Container) -or
        -not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
        throw 'GIT50_ACTIVE_ROOT_NOT_CREATED'
    }
    $journal = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
    if ($journal.status -ne 'ACTIVE_PROVISIONED' -or
        $journal.policy_revision -ne 77 -or
        [string]$journal.package_sid -notmatch '^S-1-15-2-') {
        throw 'GIT50_ACTIVATION_JOURNAL_INVALID'
    }
    $sid = [string]$journal.package_sid
    $rootSddl = (Get-Acl -LiteralPath $root).Sddl
    if ($rootSddl -notmatch [Regex]::Escape($sid)) {
        throw 'GIT50_ROOT_PACKAGE_SID_NOT_GRANTED'
    }
    # Check actual native command execution against the provisioned root.
    $marker = Join-Path $root 'actual_turbo_work.txt'
    $code = "[IO.File]::WriteAllText('actual_turbo_work.txt','GIT50_WORK');"
    $r = [SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        [string]$journal.moniker, $code, $root, [uint32]30000) | ConvertFrom-Json
    if (-not $r.sandbox_identity_verified -or
        $r.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        [int]$r.exit_code -ne 0 -or
        [IO.File]::ReadAllText($marker) -cne 'GIT50_WORK') {
        throw 'GIT50_PROVISIONED_ROOT_CHILD_EXECUTION_FAILED'
    }
    Write-Host 'GIT50_NEW_ROOT_APP_CONTAINER_ACTIVATION_PASS'

    # Scheme B's desired policy: outside the Turbo Root, ONLY an
    # explicitly read-authorized path may be read. Even there the kernel
    # must reject writes. All changes are to this disposable CI sentinel;
    # no persistent grant to a real user directory is created.
    $outsideReadOnly=Join-Path $workspace ('turbo-ro-only-'+[Guid]::NewGuid().ToString('N')+'.txt')
    [IO.File]::WriteAllText($outsideReadOnly,'READ_ONLY_ORIGINAL')
    try {
        $sidObject=[Security.Principal.SecurityIdentifier]::new($sid)
        $outsideAcl=Get-Acl -LiteralPath $outsideReadOnly
        $outsideRule=[Security.AccessControl.FileSystemAccessRule]::new(
            $sidObject,[Security.AccessControl.FileSystemRights]::Read,
            [Security.AccessControl.InheritanceFlags]::None,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        [void]$outsideAcl.AddAccessRule($outsideRule)
        Set-Acl -LiteralPath $outsideReadOnly -AclObject $outsideAcl -ErrorAction Stop
        $literal=$outsideReadOnly.Replace("'","''")
        $readOnlyProof=@"
try {
    if ([IO.File]::ReadAllText('$literal') -cne 'READ_ONLY_ORIGINAL') { exit 97 }
    try { [IO.File]::WriteAllText('$literal','ILLEGAL_WRITE'); exit 98 }
    catch [System.UnauthorizedAccessException] { exit 0 }
} catch { exit 99 }
"@
        $proof=[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
            [string]$journal.moniker,$readOnlyProof,$root,[uint32]30000) | ConvertFrom-Json
        if ($proof.sandbox_identity_verified -ne $true -or
            $proof.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
            [int]$proof.exit_code -ne 0 -or
            [IO.File]::ReadAllText($outsideReadOnly) -cne 'READ_ONLY_ORIGINAL') {
            throw ('TURBO_B_NATIVE_OUTSIDE_READONLY_KERNEL_DENIAL_FAILED:exit='+$proof.exit_code)
        }
        Write-Host 'TURBO_B_NATIVE_EXPLICIT_READONLY_OUTSIDE_WRITE_DENIED_PASS'
    } finally {
        Remove-Item -LiteralPath $outsideReadOnly -Force -ErrorAction SilentlyContinue
    }

    # The runtime-facing broker must execute the untrusted command ONLY
    # inside the AppContainer and return real output + kernel exit status.
    $brokerCommand=[Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes("'GIT50_BROKER_EXECUTION_OUTPUT'"))
    $brokerJson=& ([string]$journal.broker_entry) -ProjectRoot $project -Workspace $workspace -WorkingDirectory $root -ExpectedRevision 77 -TaskId 'TASK-GIT50' -ActionId 'ACT-GIT50' -CommandBase64 $brokerCommand -TimeoutSeconds 30
    $brokerReceipt=($brokerJson | Out-String | ConvertFrom-Json -ErrorAction Stop)
    $brokerOutput=[Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([string]$brokerReceipt.stdout_base64)).Trim()
    if ($brokerReceipt.sandbox_identity_verified -ne $true -or
        $brokerReceipt.kernel_outside_read_denied -ne $true -or
        $brokerReceipt.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        [int]$brokerReceipt.command_exit_code -ne 0 -or
        $brokerOutput -cne 'GIT50_BROKER_EXECUTION_OUTPUT') {
        throw 'GIT50_BROKER_EXECUTION_NOT_PROVEN'
    }
    Write-Host 'GIT50_PROTECTED_OS_BROKER_COMMAND_AND_CAPTURE_PASS'

    # Git50 P3 integration uses the real SmartAgent run_bounded_process,
    # not a direct test-only native launcher call. Python monkeypatches
    # only where the disposable fixture lives; the actual kernel token
    # and independent OS denial remain mandatory inside the protected broker.
    $pythonTest = Join-Path $PSScriptRoot '..\..\source\WebAgent\tests\git50_runtime_os_broker_disposable_integration.py'
    if (-not (Test-Path -LiteralPath $pythonTest -PathType Leaf)) {
        throw 'GIT50_PYTHON_RUNTIME_BROKER_TEST_MISSING'
    }
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = [IO.Path]::GetFullPath(
            (Join-Path $PSScriptRoot '..\..\source'))
        & python -B $pythonTest --project-root $project --workspace $workspace --turbo-root $root
        if ($LASTEXITCODE -ne 0) {
            throw 'GIT50_PYTHON_RUNTIME_BROKER_INTEGRATION_FAILED'
        }
    } finally {
        $env:PYTHONPATH = $oldPythonPath
    }

    # Mode can transition during task cancellation. Revocation MUST still
    # work if the mode is already transitioning_off (not currently 'on').
    $mode.mode='transitioning_off'
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),
        ($mode | ConvertTo-Json), [Text.Encoding]::UTF8)
    & $activation -ProjectRoot $project -Action Revoke
    if (Test-Path -LiteralPath $journalPath) {
        throw 'GIT50_REVOKE_JOURNAL_STILL_PRESENT'
    }
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw 'GIT50_REVOKE_DELETED_USER_OUTPUT'
    }
    if ((Get-Acl -LiteralPath $root).Sddl -match [Regex]::Escape($sid) -or
        (Get-Acl -LiteralPath $marker).Sddl -match [Regex]::Escape($sid)) {
        throw 'GIT50_REVOKE_APP_CONTAINER_ACE_STILL_PRESENT'
    }
    Write-Host 'GIT50_NEW_ROOT_ACTIVATION_AND_REVOCATION_PASS'

    # The empty-root constraint fails safely without changing a populated tree.
    $populated=Join-Path $workspace 'existing'
    New-Item -ItemType Directory -Path $populated | Out-Null
    [IO.File]::WriteAllText((Join-Path $populated 'do_not_touch.txt'),'ORIGINAL')
    $policy.turbo_roots=@($populated)
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),
        ($policy | ConvertTo-Json), [Text.Encoding]::UTF8)
    $mode.mode='on'
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),
        ($mode | ConvertTo-Json), [Text.Encoding]::UTF8)
    $rejected=$false
    try { & $autoActivation -ProjectRoot $project -Action Apply }
    catch {
        $rejected = $_.Exception.Message -match 'TURBO_AUTOSTART_EXISTING_ROOT_IMPORT_UNSUPPORTED'
    }
    if (-not $rejected -or
        [IO.File]::ReadAllText((Join-Path $populated 'do_not_touch.txt')) -cne 'ORIGINAL') {
        throw 'GIT50_EXISTING_ROOT_PRECONDITION_DID_NOT_FAIL_CLOSED'
    }
    Write-Host 'GIT50_EXISTING_TREE_NO_MODIFICATION_PASS'
}
finally {
    if (Test-Path -LiteralPath $project) {
        $journalPath=Join-Path $project 'localdata\secure\windows_security\turbo_os_activation.json'
        if (Test-Path -LiteralPath $journalPath) {
            try { & $activation -ProjectRoot $project -Action Revoke } catch {
                Write-Warning 'GIT50_ACTIVATION_FIXTURE_REVOKE_NEEDS_INSPECTION'
            }
        }
    }
    # Clean fixture only after verifying it is still inside the unique
    # disposable test parent and the journal was safely revoked.
    if ((Test-Path -LiteralPath $fixture) -and
        -not (Test-Path -LiteralPath $journalPath)) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
    }
}
