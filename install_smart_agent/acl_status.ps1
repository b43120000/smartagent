[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('on','off')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [string]$ControllerSid = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$SecurityRoot = Join-Path $ProjectRoot 'localdata\secure\windows_security'
$ProfilePath = Join-Path $SecurityRoot 'security_profile.json'
$AccessPolicyPath = Join-Path $SecurityRoot 'workspace_access_policy.json'
$ModePath = Join-Path $SecurityRoot 'acl_mode.json'
$TaskName = 'SmartAgent Restricted Executor'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'ACL mode changes require an elevated administrator process.'
}
foreach ($required in @('install_smart_agent.bat','install_smart_agent\configure_security.ps1','source','localdata')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $required))) {
        throw "Invalid SmartAgent root; missing: $required"
    }
}

function Invoke-Icacls([string[]]$Arguments) {
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed (exit=$LASTEXITCODE): $($Arguments -join ' ')" }
}

function Assert-NoReparsePoints([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return }
    $root = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($root.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "acl_mode_reparse_point_forbidden:$Path" }
    $reparse = Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
        Select-Object -First 1
    if ($reparse) { throw "acl_mode_reparse_point_forbidden:$($reparse.FullName)" }
}

function Write-ModeState([string]$Value, [string]$ExecutorSid, [string]$ControllerSid) {
    $directory = Split-Path -Parent $ModePath
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $payload = [ordered]@{
        schema = 'SMARTAGENT_ACL_MODE_V1'
        mode = $Value
        install_root = $ProjectRoot
        controller_sid = $ControllerSid
        updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    $temporary = "$ModePath.tmp"
    [IO.File]::WriteAllText($temporary, ($payload | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $ModePath -Force
    $modeAcl = @($ModePath,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F','*S-1-5-32-545:R')
    if ($ControllerSid -match '^S-1-' -and $ControllerSid -ne 'S-1-5-32-545') { $modeAcl += "*$ControllerSid`:R" }
    if ($ExecutorSid -match '^S-1-') { $modeAcl += "*$ExecutorSid`:R" }
    Invoke-Icacls $modeAcl
}

function Assert-SafeInstallRoot([string]$Path) {
    $normalized = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $pathRoot = [IO.Path]::GetPathRoot($normalized).TrimEnd('\')
    if ($normalized.Equals($pathRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "acl_mode_install_root_must_not_be_drive_or_share_root:$normalized"
    }
    if (-not (Test-Path -LiteralPath $normalized -PathType Container)) {
        throw "acl_mode_install_root_missing:$normalized"
    }
    $item = Get-Item -LiteralPath $normalized -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "acl_mode_reparse_point_forbidden:$normalized"
    }
}

function Assert-ContainedPathHasNoReparseAncestor([string]$BasePath, [string]$CandidatePath) {
    $base = [IO.Path]::GetFullPath($BasePath).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath($CandidatePath).TrimEnd('\')
    if (-not $candidate.Equals($base, [StringComparison]::OrdinalIgnoreCase) -and
        -not $candidate.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "acl_mode_owned_path_outside_install_root:$candidate"
    }
    $relative = if ($candidate.Equals($base, [StringComparison]::OrdinalIgnoreCase)) {
        ''
    } else {
        $candidate.Substring($base.Length + 1)
    }
    $current = $base
    $segments = if ([string]::IsNullOrWhiteSpace($relative)) { @() } else { @($relative.Split('\')) }
    foreach ($segment in @('') + $segments) {
        if (-not [string]::IsNullOrWhiteSpace($segment)) { $current = Join-Path $current $segment }
        if (-not (Test-Path -LiteralPath $current)) { continue }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "acl_mode_reparse_point_forbidden:$current"
        }
    }
}

function Assert-AclOffPostcondition([string]$ControllerSid, [string[]]$WritableRoots) {
    if ($ControllerSid -notmatch '^S-1-') {
        throw 'acl_off_postcondition_invalid_controller_sid'
    }
    foreach ($writableRoot in @($WritableRoots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $writableRoot -PathType Container)) {
            throw "acl_off_postcondition_writable_root_missing:$writableRoot"
        }
        $rootAcl = Get-Acl -LiteralPath $writableRoot -ErrorAction Stop
        $grantedRights = [Security.AccessControl.FileSystemRights]0
        foreach ($rule in @($rootAcl.Access)) {
            try {
                $ruleSid = $rule.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]
                ).Value
            } catch {
                continue
            }
            if ($ruleSid -ne $ControllerSid) { continue }
            if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny -and
                (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne 0)) {
                throw "acl_off_postcondition_controller_write_denied:$writableRoot"
            }
            if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
                $grantedRights = $grantedRights -bor $rule.FileSystemRights
            }
        }
        $requiredRights = [Security.AccessControl.FileSystemRights]::FullControl
        if (($grantedRights -band $requiredRights) -ne $requiredRights) {
            throw "acl_off_postcondition_controller_full_control_missing:$writableRoot"
        }

        $probeId = [Guid]::NewGuid().ToString('N')
        $probeSource = Join-Path $writableRoot ".acl-off-write-probe-$probeId.tmp"
        $probeDestination = Join-Path $writableRoot ".acl-off-write-probe-$probeId.verified"
        try {
            [IO.File]::WriteAllText($probeSource, 'SMARTAGENT_ACL_OFF_WRITE_PROBE', (New-Object Text.UTF8Encoding($false)))
            Move-Item -LiteralPath $probeSource -Destination $probeDestination -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $probeDestination -PathType Leaf)) {
                throw 'acl_off_write_probe_destination_missing'
            }
        } catch {
            throw "acl_off_postcondition_write_probe_failed:${writableRoot}:$($_.Exception.Message)"
        } finally {
            Remove-Item -LiteralPath $probeSource,$probeDestination -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-AclOffOwnedRoots(
    [string]$InstallRoot,
    [string]$LocalSecurityRoot,
    [object]$SecurityProfile,
    [bool]$UseProfileOwnedRoots
) {
    $normalizedInstall = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    $candidates = @(
        $LocalSecurityRoot,
        (Join-Path $normalizedInstall 'localdata\runtime\security')
    )
    if ($SecurityProfile -and $UseProfileOwnedRoots) {
        $candidates += @(
            $SecurityProfile.runtime_root,
            $SecurityProfile.executor_endpoint,
            $SecurityProfile.controller_state_root,
            $SecurityProfile.executor_code_root
        )
        $candidates += @($SecurityProfile.skill_roots)
    }
    $insideInstall = @($candidates |
        Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
        ForEach-Object { [IO.Path]::GetFullPath([string]$_).TrimEnd('\') } |
        Where-Object {
            $_.StartsWith($normalizedInstall + '\', [StringComparison]::OrdinalIgnoreCase)
        } |
        Select-Object -Unique | Sort-Object Length)
    $ownedRoots = @()
    foreach ($candidateRoot in $insideInstall) {
        $covered = $false
        foreach ($existingRoot in $ownedRoots) {
            if ($candidateRoot.Equals($existingRoot, [StringComparison]::OrdinalIgnoreCase) -or
                $candidateRoot.StartsWith($existingRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
                $covered = $true
                break
            }
        }
        if (-not $covered) { $ownedRoots += $candidateRoot }
    }
    return $ownedRoots
}

function Resolve-ProfilePath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    return [IO.Path]::GetFullPath($Value)
}

function Get-SecurityProvisioningOptions {
    $optionsPath = Join-Path $ProjectRoot 'localdata\metadata\provisioning_options.json'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $defaults = [ordered]@{
        workspace_container = (Join-Path $desktop 'SmartAgentWorkspace\default')
        additional_write_roots = @()
        runtime_root = (Join-Path $ProjectRoot 'localdata\runtime\security')
        denied_write_roots = @($env:PUBLIC, (Join-Path $env:WINDIR 'Temp'))
        read_only_roots = @()
        skill_root = (Join-Path $SecurityRoot 'skills')
        skill_source_path = (Join-Path $env:USERPROFILE '.codex\skills')
        executor_user = 'SmartAgentExecutor'
    }
    if (Test-Path -LiteralPath $optionsPath -PathType Leaf) {
        try {
            $saved = Get-Content -LiteralPath $optionsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            throw 'acl_on_provisioning_options_invalid'
        }
        $mapping = [ordered]@{
            security_workspace_container = 'workspace_container'
            security_additional_write_roots = 'additional_write_roots'
            security_runtime_root = 'runtime_root'
            security_denied_write_roots = 'denied_write_roots'
            security_read_only_roots = 'read_only_roots'
            security_skill_root = 'skill_root'
            security_skill_source_path = 'skill_source_path'
        }
        foreach ($sourceName in $mapping.Keys) {
            if ($saved.PSObject.Properties.Name -contains $sourceName -and $null -ne $saved.$sourceName) {
                $targetName = $mapping[$sourceName]
                $defaults[$targetName] = $saved.$sourceName
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$defaults.workspace_container)) {
        $defaults.workspace_container = Join-Path $desktop 'SmartAgentWorkspace\default'
    }
    if ([string]::IsNullOrWhiteSpace([string]$defaults.runtime_root)) {
        $defaults.runtime_root = Join-Path $ProjectRoot 'localdata\runtime\security'
    }
    if ([string]::IsNullOrWhiteSpace([string]$defaults.skill_root)) {
        $defaults.skill_root = Join-Path $SecurityRoot 'skills'
    }
    return [pscustomobject]$defaults
}

function Get-WorkspaceAccessPolicy {
    if (-not (Test-Path -LiteralPath $AccessPolicyPath -PathType Leaf)) { return $null }
    try {
        $policy = Get-Content -LiteralPath $AccessPolicyPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw 'acl_workspace_access_policy_invalid_json'
    }
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1') {
        throw 'acl_workspace_access_policy_invalid_schema'
    }
    $writable = @($policy.writable_workspaces | ForEach-Object {
        if (-not [string]::IsNullOrWhiteSpace([string]$_)) { [IO.Path]::GetFullPath([string]$_) }
    })
    if ($writable.Count -lt 1) { throw 'acl_workspace_access_policy_has_no_writable_workspace' }
    return [pscustomobject]@{
        writable_workspaces = @($writable | Select-Object -Unique)
        read_only_roots = @($policy.read_only_roots | ForEach-Object {
            if (-not [string]::IsNullOrWhiteSpace([string]$_)) { [IO.Path]::GetFullPath([string]$_) }
        } | Select-Object -Unique)
        denied_write_roots = @($policy.denied_write_roots | ForEach-Object {
            if (-not [string]::IsNullOrWhiteSpace([string]$_)) { [IO.Path]::GetFullPath([string]$_) }
        } | Select-Object -Unique)
    }
}

function Assert-OnEnvironmentReady {
    $executorPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $executorPython -PathType Leaf)) {
        throw "ACL_ON_ENVIRONMENT_NOT_READY:missing=$executorPython; run install_smart_agent.bat"
    }
    $protocolManifest = Join-Path $ProjectRoot 'config\protocol_manifest.json'
    if (-not (Test-Path -LiteralPath $protocolManifest -PathType Leaf)) {
        throw "ACL_ON_ENVIRONMENT_NOT_READY:missing=$protocolManifest; run install_smart_agent.bat"
    }
    return $executorPython
}

function Read-MachineAuthorization {
    $path = Join-Path ([string]$env:ProgramData) 'SmartAgent\machine_authorization.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [pscustomobject]@{ state='MISSING'; path=$path; value=$null }
    }
    try {
        $value = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($value.schema -ne 'SMARTAGENT_MACHINE_AUTHORIZATION_V1') { throw 'schema' }
        $authorizedRoot = [IO.Path]::GetFullPath([string]$value.install_root).TrimEnd('\')
        $authorizedProfile = [IO.Path]::GetFullPath([string]$value.security_profile)
        if (-not $authorizedRoot.Equals($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -or
            -not $authorizedProfile.Equals($ProfilePath, [StringComparison]::OrdinalIgnoreCase)) {
            return [pscustomobject]@{ state='FOREIGN'; path=$path; value=$value }
        }
        return [pscustomobject]@{ state='BOUND'; path=$path; value=$value }
    } catch {
        return [pscustomobject]@{ state='INVALID'; path=$path; value=$null }
    }
}

function Wait-ExecutorReady([string]$ExpectedMode, [string]$Endpoint) {
    $statePath = Join-Path $Endpoint 'service_state.json'
    $deadline = (Get-Date).AddSeconds(35)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            try {
                $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
                $selfTest = $state.self_test
                $fresh = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$state.heartbeat_at) -lt 10
                if ($fresh -and $selfTest.passed -eq $true -and $selfTest.mode -eq $ExpectedMode) { return }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "restricted_executor_mode_transition_failed:$ExpectedMode"
}

$mutexBytes = [Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToUpperInvariant())
$mutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $mutexHash = ([BitConverter]::ToString($mutexHasher.ComputeHash($mutexBytes))).Replace('-', '')
} finally {
    $mutexHasher.Dispose()
}
$transitionMutex = New-Object Threading.Mutex($false, "Global\SmartAgentAclMode_$mutexHash")
$transitionMutexHeld = $false
try {
    try {
        $transitionMutexHeld = $transitionMutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $transitionMutexHeld = $true
    }
    if (-not $transitionMutexHeld) {
        throw 'acl_mode_transition_already_in_progress'
    }

    Assert-SafeInstallRoot $ProjectRoot
    Assert-ContainedPathHasNoReparseAncestor $ProjectRoot $SecurityRoot

$profile = $null
$profileFileExists = Test-Path -LiteralPath $ProfilePath -PathType Leaf
if (Test-Path -LiteralPath $ProfilePath -PathType Leaf) {
    try { $profile = Get-Content -LiteralPath $ProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'acl_security_profile_invalid_json' }
}
$expectedCodeRoot = Join-Path $SecurityRoot 'executor_code'
$profileBoundToRoot = $false
$executorSid = ''
if ($profile) {
    $validProfile = $profile.schema -eq 'SMARTAGENT_WINDOWS_SECURITY_V2' -and
        ([string]$profile.executor_sid -match '^S-1-') -and
        ([string]$profile.executor_user -match '^[A-Za-z0-9._-]{1,20}$')
    if ($validProfile -and (Resolve-ProfilePath ([string]$profile.executor_code_root)).Equals(
        [IO.Path]::GetFullPath($expectedCodeRoot), [StringComparison]::OrdinalIgnoreCase
    )) {
        $machineAuthorizationPath = Join-Path ([string]$env:ProgramData) 'SmartAgent\machine_authorization.json'
        if (Test-Path -LiteralPath $machineAuthorizationPath -PathType Leaf) {
            try {
                $machineAuthorization = Get-Content -LiteralPath $machineAuthorizationPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $authorizedProfile = [IO.Path]::GetFullPath([string]$machineAuthorization.security_profile)
                $authorizedRoot = [IO.Path]::GetFullPath([string]$machineAuthorization.install_root).TrimEnd('\')
                $profileBoundToRoot =
                    $machineAuthorization.schema -eq 'SMARTAGENT_MACHINE_AUTHORIZATION_V1' -and
                    $authorizedRoot.Equals($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -and
                    $authorizedProfile.Equals($ProfilePath, [StringComparison]::OrdinalIgnoreCase) -and
                    [string]$machineAuthorization.executor_sid -eq [string]$profile.executor_sid
            } catch { $profileBoundToRoot = $false }
        }
        if ($profileBoundToRoot) {
            $executorSid = [string]$profile.executor_sid
            $localExecutor = Get-LocalUser -Name ([string]$profile.executor_user) -ErrorAction SilentlyContinue
            if ($localExecutor) { $executorSid = [string]$localExecutor.SID }
        }
    }
}
$machineAuthorizationState = Read-MachineAuthorization
$profileStructurallyValid = $false
if ($profile) {
    $profileStructurallyValid = $profile.schema -eq 'SMARTAGENT_WINDOWS_SECURITY_V2' -and
        ([string]$profile.executor_sid -match '^S-1-') -and
        ([string]$profile.executor_user -match '^[A-Za-z0-9._-]{1,20}$') -and
        -not [string]::IsNullOrWhiteSpace([string]$profile.workspace_container) -and
        -not [string]::IsNullOrWhiteSpace([string]$profile.runtime_root) -and
        -not [string]::IsNullOrWhiteSpace([string]$profile.executor_python) -and
        (Resolve-ProfilePath ([string]$profile.executor_code_root)).Equals(
            [IO.Path]::GetFullPath($expectedCodeRoot), [StringComparison]::OrdinalIgnoreCase
        )
}
$previousModeState = $null
if (Test-Path -LiteralPath $ModePath -PathType Leaf) {
    try {
        $candidateModeState = Get-Content -LiteralPath $ModePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $candidateRoot = [IO.Path]::GetFullPath([string]$candidateModeState.install_root).TrimEnd('\')
        if ($candidateModeState.schema -eq 'SMARTAGENT_ACL_MODE_V1' -and
            $candidateRoot.Equals($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -and
            $candidateModeState.mode -in @('on','off')) {
            $previousModeState = $candidateModeState
        }
    } catch { $previousModeState = $null }
}
$callerSid = if ($ControllerSid -match '^S-1-') { $ControllerSid } else { [string]$env:SMARTAGENT_ACL_CALLER_SID }
if ($callerSid -notmatch '^S-1-' -and $previousModeState -and [string]$previousModeState.controller_sid -match '^S-1-') {
    $callerSid = [string]$previousModeState.controller_sid
}
if ($callerSid -notmatch '^S-1-') { $callerSid = [string]$identity.User.Value }

if ($Mode -eq 'on') {
    $defaultExecutorPython = Assert-OnEnvironmentReady
    $accessPolicy = Get-WorkspaceAccessPolicy
    if ($profileFileExists -and -not $profileStructurallyValid) {
        throw 'ACL_ON_SECURITY_STATE_INVALID:security_profile_invalid; use reinstall_smart_agent.bat to remove the inconsistent security state before retrying'
    }
    if ($machineAuthorizationState.state -eq 'FOREIGN') {
        throw "ACL_ON_SECURITY_STATE_CONFLICT:machine_authorization_belongs_to_another_installation:$($machineAuthorizationState.path)"
    }
    if ($machineAuthorizationState.state -eq 'INVALID') {
        throw "ACL_ON_SECURITY_STATE_INVALID:machine_authorization_invalid:$($machineAuthorizationState.path)"
    }
    if (-not $profile -and $machineAuthorizationState.state -eq 'BOUND') {
        throw 'ACL_ON_SECURITY_STATE_INCOMPLETE:machine_authorization_exists_but_security_profile_is_missing'
    }
    if (-not $profile -and $machineAuthorizationState.state -eq 'MISSING') {
        $unboundTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($unboundTask) {
            Write-Warning "The machine-wide '$TaskName' task exists, but no local profile or machine authorization proves that it belongs to this installation."
            $repairAnswer = Read-Host 'Type REPAIR to bind and re-attest that task for this installation'
            if ($repairAnswer -cne 'REPAIR') {
                throw 'ACL_ON_SECURITY_STATE_INCOMPLETE:unbound_executor_task_requires_explicit_repair'
            }
        }
    }
    $configure = Join-Path $PSScriptRoot 'configure_security.ps1'
    if ($profileStructurallyValid) {
        $writeRoots = if ($accessPolicy) {
            @($accessPolicy.writable_workspaces)
        } else {
            @($profile.authorized_workspace_roots | ForEach-Object { [string]$_ })
        }
        $workspace = [string]$writeRoots[0]
        $additionalWriteRoots = @($writeRoots | Where-Object {
            -not ([IO.Path]::GetFullPath($_).Equals([IO.Path]::GetFullPath($workspace), [StringComparison]::OrdinalIgnoreCase))
        })
        $builtInReadOnlyRoots = @([string]$profile.executor_code_root) + @($profile.skill_roots | ForEach-Object { [string]$_ })
        $readOnlyRoots = if ($accessPolicy) {
            @($accessPolicy.read_only_roots)
        } else {
            @($profile.read_only_roots | ForEach-Object { [string]$_ } | Where-Object {
                $candidate = [IO.Path]::GetFullPath($_)
                -not @($builtInReadOnlyRoots | Where-Object {
                    [IO.Path]::GetFullPath($_).Equals($candidate, [StringComparison]::OrdinalIgnoreCase)
                }).Count
            })
        }
        $deniedWriteRoots = if ($accessPolicy) {
            @($accessPolicy.denied_write_roots)
        } else {
            @($profile.denied_write_roots | ForEach-Object { [string]$_ })
        }
        $skillRoot = if (@($profile.skill_roots).Count -gt 0) { [string]$profile.skill_roots[0] } else { Join-Path $SecurityRoot 'skills' }
        Write-Host '[SmartAgent ACL] Re-applying the complete security profile...' -ForegroundColor Cyan
        & $configure `
            -WorkspaceContainer $workspace `
            -AdditionalWriteRoots $additionalWriteRoots `
            -RuntimeRoot ([string]$profile.runtime_root) `
            -DeniedWriteRoots $deniedWriteRoots `
            -ReadOnlyRoots $readOnlyRoots `
            -SkillRoot $skillRoot `
            -ExecutorUser ([string]$profile.executor_user) `
            -ControllerSid $callerSid `
            -ProfilePath $ProfilePath `
            -ExecutorPython $defaultExecutorPython `
            -SkipAclModeRestore -NonInteractive -Apply
    } else {
        $options = Get-SecurityProvisioningOptions
        if ($accessPolicy) {
            $options.workspace_container = [string]$accessPolicy.writable_workspaces[0]
            $options.additional_write_roots = @($accessPolicy.writable_workspaces | Select-Object -Skip 1)
            $options.read_only_roots = @($accessPolicy.read_only_roots)
            $options.denied_write_roots = @($accessPolicy.denied_write_roots)
        }
        Write-Host '[SmartAgent ACL] No completed security profile was found.' -ForegroundColor Yellow
        Write-Host '[SmartAgent ACL] Starting the full security-only provisioning flow; Python and .venv will not be installed or modified.' -ForegroundColor Cyan
        & $configure `
            -WorkspaceContainer ([string]$options.workspace_container) `
            -AdditionalWriteRoots @($options.additional_write_roots) `
            -RuntimeRoot ([string]$options.runtime_root) `
            -DeniedWriteRoots @($options.denied_write_roots) `
            -ReadOnlyRoots @($options.read_only_roots) `
            -SkillRoot ([string]$options.skill_root) `
            -SkillSourcePath ([string]$options.skill_source_path) `
            -ExecutorUser ([string]$options.executor_user) `
            -ControllerSid $callerSid `
            -ProfilePath $ProfilePath `
            -ExecutorPython $defaultExecutorPython `
            -SkipAclModeRestore -Apply
    }
    if ($LASTEXITCODE -ne 0) { throw "ACL_ON_SECURITY_PROVISIONING_FAILED:exit=$LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) {
        throw 'ACL_ON_SECURITY_PROVISIONING_FAILED:security_profile_not_published'
    }
    $profile = Get-Content -LiteralPath $ProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $executorSid = [string]$profile.executor_sid
    $postAuthorization = Read-MachineAuthorization
    if ($postAuthorization.state -ne 'BOUND' -or
        [string]$postAuthorization.value.executor_sid -ne $executorSid) {
        throw "ACL_ON_SECURITY_PROVISIONING_FAILED:machine_authorization_state=$($postAuthorization.state)"
    }
    Write-ModeState 'on' $executorSid $callerSid
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Wait-ExecutorReady 'on' ([string]$profile.executor_endpoint)
    Write-Host 'ACL mode is ON. SmartAgent filesystem restrictions are active.' -ForegroundColor Green
    return
}

# Publish an intentionally invalid intermediate mode before any OFF preflight.
# A stale mode=off can therefore never survive a failed repair attempt.
Write-ModeState 'transitioning_off' $executorSid $callerSid

$identities = @($callerSid)
if ($identity.User.Value -ne $callerSid) { $identities += [string]$identity.User.Value }
$identities = @($identities | Select-Object -Unique)
$ownedRoots = @(Get-AclOffOwnedRoots $ProjectRoot $SecurityRoot $profile $profileStructurallyValid)
$mutationRoots = @($ownedRoots | Where-Object { Test-Path -LiteralPath $_ -PathType Container })
foreach ($path in $mutationRoots) {
    Assert-ContainedPathHasNoReparseAncestor $ProjectRoot $path
    Assert-NoReparsePoints $path
}

if ($profile -and $profileBoundToRoot) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $stopDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $stopDeadline) {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task -or $task.State -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') { throw 'restricted_executor_stop_timeout' }
}
foreach ($sid in $identities) {
    Invoke-Icacls @($ProjectRoot,'/remove:d',"*$sid")
    Invoke-Icacls @($ProjectRoot,'/grant:r',"*$sid`:(OI)(CI)F")
}
foreach ($path in $mutationRoots) {
    $items = @((Get-Item -LiteralPath $path -Force)) + @(Get-ChildItem -LiteralPath $path -Recurse -Force -ErrorAction Stop)
    foreach ($item in $items) {
        foreach ($sid in $identities) {
            Invoke-Icacls @($item.FullName,'/remove:d',"*$sid")
            if ($item.PSIsContainer) {
                Invoke-Icacls @($item.FullName,'/grant:r',"*$sid`:(OI)(CI)F")
            } else {
                Invoke-Icacls @($item.FullName,'/grant:r',"*$sid`:F")
            }
        }
    }
}
# OFF is committed only after the protected tree is writable by the controller.
# This prevents acl_mode.json from claiming software-only mode while stale ACLs
# still block atomic workspace policy updates.
Assert-AclOffPostcondition $callerSid (@($ProjectRoot) + $mutationRoots)
Write-ModeState 'off' $executorSid $callerSid
# OFF is a real software-only runtime mode.  Keep the protected task stopped;
# runtime command/path admission remains active in the controller process.
# ACLstatus ON will re-provision/re-attest and start the task again.
Write-Host 'ACL mode is OFF. Restricted executor checks are bypassed; SmartAgent command and path safety checks remain enabled.' -ForegroundColor Yellow
} finally {
    if ($transitionMutexHeld) {
        try { $transitionMutex.ReleaseMutex() } catch { }
    }
    $transitionMutex.Dispose()
}
