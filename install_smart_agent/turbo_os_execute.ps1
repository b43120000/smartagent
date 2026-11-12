# Git50 P3: non-elevated, protected-copy execution entrance.
# NEVER runs Planner script in this host process. Only native verified
# AppContainer LaunchPowerShellCaptured may execute the user's command.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$WorkingDirectory,
    [Parameter(Mandatory=$true)][int]$ExpectedRevision,
    [Parameter(Mandatory=$true)][string]$TaskId,
    [Parameter(Mandatory=$true)][string]$ActionId,
    [Parameter(Mandatory=$true)][string]$CommandBase64,
    [int]$TimeoutSeconds = 30,
    [int]$MaxOutputBytes = 1048576
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300 -or
    $MaxOutputBytes -lt 1 -or $MaxOutputBytes -gt 2097152) {
    throw 'GIT50_BROKER_LIMITS_INVALID'
}
if ([string]::IsNullOrWhiteSpace($TaskId) -or
    [string]::IsNullOrWhiteSpace($ActionId) -or
    $TaskId.Length -gt 128 -or $ActionId.Length -gt 128 -or
    $TaskId -notmatch '^[A-Za-z0-9._-]+$' -or
    $ActionId -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'GIT50_BROKER_ACTION_ID_INVALID'
}
$ProjectRoot=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$rootOfSource = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$secure = Join-Path $ProjectRoot 'localdata\secure\windows_security'
$expectedBrokerDir = Join-Path $secure 'turbo_broker_code'
if (-not [IO.Path]::GetFullPath($PSScriptRoot).Equals(
    [IO.Path]::GetFullPath($expectedBrokerDir),[StringComparison]::OrdinalIgnoreCase)) {
    throw 'GIT50_BROKER_NOT_RUNNING_FROM_PROTECTED_CODE_ROOT'
}
$modePath = Join-Path $secure 'acl_mode.json'
$policyPath = Join-Path $secure 'workspace_access_policy.json'
$journalPath = Join-Path $secure 'turbo_os_activation.json'
$pendingSwitchPath = Join-Path $secure 'turbo_existing_root_switch.json'
foreach($item in @($modePath,$policyPath,$journalPath)) {
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw 'GIT50_OS_BROKER_PROTECTED_STATE_MISSING'
    }
}
function Load-State([string]$Path) {
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop)
}
function Assert-Trusted-State {
    if (Test-Path -LiteralPath $pendingSwitchPath) {
        throw 'GIT50_BROKER_SWITCH_INCOMPLETE_NO_NATIVE_ACTION'
    }
    $mode = Load-State $modePath
    $policy = Load-State $policyPath
    $activation = Load-State $journalPath
    if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
        $mode.mode -ne 'on' -or
        -not [IO.Path]::GetFullPath([string]$mode.install_root).TrimEnd('\').Equals(
            $ProjectRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_ACL_NOT_ON'
    }
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
        $policy.turbo_requested -ne $true -or
        [int]$policy.revision -ne $ExpectedRevision) {
        throw 'GIT50_BROKER_POLICY_STALE_OR_DISABLED'
    }
    if ($activation.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
        $activation.status -ne 'ACTIVE_PROVISIONED' -or
        [int]$activation.policy_revision -ne $ExpectedRevision -or
        -not [IO.Path]::GetFullPath([string]$activation.install_root).TrimEnd('\').Equals(
            $ProjectRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_OS_ACTIVATION_STALE_OR_INVALID'
    }
    $workspace = [IO.Path]::GetFullPath($Workspace).TrimEnd('\')
    $root = [IO.Path]::GetFullPath([string]$activation.turbo_root).TrimEnd('\')
    $activatedWorkspace = [IO.Path]::GetFullPath([string]$activation.workspace_root).TrimEnd('\')
    if (-not $activatedWorkspace.Equals($workspace,[StringComparison]::OrdinalIgnoreCase) -or
        -not @($policy.writable_workspaces | Where-Object {
            [IO.Path]::GetFullPath([string]$_).TrimEnd('\').Equals(
                $workspace,[StringComparison]::OrdinalIgnoreCase)
        }).Count -or
        @($policy.turbo_roots).Count -ne 1 -or
        -not [IO.Path]::GetFullPath([string]$policy.turbo_roots[0]).TrimEnd('\').Equals(
            $root,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_SCOPE_POLICY_MISMATCH'
    }
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw 'GIT50_BROKER_ROOT_NOT_DIRECTORY'
    }
    $rawCwd = $WorkingDirectory.Replace('/','\')
    if (@($rawCwd.Split('\') | Where-Object {$_ -eq '..'}).Count -gt 0) {
        throw 'GIT50_BROKER_CWD_PARENT_TRAVERSAL'
    }
    $cwd = [IO.Path]::GetFullPath($WorkingDirectory).TrimEnd('\')
    if (-not $cwd.Equals($root,[StringComparison]::OrdinalIgnoreCase) -and
        -not $cwd.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_CWD_OUTSIDE_ROOT'
    }
    if (-not (Test-Path -LiteralPath $cwd -PathType Container)) {
        throw 'GIT50_BROKER_CWD_NOT_DIRECTORY'
    }
    $current = $root
    foreach ($part in @('') + @($cwd.Substring($root.Length).TrimStart([char]92).Split('\'))) {
        if (-not [string]::IsNullOrWhiteSpace($part)) {
            $current = Join-Path $current $part
        }
        if ((Get-Item -LiteralPath $current -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) {
            throw 'GIT50_BROKER_REPARSE_CWD_FORBIDDEN'
        }
    }
    $sentinel = [IO.Path]::GetFullPath([string]$activation.negative_sentinel_path)
    $parent = [IO.Path]::GetDirectoryName($root)
    if (-not [IO.Path]::GetDirectoryName($sentinel).Equals(
        $parent,[StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($sentinel).StartsWith('.git50-os-denial-') -or
        -not (Test-Path -LiteralPath $sentinel -PathType Leaf) -or
        [IO.File]::ReadAllText($sentinel) -cne 'OUTSIDE_SENTINEL') {
        throw 'GIT50_BROKER_NEGATIVE_SENTINEL_UNVERIFIED'
    }
    if ([string]$activation.package_sid -notmatch '^S-1-15-2-') {
        throw 'GIT50_BROKER_PACKAGE_SID_INVALID'
    }
    return @{
        mode=$mode; policy=$policy; activation=$activation
        workspace=$workspace; root=$root; cwd=$cwd; sentinel=$sentinel
    }
}
# Broker execution and ACL OFF revocation use the same global mutex.
$hash=[Security.Cryptography.SHA256]::Create()
try {
    $digest=[BitConverter]::ToString(
        $hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToUpperInvariant()))
    ).Replace('-','')
} finally { $hash.Dispose() }
$mutex=New-Object Threading.Mutex($false,('Global\SmartAgentTurbo-'+$digest.Substring(0,32)))
$held=$false
try {
    try { $held=$mutex.WaitOne([TimeSpan]::FromSeconds(305)) }
    catch [Threading.AbandonedMutexException] { $held=$true }
    if (-not $held) { throw 'GIT50_BROKER_MUTEX_TIMEOUT' }
    $state=Assert-Trusted-State
    if (-not ('SmartAgentGit47AppContainerProbe' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -ErrorAction Stop
    }
    $moniker=[string]$state.activation.moniker
    $sid=[SmartAgentGit47AppContainerProbe]::DeriveSid($moniker)
    if (-not $sid.Equals([string]$state.activation.package_sid,
                         [StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_BROKER_CHILD_SID_NOT_BOUND_TO_ACTIVATION'
    }
    # Independently confirm the *real* AppContainer cannot read the known
    # outside sentinel, and can write a disposable marker inside. This is a
    # kernel denial probe, NOT a text/Planner/JSON assertion.
    $outsideLiteral=$state.sentinel.Replace("'","''")
    $proof=@"
try { [void][IO.File]::ReadAllText('$outsideLiteral'); exit 91 }
catch [System.UnauthorizedAccessException] {
    [IO.File]::WriteAllText('git50_scope_proof.tmp','OS_DENIED')
    exit 0
}
"@
    $os=[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        $moniker,$proof,$state.root,[uint32]30000) | ConvertFrom-Json
    if ($os.sandbox_identity_verified -ne $true -or
        $os.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        [int]$os.exit_code -ne 0 -or
        [IO.File]::ReadAllText((Join-Path $state.root 'git50_scope_proof.tmp')) -cne 'OS_DENIED') {
        throw 'GIT50_BROKER_KERNEL_NEGATIVE_PROBE_FAILED'
    }
    Remove-Item -LiteralPath (Join-Path $state.root 'git50_scope_proof.tmp') -Force
    $state=Assert-Trusted-State
    try {
        $script=[Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($CommandBase64))
    } catch { throw 'GIT50_BROKER_COMMAND_BASE64_INVALID' }
    if ([string]::IsNullOrWhiteSpace($script) -or $script.Length -gt 8192) {
        throw 'GIT50_BROKER_SCRIPT_LENGTH_INVALID'
    }
    # The native command is the *only* path that receives untrusted script.
    # The elevated/setup PowerShell code must never invoke the command.
    $nativeRaw=[SmartAgentGit47AppContainerProbe]::LaunchPowerShellCaptured(
        $moniker,$script,$state.cwd,[uint32]($TimeoutSeconds*1000),
        [int]$MaxOutputBytes)
    $native=$nativeRaw | ConvertFrom-Json -ErrorAction Stop
    if ($native.sandbox_identity_verified -ne $true -or
        $native.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
        $native.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        $native.os_process_started -ne $true -or
        $native.output_is_authoritative -ne $false) {
        throw 'GIT50_BROKER_KERNEL_PROCESS_RECEIPT_INVALID'
    }
    $fresh=Assert-Trusted-State
    $response=[ordered]@{
        schema='SMARTAGENT_TURBO_OS_EXECUTION_RECEIPT_V1'
        task_id=$TaskId
        action_id=$ActionId
        policy_revision=$ExpectedRevision
        workspace_root=[string]$state.workspace
        turbo_root=[string]$state.root
        working_directory=[string]$state.cwd
        acl_mode='on'
        sandbox_backend='WIN32_APPCONTAINER'
        sandbox_identity_verified=$true
        child_process_policy='JOB_KILL_ON_CLOSE'
        kernel_outside_read_denied=$true
        process_started=$true
        executed=$true
        command_exit_code=[int64]$native.exit_code
        stdout_base64=[string]$native.stdout_base64
        stderr_base64=[string]$native.stderr_base64
        stdout_bytes=[int64]$native.stdout_bytes
        stderr_bytes=[int64]$native.stderr_bytes
        stdout_truncated=[bool]$native.stdout_truncated
        stderr_truncated=[bool]$native.stderr_truncated
        output_is_authoritative=$false
        authorization_scope='THIS_SINGLE_APP_CONTAINER_ACTION'
        production_turbo_full_access_attested=$false
        status='EXECUTED_IN_OS_APPCONTAINER'
    }
    # Only a direct OS-spawn receipt can mark *this action* executed; the
    # global Turbo setting is not promoted by this JSON response.
    $response | ConvertTo-Json -Compress -Depth 8
}
finally {
    if ($held) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
