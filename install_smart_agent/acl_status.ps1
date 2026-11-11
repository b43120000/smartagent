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
$MachineRoot = Join-Path $env:ProgramData 'SmartAgent'
$RevocationMarker = Join-Path $MachineRoot 'acl_revocation_pending.json'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'ACL mode changes require an elevated administrator process.'
}
foreach ($required in @('install_smart_agent\launchers\install_smart_agent.bat','install_smart_agent\configure_security.ps1','source','localdata')) {
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

function Write-ModeState([string]$Value, [string]$ExecutorSid, [string]$ControllerSid, [string]$RetiredUser = '', [string]$RetiredSid = '') {
    $directory = Split-Path -Parent $ModePath
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $payload = [ordered]@{
        schema = 'SMARTAGENT_ACL_MODE_V1'
        mode = $Value
        install_root = $ProjectRoot
        controller_sid = $ControllerSid
        updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    if ($RetiredUser) {
        $payload.retired_executor_user = $RetiredUser
        $payload.retired_executor_sid = $RetiredSid
    }
    $temporary = "$ModePath.tmp"
    [IO.File]::WriteAllText($temporary, ($payload | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $ModePath -Force
    $modeAcl = @($ModePath,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F','*S-1-5-32-545:R')
    if ($ControllerSid -match '^S-1-' -and $ControllerSid -ne 'S-1-5-32-545') { $modeAcl += "*$ControllerSid`:R" }
    if ($ExecutorSid -match '^S-1-') { $modeAcl += "*$ExecutorSid`:R" }
    Invoke-Icacls $modeAcl
}

function Clear-TurboPolicyOnAclOff {
    # ACL OFF revokes Turbo roots and intent. ACL ON never restores this state.
    if (-not (Test-Path -LiteralPath $AccessPolicyPath -PathType Leaf)) { return }
    $policy = Get-Content -LiteralPath $AccessPolicyPath -Raw -Encoding UTF8 |
        ConvertFrom-Json -ErrorAction Stop
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1') {
        throw 'TURBO_POLICY_INVALID_DURING_ACL_OFF'
    }
    $policy.turbo_roots = @()
    if (-not $policy.PSObject.Properties['turbo_requested']) {
        $policy | Add-Member -NotePropertyName 'turbo_requested' -NotePropertyValue $false
    } else {
        $policy.turbo_requested = $false
    }
    $policy.revision = [long]$policy.revision + 1
    $newPolicyUpdatedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($policy.PSObject.Properties['updated_at']) {
        $policy.updated_at = $newPolicyUpdatedAt
    } else {
        $policy | Add-Member -NotePropertyName 'updated_at' -NotePropertyValue $newPolicyUpdatedAt
    }
    $temp = "$AccessPolicyPath.git46-acl-off.tmp"
    try {
        [IO.File]::WriteAllText($temp, ($policy | ConvertTo-Json -Depth 16),
            (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temp -Destination $AccessPolicyPath -Force -ErrorAction Stop
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
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
        throw "ACL_ON_ENVIRONMENT_NOT_READY:missing=$executorPython; run install_smart_agent\launchers\install_smart_agent.bat"
    }
    $protocolManifest = Join-Path $ProjectRoot 'config\protocol_manifest.json'
    if (-not (Test-Path -LiteralPath $protocolManifest -PathType Leaf)) {
        throw "ACL_ON_ENVIRONMENT_NOT_READY:missing=$protocolManifest; run install_smart_agent\launchers\install_smart_agent.bat"
    }
    $source = Join-Path $ProjectRoot 'source'
    $validation = 'import sys; sys.path.insert(0, sys.argv[1]); from agent_core.protocol_manifest import validate_protocol_manifest; result = validate_protocol_manifest(sys.argv[2]); mismatches = result["hash_mismatches"]; sys.exit("protocol_manifest_hash_mismatch:" + ",".join(mismatches) if mismatches else 0)'
    # Windows PowerShell's native argument parser strips embedded double quotes.
    $validation = $validation.Replace('"', "'")
    & $executorPython -I -c $validation $source $ProjectRoot | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'ACL_ON_PROTOCOL_MANIFEST_UNVERIFIED' }
    return $executorPython
}

function Read-MachineAuthorization {
    $path = Join-Path $MachineRoot 'machine_authorization.json'
    Assert-RevocationPath $path
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

function Get-RecordedValue($Record, [string]$Name) {
    if ($Record -and $Record.PSObject.Properties[$Name]) { return $Record.$Name }
}

function Get-ExecutorTask {
    $tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $TaskName })
    if ($tasks.Count -gt 1 -or ($tasks.Count -eq 1 -and $tasks[0].TaskPath -ne '\')) {
        throw 'acl_executor_task_ambiguous'
    }
    if ($tasks.Count -eq 1) { return $tasks[0] }
}

function Get-ExecutorTaskBinding($Task) {
    $actions = @($Task.Actions)
    $python = if ($actions.Count -eq 1) { [string]$actions[0].Execute } else { '' }
    $suffix = '\.venv\Scripts\python.exe'
    $executorArguments = '-I -c "import os,sys,runpy; sys.path.insert(0,os.getcwd()); runpy.run_module(''agent_core.restricted_executor_service'',run_name=''__main__'')"'
    if (-not [IO.Path]::IsPathRooted($python) -or
        -not $python.EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase) -or
        [string]$actions[0].Arguments -cnotin @('-m agent_core.restricted_executor_service', $executorArguments) -or
        [string]$Task.Principal.LogonType -ne 'Password' -or
        [string]$Task.Principal.RunLevel -notin @('Limited','LeastPrivilege')) {
        throw 'acl_executor_task_ownership_unverified'
    }
    $root = [IO.Path]::GetFullPath($python.Substring(0, $python.Length - $suffix.Length)).TrimEnd('\')
    $directory = Join-Path $root 'localdata\secure\windows_security\executor_code'
    if ([string]$actions[0].WorkingDirectory -ine $directory) {
        throw 'acl_executor_task_ownership_unverified'
    }
    $principal = [string]$Task.Principal.UserId
    $sid = if ($principal -match '^S-1-') {
        [Security.Principal.SecurityIdentifier]::new($principal)
    } else {
        [Security.Principal.NTAccount]::new($principal).Translate([Security.Principal.SecurityIdentifier])
    }
    $account = Get-LocalUser -SID $sid -ErrorAction Stop
    return [pscustomobject]@{root=$root; sid=[string]$account.SID; user=[string]$account.Name}
}

function Assert-RevocationPath([string]$Path) {
    if ($Path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') {
        throw "acl_revocation_path_not_absolute:$Path"
    }
    $itemPath = [IO.Path]::GetFullPath($Path)
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            if ((Get-Item -LiteralPath $itemPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "acl_mode_reparse_point_forbidden:$itemPath"
            }
        }
        $itemPath = [IO.Path]::GetDirectoryName($itemPath)
    }
}

function Get-ExecutorRevocation($Machine, $LocalProfile, $Task) {
    if ($Machine.state -eq 'INVALID') { throw 'acl_revocation_machine_authorization_invalid' }
    $binding = if ($Task) { Get-ExecutorTaskBinding $Task } else { $null }
    $owner = if ($Machine.value) { $Machine.value } else { $null }
    if ($owner) { Assert-RevocationPath ([string]$owner.install_root) }
    $ownerRoot = if ($owner) { [IO.Path]::GetFullPath([string]$owner.install_root).TrimEnd('\') }
        elseif ($binding) { $binding.root } else { $ProjectRoot }
    if ($ownerRoot -ieq [IO.Path]::GetPathRoot($ownerRoot).TrimEnd('\')) { throw 'acl_revocation_owner_is_volume_root' }
    if ($ownerRoot -ine $ProjectRoot -and $LocalProfile) { throw 'acl_revocation_conflicting_local_profile' }
    $ownerSecurity = Join-Path $ownerRoot 'localdata\secure\windows_security'
    $ownerProfilePath = Join-Path $ownerSecurity 'security_profile.json'
    $ownerRuntime = Join-Path $ownerRoot 'localdata\runtime\security'
    if ($owner -and ([string]$owner.security_profile -ine $ownerProfilePath -or
            [string]$owner.executor_task_name -ne $TaskName -or
            ($binding -and ($binding.root -ine $ownerRoot -or $binding.sid -ne [string]$owner.executor_sid -or
                $binding.user -ne [string]$owner.executor_user)))) {
        throw 'acl_revocation_machine_owner_mismatch'
    }
    Assert-RevocationPath $ownerProfilePath
    $ownerProfile = if ($ownerRoot -ieq $ProjectRoot) { $LocalProfile }
        elseif (Test-Path -LiteralPath $ownerProfilePath -PathType Leaf) {
            Get-Content -LiteralPath $ownerProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
    $sid = if ($owner) { [string]$owner.executor_sid } elseif ($binding) { $binding.sid }
        else { [string](Get-RecordedValue $ownerProfile 'executor_sid') }
    $user = if ($owner) { [string]$owner.executor_user } elseif ($binding) { $binding.user }
        else { [string](Get-RecordedValue $ownerProfile 'executor_user') }
    if ($ownerProfile -and ($ownerProfile.schema -ne 'SMARTAGENT_WINDOWS_SECURITY_V2' -or
            [string]$ownerProfile.executor_sid -ne $sid -or [string]$ownerProfile.executor_user -ne $user -or
            [string]$ownerProfile.executor_code_root -ine (Join-Path $ownerSecurity 'executor_code') -or
            [string]$ownerProfile.executor_task_name -ne $TaskName)) {
        throw 'acl_revocation_profile_owner_mismatch'
    }
    if ($binding -and -not $owner -and -not $ownerProfile -and
        $binding.user -ne 'SmartAgentExecutor' -and $binding.user -ne (Get-InstanceExecutorName $ownerRoot)) {
        throw 'acl_executor_task_ownership_unverified'
    }
    $installPendingPath = Join-Path $ownerSecurity 'executor_install_pending.json'
    Assert-RevocationPath $installPendingPath
    if (Test-Path -LiteralPath $installPendingPath -PathType Leaf) {
        $installPending = Get-Content -LiteralPath $installPendingPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $pendingUser = [string]$installPending.executor_user
        $pendingSid = [string]$installPending.executor_sid
        if ($installPending.schema -ne 'SMARTAGENT_EXECUTOR_INSTALL_PENDING_V1' -or
            ($user -and $pendingUser -ne $user) -or ($sid -and $pendingSid -and $pendingSid -ne $sid) -or
            (-not $user -and $pendingUser -ne (Get-InstanceExecutorName $ownerRoot))) {
            throw 'acl_revocation_pending_account_owner_mismatch'
        }
        if (-not $sid) {
            $account = Get-LocalUser -Name $pendingUser -ErrorAction SilentlyContinue
            if ($account -and $pendingSid -and [string]$account.SID -ne $pendingSid) {
                throw 'acl_revocation_pending_account_owner_mismatch'
            }
            if ($account) { $sid = [string]$account.SID; $user = $pendingUser }
        }
    }
    if ($sid -and $sid -notmatch '^S-1-5-21-(\d+-){3}\d+$') { throw 'acl_revocation_executor_sid_invalid' }

    $journalPath = Join-Path (Split-Path -Parent $Machine.path) 'pending_acl_transaction.json'
    Assert-RevocationPath $journalPath
    $journal = if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
        Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $journalBound = $false
    if ($journal) {
        if ($journal.schema -ne 'SMARTAGENT_PENDING_ACL_TRANSACTION_V1') { throw 'acl_revocation_pending_journal_invalid' }
        if (-not $sid) {
            $journalUser = [string]$journal.executor_user
            $journalSid = [string]$journal.executor_sid
            if ($journalUser -ne 'SmartAgentExecutor' -and $journalUser -ne (Get-InstanceExecutorName $ProjectRoot)) {
                throw 'acl_revocation_pending_journal_owner_mismatch'
            }
            $account = Get-LocalUser -Name $journalUser -ErrorAction Stop
            if ([string]$account.SID -ne $journalSid) { throw 'acl_revocation_pending_journal_owner_mismatch' }
            $sid = $journalSid
            $user = $journalUser
        }
        $journalRoot = [string](Get-RecordedValue $journal 'install_root')
        if ([string]$journal.executor_sid -ne $sid -or [string]$journal.executor_user -ne $user -or
            ($journalRoot -and $journalRoot -ine $ownerRoot)) {
            throw 'acl_revocation_pending_journal_owner_mismatch'
        }
        $journalBound = $journalRoot -ieq $ownerRoot
    }
    $paths = @(
        $ownerSecurity, $ownerRuntime, $ownerProfilePath,
        (Join-Path $ownerRoot '.venv'), (Join-Path $ownerRoot '.venv\Scripts'),
        (Join-Path $ownerSecurity 'executor_code'), (Join-Path $ownerSecurity 'skills'),
        (Join-Path $ownerSecurity 'controller_state'), (Join-Path $ownerRuntime 'executor_queue')
    )
    foreach ($name in @('authorized_workspace_roots','workspace_traverse_roots','read_only_roots',
            'denied_write_roots','root_create_denied','skill_roots','runtime_root',
            'executor_endpoint','controller_state_root','executor_code_root','python_acl_roots')) {
        $paths += @(Get-RecordedValue $ownerProfile $name)
    }
    if ($journalBound) {
        $paths += @(Get-RecordedValue $journal 'workspace_traverse_roots')
        $paths += @(Get-RecordedValue $journal 'managed_acl_roots')
        foreach ($snapshot in @(Get-RecordedValue $journal 'acl_snapshots')) {
            $paths += @(Get-RecordedValue $snapshot 'path')
        }
    }
    $paths = @($paths | Where-Object { $_ } | Select-Object -Unique)
    foreach ($path in $paths) { Assert-RevocationPath $path }
    foreach ($tree in @($ownerSecurity, $ownerRuntime)) {
        Assert-ContainedPathHasNoReparseAncestor $ownerRoot $tree
        Assert-NoReparsePoints $tree
        if (Test-Path -LiteralPath $tree -PathType Container) {
            $paths += @(Get-ChildItem -LiteralPath $tree -Recurse -Force | ForEach-Object { $_.FullName })
        }
    }
    $paths = @($paths | Select-Object -Unique)
    return [pscustomobject]@{
        root=$ownerRoot; sid=$sid; user=$user; paths=$paths
        profile_path=$ownerProfilePath; journal_path=$journalPath; install_pending=$installPendingPath
        legacy_scope_incomplete=($sid -and -not (
            ($ownerProfile -and $ownerProfile.PSObject.Properties['python_acl_roots']) -or
            ($journalBound -and $journal.PSObject.Properties['managed_acl_roots'])
        ))
    }
}

function Confirm-LegacyExecutorRetirement($Revocation) {
    if (-not $Revocation.legacy_scope_incomplete) { return }
    $sid = [Security.Principal.SecurityIdentifier]::new($Revocation.sid)
    $account = Get-LocalUser -SID $sid -ErrorAction Stop
    if (-not $account.Enabled) { return }
    $alias = @($Revocation.sid, $Revocation.user, ".\$($Revocation.user)", "$env:COMPUTERNAME\$($Revocation.user)")
    $otherTasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
        $_.TaskName -ne $TaskName -and [string]$_.Principal.UserId -in $alias
    })
    Write-Warning "The legacy ACL inventory is incomplete. To revoke effective access, ACL OFF must disable the old Windows executor '$($Revocation.user)'."
    foreach ($other in $otherTasks) {
        Write-Warning "This task also uses the old account and will be unable to run while it is disabled: $($other.TaskPath)$($other.TaskName)"
    }
    $services = @(Get-CimInstance Win32_Service -ErrorAction Stop | Where-Object { $_.StartName -in $alias })
    foreach ($service in $services) {
        Write-Warning "This service also uses the old account and will be unable to start while it is disabled: $($service.Name)"
    }
    if ((Read-Host 'Type REVOKE to disable the legacy executor and continue') -cne 'REVOKE') {
        throw 'acl_revocation_legacy_retirement_declined'
    }
}

function Disable-LegacyExecutor($Revocation) {
    if (-not $Revocation.legacy_scope_incomplete) { return }
    $sid = [Security.Principal.SecurityIdentifier]::new($Revocation.sid)
    $account = Get-LocalUser -SID $sid -ErrorAction Stop
    if ($account.Enabled) { Disable-LocalUser -SID $sid -ErrorAction Stop }
    $account = Get-LocalUser -SID $sid -ErrorAction Stop
    if ($account.Enabled) { throw 'acl_revocation_legacy_executor_still_enabled' }
    $user = "$env:COMPUTERNAME\$($Revocation.user)"
    $processes = @(Get-Process -IncludeUserName -ErrorAction Stop | Where-Object { $_.UserName -ieq $user })
    if ($processes) { throw "acl_revocation_legacy_executor_has_running_processes:$($Revocation.user)" }
}

function Assert-AdminOwnedRevocationMarker {
    $acl = Get-Acl -LiteralPath $RevocationMarker
    $trusted = @('S-1-5-18', 'S-1-5-32-544')
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $mutation = [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    if ($owner -notin $trusted -or -not $acl.AreAccessRulesProtected -or
        @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object {
            $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -notin $trusted -and
                ($_.FileSystemRights -band $mutation)
        }).Count) {
        throw 'acl_revocation_marker_unprotected'
    }
}

function Read-ExecutorRevocationMarker($Task, $Machine) {
    Assert-RevocationPath $RevocationMarker
    if (-not (Test-Path -LiteralPath $RevocationMarker)) { return }
    if (-not (Test-Path -LiteralPath $RevocationMarker -PathType Leaf)) {
        throw 'acl_revocation_marker_invalid'
    }
    Assert-AdminOwnedRevocationMarker
    $record = Get-Content -LiteralPath $RevocationMarker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($record.schema -ne 'SMARTAGENT_ACL_REVOCATION_V1' -or
        [string]$record.invoking_root -ine $ProjectRoot) {
        throw 'acl_revocation_pending_for_different_installation'
    }
    $root = [string]$record.root
    $sid = [string]$record.sid
    $user = [string]$record.user
    Assert-RevocationPath $root
    if ($root -ieq [IO.Path]::GetPathRoot($root).TrimEnd('\') -or
        ($sid -and $sid -notmatch '^S-1-5-21-(\d+-){3}\d+$') -or
        ($user -and $user -notmatch '^[A-Za-z0-9._-]{1,20}$')) {
        throw 'acl_revocation_marker_invalid'
    }
    if ($Task) {
        $binding = Get-ExecutorTaskBinding $Task
        if ($binding.root -ine $root -or $binding.sid -ne $sid -or $binding.user -ne $user) {
            throw 'acl_revocation_task_changed'
        }
    }
    if ($Machine.state -eq 'INVALID' -or ($Machine.value -and
        ([string]$Machine.value.install_root -ine $root -or [string]$Machine.value.executor_sid -ne $sid))) {
        throw 'acl_revocation_machine_owner_mismatch'
    }
    $profilePath = Join-Path $root 'localdata\secure\windows_security\security_profile.json'
    $journalPath = Join-Path $MachineRoot 'pending_acl_transaction.json'
    Assert-RevocationPath $profilePath
    Assert-RevocationPath $journalPath
    $installPendingPath = Join-Path (Split-Path -Parent $profilePath) 'executor_install_pending.json'
    Assert-RevocationPath $installPendingPath
    return [pscustomobject]@{
        root=$root; sid=$sid; user=$user; profile_path=$profilePath; journal_path=$journalPath
        install_pending=$installPendingPath
    }
}

function Save-ExecutorRevocationMarker($Revocation) {
    Assert-RevocationPath $RevocationMarker
    New-Item -ItemType Directory -Force -Path $MachineRoot | Out-Null
    Invoke-Icacls @($MachineRoot,'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F',
        '*S-1-5-32-544:(OI)(CI)F','*S-1-5-32-545:(OI)(CI)RX')
    $temporary = $RevocationMarker + '.' + [guid]::NewGuid().ToString('N')
    try {
        $record = [ordered]@{
            schema='SMARTAGENT_ACL_REVOCATION_V1'
            invoking_root=$ProjectRoot
            root=$Revocation.root
            sid=$Revocation.sid
            user=$Revocation.user
        }
        [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
        Invoke-Icacls @($temporary,'/setowner','*S-1-5-32-544')
        Invoke-Icacls @($temporary,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F')
        Move-Item -LiteralPath $temporary -Destination $RevocationMarker
        Assert-AdminOwnedRevocationMarker
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Remove-RecordedExecutorAccess($Revocation) {
    if (-not $Revocation.sid) { return }
    $identity = [Security.Principal.SecurityIdentifier]::new($Revocation.sid)
    foreach ($path in $Revocation.paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $acl = Get-Acl -LiteralPath $path
        $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]) |
            Where-Object { $_.IdentityReference.Equals($identity) })
        if (-not $rules) { continue }
        Invoke-Icacls @($path, '/remove', "*$($Revocation.sid)")
    }
    foreach ($path in $Revocation.paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $remaining = @((Get-Acl -LiteralPath $path).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
            Where-Object { $_.IdentityReference.Equals($identity) })
        if ($remaining) { throw "acl_revocation_executor_ace_remaining:$path" }
    }
}

function Get-InstanceExecutorName([string]$Root) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Root.ToUpperInvariant())
        $digest = ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        return 'SAExec_' + $digest.Substring(0, 12)
    } finally { $hasher.Dispose() }
}

function Get-ProvisioningExecutorName($PreviousMode) {
    $name = Get-InstanceExecutorName $ProjectRoot
    $existing = Get-LocalUser -Name $name -ErrorAction SilentlyContinue
    if (-not $existing) { return $name }
    if (-not $existing.Enabled) { throw 'acl_executor_instance_account_disabled' }
    $sid = [string]$existing.SID
    $markerPath = Join-Path $SecurityRoot 'executor_install_pending.json'
    $pending = if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $knownPending = $pending -and $pending.schema -eq 'SMARTAGENT_EXECUTOR_INSTALL_PENDING_V1' -and
        [string]$pending.executor_user -eq $name -and ([string]$pending.executor_sid -in @('', $sid))
    $knownRetired = (Get-RecordedValue $PreviousMode 'retired_executor_user') -eq $name -and
        (Get-RecordedValue $PreviousMode 'retired_executor_sid') -eq $sid
    if (-not ($knownPending -or $knownRetired)) { throw 'acl_executor_instance_account_unverified' }
    $admins = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop)
    if ($admins | Where-Object { [string]$_.SID -eq $sid }) { throw 'acl_executor_instance_account_is_admin' }
    $aliases = @($sid, $name, ".\$name", "$env:COMPUTERNAME\$name")
    $tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
        [string]$_.Principal.UserId -in $aliases -and [string]$_.Principal.LogonType -notin @('Interactive','InteractiveToken')
    })
    $services = @(Get-CimInstance Win32_Service -ErrorAction Stop | Where-Object { $_.StartName -in $aliases })
    if ($tasks -or $services) { throw 'acl_executor_instance_account_used_elsewhere' }
    return $name
}

function Wait-ExecutorReady([string]$ExpectedMode, [string]$Endpoint, [double]$StartedAt) {
    $statePath = Join-Path $Endpoint 'service_state.json'
    $deadline = (Get-Date).AddSeconds(35)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            try {
                $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
                $selfTest = $state.self_test
                $fresh = [double]$state.heartbeat_at -ge $StartedAt -and
                    ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$state.heartbeat_at) -lt 10
                if ($fresh -and $selfTest.passed -eq $true -and $selfTest.mode -eq $ExpectedMode) { return }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "restricted_executor_mode_transition_failed:$ExpectedMode"
}

$transitionMutex = New-Object Threading.Mutex($false, 'Global\SmartAgentAclMode_Machine_V1')
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
    Assert-RevocationPath $ProjectRoot
    Assert-ContainedPathHasNoReparseAncestor $ProjectRoot $SecurityRoot
    Assert-RevocationPath $ProfilePath
    Assert-RevocationPath $ModePath

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
            $candidateModeState.mode -in @('on','off','transitioning_off','transitioning_on')) {
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
    Assert-RevocationPath $RevocationMarker
    if ((Test-Path -LiteralPath $RevocationMarker) -or
        (Get-RecordedValue $previousModeState 'mode') -eq 'transitioning_off') {
        throw 'ACL_ON_REVOCATION_PENDING:run ACLstatus.bat off'
    }
    # Re-provisioning ACL ON while an AppContainer SID still has inherited
    # grants is unsafe. Require the transactional revoker first.
    $activeTurboJournal = Join-Path $SecurityRoot 'turbo_os_activation.json'
    if (Test-Path -LiteralPath $activeTurboJournal -PathType Leaf) {
        throw 'GIT50_TURBO_BROKER_REVOKE_REQUIRED_BEFORE_ACL_ON_RECONFIGURE'
    }
    $defaultExecutorPython = Assert-OnEnvironmentReady
    $accessPolicy = Get-WorkspaceAccessPolicy
    $aclPending = Join-Path $MachineRoot 'pending_acl_transaction.json'
    Assert-RevocationPath $aclPending
    if (Test-Path -LiteralPath $aclPending -PathType Leaf) {
        $pending = Get-Content -LiteralPath $aclPending -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($pending.schema -ne 'SMARTAGENT_PENDING_ACL_TRANSACTION_V1' -or
            (Get-RecordedValue $pending 'install_root') -ine $ProjectRoot) {
            throw 'ACL_ON_PREVIOUS_AUTHORIZATION_PENDING:run ACLstatus.bat off before retrying'
        }
    }
    if ($profileFileExists -and -not $profileStructurallyValid) {
        throw 'ACL_ON_SECURITY_STATE_INVALID:security_profile_invalid; use install_smart_agent\launchers\reinstall_smart_agent.bat to remove the inconsistent security state before retrying'
    }
    if ($machineAuthorizationState.state -eq 'FOREIGN') {
        throw "ACL_ON_SECURITY_STATE_CONFLICT:machine_authorization_belongs_to_another_installation:$($machineAuthorizationState.path)"
    }
    if ($machineAuthorizationState.state -eq 'INVALID') {
        throw "ACL_ON_SECURITY_STATE_INVALID:machine_authorization_invalid:$($machineAuthorizationState.path)"
    }
    if (-not $profile -and $machineAuthorizationState.state -eq 'BOUND') {
        throw 'ACL_ON_SECURITY_STATE_INCOMPLETE:machine_authorization_exists_but_security_profile_is_missing; run ACLstatus.bat off before retrying'
    }
    if ($machineAuthorizationState.state -eq 'MISSING') {
        $unboundTask = Get-ExecutorTask
        if ($unboundTask) {
            $taskBinding = Get-ExecutorTaskBinding $unboundTask
            if (-not $profileStructurallyValid -or $taskBinding.root -ine $ProjectRoot -or
                $taskBinding.sid -ne [string]$profile.executor_sid) {
                throw 'ACL_ON_UNBOUND_EXECUTOR_TASK:run ACLstatus.bat off before re-authorizing'
            }
        }
    }
    $configure = Join-Path $PSScriptRoot 'configure_security.ps1'
    $taskBefore = Get-ExecutorTask
    if ($taskBefore -and (Get-RecordedValue $previousModeState 'mode') -eq 'transitioning_on') {
        throw 'ACL_ON_PREVIOUS_ATTEMPT_INCOMPLETE:run ACLstatus.bat off before retrying'
    }
    $options = if (-not $profileStructurallyValid) { Get-SecurityProvisioningOptions }
    $repairExecutor = $false
    if ($options) {
        $options.executor_user = Get-ProvisioningExecutorName $previousModeState
        $repairExecutor = [bool](Get-LocalUser -Name $options.executor_user -ErrorAction SilentlyContinue)
    }
    $startedAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    Write-ModeState 'transitioning_on' $executorSid $callerSid
    try {
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
                -RepairExecutorCredential:$repairExecutor -SkipAclModeRestore -NonInteractive -Apply
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
        $liveTask = Get-ExecutorTask
        if (-not $liveTask -or (Get-ExecutorTaskBinding $liveTask).root -ine $ProjectRoot) {
            throw 'ACL_ON_SECURITY_PROVISIONING_FAILED:executor_task_binding_mismatch'
        }
        Wait-ExecutorReady 'on' ([string]$profile.executor_endpoint) $startedAt
        Write-ModeState 'on' $executorSid $callerSid
    } catch {
        $failure = $_
        $failedTask = Get-ExecutorTask
        if ($failedTask -and (Get-ExecutorTaskBinding $failedTask).root -ieq $ProjectRoot) {
            Stop-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
        }
        $installPending = Join-Path $SecurityRoot 'executor_install_pending.json'
        $aclPending = Join-Path $MachineRoot 'pending_acl_transaction.json'
        if (-not $taskBefore -and -not $failedTask -and
            -not (Test-Path -LiteralPath $ProfilePath) -and
            -not (Test-Path -LiteralPath $installPending) -and
            -not (Test-Path -LiteralPath $aclPending) -and
            (Get-RecordedValue $previousModeState 'mode') -eq 'off') {
            Write-ModeState 'off' '' $callerSid (Get-RecordedValue $previousModeState 'retired_executor_user') (Get-RecordedValue $previousModeState 'retired_executor_sid')
        }
        throw $failure
    }
    Write-Host 'ACL mode is ON. SmartAgent filesystem restrictions are active.' -ForegroundColor Green
    return
}

$task = Get-ExecutorTask
$revocation = Read-ExecutorRevocationMarker $task $machineAuthorizationState
$resumingRevocation = $null -ne $revocation
if (-not $resumingRevocation) {
    $revocation = Get-ExecutorRevocation $machineAuthorizationState $profile $task
    Confirm-LegacyExecutorRetirement $revocation
}
if ($revocation.sid -and $revocation.sid -in @($callerSid, [string]$identity.User.Value)) {
    throw 'acl_revocation_executor_is_controller'
}
$retiredUser = ''
$retiredSid = ''
if ($revocation.root -ieq $ProjectRoot -and $revocation.user -eq (Get-InstanceExecutorName $ProjectRoot)) {
    $retiredUser = $revocation.user
    $retiredSid = $revocation.sid
} elseif ((Get-RecordedValue $previousModeState 'retired_executor_user') -eq (Get-InstanceExecutorName $ProjectRoot)) {
    $retiredUser = Get-RecordedValue $previousModeState 'retired_executor_user'
    $retiredSid = Get-RecordedValue $previousModeState 'retired_executor_sid'
}
Write-ModeState 'transitioning_off' '' $callerSid $retiredUser $retiredSid

if ($task) {
    if ($task.State -ne 'Disabled') {
        Disable-ScheduledTask -InputObject $task -ErrorAction Stop | Out-Null
    }
    if ($task.State -notin @('Ready','Disabled')) {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
    }
    $stopDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $stopDeadline) {
        $task = Get-ExecutorTask
        if (-not $task -or $task.State -in @('Ready','Disabled')) { break }
        Start-Sleep -Milliseconds 250
    }
    $task = Get-ExecutorTask
    if ($task -and $task.State -notin @('Ready','Disabled')) { throw 'restricted_executor_stop_timeout' }
}

# Revoke both the invoking installation's Turbo boundary and the verified
# machine-wide executor owner's boundary before publishing OFF.
foreach ($install in @($ProjectRoot, $revocation.root) | Select-Object -Unique) {
    $turboOsJournal = Join-Path $install 'localdata\secure\windows_security\turbo_os_activation.json'
    Assert-RevocationPath $turboOsJournal
    if (Test-Path -LiteralPath $turboOsJournal -PathType Leaf) {
        $turboRevoker = Join-Path $PSScriptRoot 'turbo_os_activation.ps1'
        & $turboRevoker -Action Revoke -ProjectRoot $install
        if (Test-Path -LiteralPath $turboOsJournal -PathType Leaf) {
            throw 'GIT50_TURBO_OS_REVOCATION_NOT_CONFIRMED_ACL_OFF_BLOCKED'
        }
    }
}

$identities = @($callerSid)
if ($identity.User.Value -ne $callerSid) { $identities += [string]$identity.User.Value }
$identities = @($identities | Select-Object -Unique)
$ownedRoots = @(Get-AclOffOwnedRoots $ProjectRoot $SecurityRoot $profile $profileStructurallyValid)
$mutationRoots = @($ownedRoots | Where-Object { Test-Path -LiteralPath $_ -PathType Container })
foreach ($path in $mutationRoots) {
    Assert-ContainedPathHasNoReparseAncestor $ProjectRoot $path
    Assert-NoReparsePoints $path
}

if (-not $resumingRevocation) {
    Disable-LegacyExecutor $revocation
    Remove-RecordedExecutorAccess $revocation
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
Clear-TurboPolicyOnAclOff
if (-not $resumingRevocation -and ($task -or $revocation.sid)) {
    Save-ExecutorRevocationMarker $revocation
}
# Keep ownership evidence until ACL removal succeeds. Each removal is safe
# to retry, and the existing task identifies the old root until it is removed.
if (Test-Path -LiteralPath $revocation.profile_path -PathType Leaf) {
    Remove-Item -LiteralPath $revocation.profile_path -Force -ErrorAction Stop
}
if (Test-Path -LiteralPath $revocation.install_pending -PathType Leaf) {
    Remove-Item -LiteralPath $revocation.install_pending -Force -ErrorAction Stop
}
if (Test-Path -LiteralPath $revocation.journal_path -PathType Leaf) {
    Remove-Item -LiteralPath $revocation.journal_path -Force -ErrorAction Stop
}
if (Test-Path -LiteralPath $machineAuthorizationState.path -PathType Leaf) {
    Remove-Item -LiteralPath $machineAuthorizationState.path -Force -ErrorAction Stop
}
$task = Get-ExecutorTask
if ($task) {
    $binding = Get-ExecutorTaskBinding $task
    if ($binding.root -ine $revocation.root -or $binding.sid -ne $revocation.sid -or
        $binding.user -ne $revocation.user -or $task.State -ne 'Disabled') {
        throw 'acl_revocation_task_changed'
    }
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
}
if (Get-ExecutorTask) { throw 'acl_revocation_executor_task_remaining' }

Write-ModeState 'off' '' $callerSid $retiredUser $retiredSid
if (Test-Path -LiteralPath $RevocationMarker -PathType Leaf) {
    Remove-Item -LiteralPath $RevocationMarker -Force -ErrorAction Stop
}
Write-Host 'ACL mode is OFF. SmartAgent Windows executor authorization was revoked. Command and path safety checks remain enabled.' -ForegroundColor Yellow
} finally {
    if ($transitionMutexHeld) {
        try { $transitionMutex.ReleaseMutex() } catch { }
    }
    $transitionMutex.Dispose()
}
