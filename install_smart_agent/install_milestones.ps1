[CmdletBinding()]
param(
    [ValidateSet("Check", "List", "Menu", "Install", "InstallAll")]
    [string]$Action = "Check",
    [string]$Milestone = "",
    [string]$ProjectRoot = "",
    [switch]$Json,
    [switch]$Quiet,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
} else {
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
}
$InstallerDir = Join-Path $ProjectRoot "install_smart_agent"
$SourceDir = Join-Path $ProjectRoot "source"
$env:PYTHONPATH = $SourceDir + ";" + $env:PYTHONPATH
$env:PYTHONSAFEPATH = "1"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Requirements = Join-Path $InstallerDir "requirements.txt"
$MetadataDir = Join-Path $ProjectRoot "localdata\metadata"
$LogDir = Join-Path $ProjectRoot "localdata\logs"
$OptionsPath = Join-Path $MetadataDir "provisioning_options.json"
$BootstrapStatePath = Join-Path $MetadataDir "bootstrap_state.json"
$AttemptStatePath = Join-Path $MetadataDir "milestone-state.json"
$LegacyStatePath = Join-Path $MetadataDir "install-state.json"
$ReportPath = Join-Path $LogDir "install_report.txt"
$MilestoneOrder = @("M0", "M1", "M2", "M3", "M4", "M5")
$MilestoneNames = [ordered]@{
    M0 = "WebDirect Bootstrap"
    M1 = "Full Python Dependencies"
    M2 = "Playwright Chromium"
    M3 = "Lightweight Runtime"
    M4 = "Restricted Executor Security"
    M5 = "Final Validation"
}

function Write-JsonNoBom([string]$Path, [object]$Value) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = $Path + ".tmp"
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Write-InstallLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $safe = $Message -replace '(?i)(token|password|cookie|authorization)\s*[:=]\s*\S+', '$1=[REDACTED]'
    Add-Content -LiteralPath $ReportPath -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $safe) -Encoding UTF8
    if (-not $Quiet -and -not $Json) { Write-Host $Message }
}

function Get-PublicDesktopRoot {
    $override = [string]$env:SMARTAGENT_PUBLIC_DESKTOP_ROOT
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        return [IO.Path]::GetFullPath($override)
    }
    return [Environment]::GetFolderPath('Desktop')
}

function Get-Options {
    $publicLayout = Test-Path -LiteralPath (Join-Path $InstallerDir "deploy_public_layout.ps1")
    $defaultWorkspace = if ($publicLayout) {
        Join-Path (Get-PublicDesktopRoot) "SmartAgentWorkspace\default"
    } else {
        Join-Path $env:USERPROFILE "SmartAgentWorkspaces\default"
    }
    $defaults = [ordered]@{
        configure_security = $false
        security_workspace_container = $defaultWorkspace
        security_additional_write_roots = @()
        security_runtime_root = (Join-Path $ProjectRoot "localdata\runtime\security")
        security_denied_write_roots = @($env:PUBLIC, (Join-Path $env:WINDIR "Temp"))
        security_read_only_roots = @()
        security_skill_root = (Join-Path $ProjectRoot "localdata\secure\windows_security\skills")
        security_skill_source_path = (Join-Path $env:USERPROFILE ".codex\skills")
    }
    if (Test-Path -LiteralPath $OptionsPath) {
        try {
            $saved = Get-Content -LiteralPath $OptionsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($name in @($defaults.Keys)) {
                if ($null -ne $saved.$name) { $defaults[$name] = $saved.$name }
            }
        } catch { }
    }
    if ([string]::IsNullOrWhiteSpace([string]$defaults.security_workspace_container)) {
        $defaults.security_workspace_container = $defaultWorkspace
    }
    return [pscustomobject]$defaults
}

function Test-SamePath([string]$Left, [string]$Right) {
    try {
        return [IO.Path]::GetFullPath($Left).TrimEnd('\') -ieq [IO.Path]::GetFullPath($Right).TrimEnd('\')
    } catch { return $false }
}

function Test-PathCollectionContains([object[]]$Values, [string]$Expected) {
    foreach ($value in @($Values)) {
        if (Test-SamePath ([string]$value) $Expected) { return $true }
    }
    return $false
}

function Test-PathCollectionsEqual([object[]]$Left, [object[]]$Right) {
    $leftKeys = @($Left | ForEach-Object { [IO.Path]::GetFullPath([string]$_).TrimEnd('\').ToLowerInvariant() } | Sort-Object -Unique)
    $rightKeys = @($Right | ForEach-Object { [IO.Path]::GetFullPath([string]$_).TrimEnd('\').ToLowerInvariant() } | Sort-Object -Unique)
    return $null -eq (Compare-Object -ReferenceObject $leftKeys -DifferenceObject $rightKeys | Select-Object -First 1)
}

function Test-WorkspaceTraverseAcl([string]$Path, [string]$ExecutorSid) {
    try {
        $sid = [Security.Principal.SecurityIdentifier]::new($ExecutorSid)
        $directory = [IO.DirectoryInfo]$Path
        # Avoid Get-Acl module auto-loading conflicts when a BAT launched by
        # Windows PowerShell 5.1 inherits a PowerShell 7 PSModulePath.  .NET
        # Framework exposes the instance method; modern .NET exposes the same
        # operation through FileSystemAclExtensions.
        if ($directory.PSObject.Methods.Name -contains 'GetAccessControl') {
            $acl = $directory.GetAccessControl()
        } else {
            $acl = [System.IO.FileSystemAclExtensions]::GetAccessControl($directory)
        }
        $rules = @($acl.GetAccessRules(
            $true,$true,[Security.Principal.SecurityIdentifier]
        ) | Where-Object { $_.IdentityReference.Value -eq $sid.Value })
        $inheritedRules = @($rules | Where-Object { $_.IsInherited })
        if ($rules.Count -ne 2) { return $false }
        if ($inheritedRules.Count -gt 0) { return $false }
        $mutationMask = [Security.AccessControl.FileSystemRights]::Write -bor
            [Security.AccessControl.FileSystemRights]::Delete -bor
            [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
            [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
            [Security.AccessControl.FileSystemRights]::TakeOwnership
        $expectedAllow = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
            [Security.AccessControl.FileSystemRights]::Synchronize
        $expectedDeny = $mutationMask
        $allowRules = @($rules | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow })
        $denyRules = @($rules | Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny })
        if ($allowRules.Count -ne 1 -or $denyRules.Count -ne 1) { return $false }
        if ($allowRules[0].InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None) { return $false }
        if ($allowRules[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) { return $false }
        if ($denyRules[0].InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None) { return $false }
        if ($denyRules[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) { return $false }
        if ([int64]$allowRules[0].FileSystemRights -ne [int64]$expectedAllow) { return $false }
        if ([int64]$denyRules[0].FileSystemRights -ne [int64]$expectedDeny) { return $false }
        return $true
    } catch { return $false }
}

function New-Result([string]$Id, [string]$Status, [string]$ReasonCode, [string]$Detail) {
    $repairCommand = if ($Status -in @("PASS", "SKIPPED")) {
        $null
    } elseif ($Id -eq "M0") {
        "install_smart_agent.bat"
    } else {
        "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install_smart_agent\install_milestones.ps1 -Action Install -ProjectRoot . -Milestone $Id -NonInteractive"
    }
    return [pscustomobject][ordered]@{
        id = $Id
        name = $MilestoneNames[$Id]
        status = $Status
        reason_code = $ReasonCode
        detail = $Detail
        repair_command = $repairCommand
    }
}

function Test-Python([string[]]$Arguments) {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    try {
        & $VenvPython @Arguments *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AttemptState {
    if (-not (Test-Path -LiteralPath $AttemptStatePath)) { return @{} }
    try {
        $value = Get-Content -LiteralPath $AttemptStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $result = @{}
        foreach ($property in $value.PSObject.Properties) { $result[$property.Name] = $property.Value }
        return $result
    } catch { return @{} }
}

function Save-Attempt([string]$Id, [string]$Status, [string]$ReasonCode, [string]$Detail) {
    $state = Get-AttemptState
    $state[$Id] = [ordered]@{
        status = $Status
        reason_code = $ReasonCode
        detail = $Detail
        checked_at = [DateTimeOffset]::Now.ToString("o")
    }
    Write-JsonNoBom $AttemptStatePath $state
}

function Get-MilestoneResult([string]$Id) {
    $options = Get-Options
    switch ($Id) {
        "M0" {
            if (-not [Environment]::Is64BitOperatingSystem) { return New-Result $Id "FAIL" "WINDOWS_64BIT_REQUIRED" "SmartAgent requires 64-bit Windows." }
            foreach ($path in @("source\WebAgent\controller.py", "launch_webcopilot_chatgpt.bat", "install_smart_agent\verify_webdirect_bootstrap.py")) {
                if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $path))) { return New-Result $Id "FAIL" "BOOTSTRAP_SOURCE_MISSING" "Missing $path" }
            }
            if (-not (Test-Path -LiteralPath $VenvPython)) { return New-Result $Id "PENDING" "VENV_MISSING" "The bootstrap virtual environment is missing." }
            try {
                $bootstrapState = Get-Content -LiteralPath $BootstrapStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($bootstrapState.schema -ne "SMARTAGENT_WEBDIRECT_BOOTSTRAP_V1") { throw "schema" }
                if ($bootstrapState.status -ne "PASS") { throw "status" }
                if ($bootstrapState.browser_launch_verified -ne $true) { throw "browser" }
                if (-not (Test-SamePath ([string]$bootstrapState.project_root) $ProjectRoot)) { throw "root" }
                if (-not (Test-SamePath ([string]$bootstrapState.venv_python) $VenvPython)) { throw "python" }
            } catch {
                return New-Result $Id "NEEDS_USER_ACTION" "BOOTSTRAP_ATTESTATION_MISSING" "The pre-WebGPT bootstrap did not leave a valid verification record. Re-run install_smart_agent.bat."
            }
            return New-Result $Id "PASS" "OK" "WebDirect was verified before WebGPT startup."
        }
        "M1" {
            if (-not (Test-Path -LiteralPath $VenvPython)) { return New-Result $Id "BLOCKED" "M0_REQUIRED" "Complete M0 first." }
            $env:SMARTAGENT_REQUIREMENTS_PATH = $Requirements
            $probe = "import importlib,importlib.metadata as m,os; from pathlib import Path; from pip._vendor.packaging.requirements import Requirement; reqs=[Requirement(x) for x in Path(os.environ['SMARTAGENT_REQUIREMENTS_PATH']).read_text(encoding='utf-8-sig').splitlines() if x.strip() and not x.lstrip().startswith('#')]; bad=[str(r) for r in reqs if not r.specifier.contains(m.version(r.name),prereleases=True)]; assert not bad,bad; [importlib.import_module(x) for x in ('playwright','qrcode','googleapiclient','google_auth_httplib2','google_auth_oauthlib')]"
            try {
                $ready = (Test-Python @("-c", $probe)) -and (Test-Python @("-m", "pip", "check"))
            } finally {
                Remove-Item Env:SMARTAGENT_REQUIREMENTS_PATH -ErrorAction SilentlyContinue
            }
            if (-not $ready) { return New-Result $Id "PENDING" "PYTHON_DEPENDENCIES_MISSING_OR_INCOMPATIBLE" "Required Python packages are missing, incompatible, or have broken dependencies." }
            return New-Result $Id "PASS" "OK" "Required Python package versions and dependency health are valid."
        }
        "M2" {
            $m1 = Get-MilestoneResult "M1"
            if ($m1.status -ne "PASS") { return New-Result $Id "BLOCKED" "M1_REQUIRED" "Complete M1 first." }
            $m0 = Get-MilestoneResult "M0"
            if ($m0.status -ne "PASS") { return New-Result $Id "BLOCKED" "M0_REQUIRED" "Re-run the pre-WebGPT bootstrap verification." }
            return New-Result $Id "PASS" "OK" "Playwright Chromium launch was verified before WebGPT startup."
        }
        "M3" {
            $launcher = Join-Path $ProjectRoot "launch_webcopilot_chatgpt.bat"
            $probe = "import agent_core.runtime_cleanup, WebAgent.controller"
            if (-not (Test-Path -LiteralPath $launcher -PathType Leaf) -or -not (Test-Python @("-c", $probe))) {
                return New-Result $Id "PENDING" "LIGHTWEIGHT_RUNTIME_NOT_READY" "The WebGPT launcher or lightweight runtime imports are not ready."
            }
            return New-Result $Id "PASS" "LIGHTWEIGHT_RUNTIME_READY" "SmartAgentv1 lightweight WebGPT runtime is ready."
        }
        "M4" {
            $localProfilePath = Join-Path $ProjectRoot "localdata\secure\windows_security\security_profile.json"
            $localModePath = Join-Path (Split-Path -Parent $localProfilePath) "acl_mode.json"
            $localAclMode = ""
            if (Test-Path -LiteralPath $localModePath -PathType Leaf) {
                try {
                    $localModeState = Get-Content -LiteralPath $localModePath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $localModeRoot = [IO.Path]::GetFullPath([string]$localModeState.install_root).TrimEnd('\')
                    if ($localModeState.schema -ne "SMARTAGENT_ACL_MODE_V1" -or
                        $localModeState.mode -notin @("on", "off") -or
                        -not $localModeRoot.Equals($ProjectRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
                        throw "acl_mode_state_mismatch"
                    }
                    $localAclMode = [string]$localModeState.mode
                } catch {
                    return New-Result $Id "FAIL" "ACL_MODE_STATE_INVALID" "The local ACL mode record is invalid or belongs to another installation."
                }
            }
            $localProfileExists = Test-Path -LiteralPath $localProfilePath -PathType Leaf
            if (-not [bool]$options.configure_security -and
                ($localAclMode -eq "off" -or (-not $localProfileExists -and $localAclMode -ne "on"))) {
                $skipReason = if ($localAclMode -eq "off") { "ACL_OFF_SOFTWARE_GUARD_ONLY" } else { "DISABLED_BY_OPTION" }
                return New-Result $Id "SKIPPED" $skipReason "Restricted executor security was not requested for this installation."
            }
            if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
                return New-Result $Id "BLOCKED" "M0_REQUIRED" "Complete M0 before resolving machine authorization."
            }
            try {
                $profilePath = (& $VenvPython -B -m agent_core.windows_security --resolve-profile | Select-Object -Last 1)
                if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$profilePath)) {
                    throw "machine_authorization"
                }
            } catch {
                return New-Result $Id "FAIL" "MACHINE_AUTHORIZATION_INVALID" "The machine-wide restricted-executor authorization pointer is invalid. Run reinstall_smart_agent.bat to revoke it before reinstalling."
            }
            if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
                if (-not [bool]$options.configure_security) { return New-Result $Id "SKIPPED" "DISABLED_BY_OPTION" "Restricted executor security was not requested." }
                return New-Result $Id "NEEDS_USER_ACTION" "SECURITY_PROFILE_MISSING" "Run this milestone with administrator approval."
            }
            try {
                $profile = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($profile.schema -ne "SMARTAGENT_WINDOWS_SECURITY_V2") { throw "schema" }
                # The restricted-executor task intentionally uses a protected task ACL.
                # Task Scheduler may therefore hide it completely from a non-admin
                # caller.  Validate its definition whenever it is visible; an
                # administrator must never accept a missing task.  For a non-admin,
                # the live service attestation below is the authoritative proof that
                # the protected task exists and is running under the expected identity.
                $task = Get-ScheduledTask -TaskName ([string]$profile.executor_task_name) -ErrorAction SilentlyContinue
                if ($task) {
                    if ($task.Settings.Enabled -ne $true -or [string]$task.State -eq "Disabled") { throw "task" }
                    $taskActions = @($task.Actions)
                    if ($taskActions.Count -ne 1) { throw "task_action_count" }
                    if (-not (Test-SamePath ([string]$taskActions[0].Execute) ([string]$profile.executor_python))) { throw "task_python" }
                    if (-not (Test-SamePath ([string]$taskActions[0].WorkingDirectory) ([string]$profile.executor_code_root))) { throw "task_code" }
                } elseif (Test-Administrator) {
                    throw "task_missing"
                }
                foreach ($requiredPath in @($profile.executor_python,$profile.runtime_root,$profile.executor_endpoint,$profile.controller_state_root,$profile.executor_code_root)) {
                    if (-not (Test-Path -LiteralPath ([string]$requiredPath))) { throw "profile_path" }
                }
                $configuredTraverse = @($profile.workspace_traverse_roots | ForEach-Object { [IO.Path]::GetFullPath([string]$_).TrimEnd('\') })
                if ([string]::IsNullOrWhiteSpace([string]$profile.executor_sid)) { throw "executor_sid" }
                foreach ($traverseRoot in $configuredTraverse) {
                    if (-not (Test-WorkspaceTraverseAcl $traverseRoot ([string]$profile.executor_sid))) {
                        throw "workspace_traverse_acl"
                    }
                }
                foreach ($workspaceRoot in @($profile.authorized_workspace_roots)) {
                    $volumeRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath([string]$workspaceRoot)).TrimEnd('\')
                    $parent = Split-Path -Parent ([IO.Path]::GetFullPath([string]$workspaceRoot).TrimEnd('\'))
                    while (-not [string]::IsNullOrWhiteSpace($parent)) {
                        $resolvedParent = [IO.Path]::GetFullPath($parent).TrimEnd('\')
                        if ($resolvedParent.Equals($volumeRoot,[StringComparison]::OrdinalIgnoreCase)) { break }
                        $insideWritableRoot = @($profile.authorized_workspace_roots | Where-Object {
                            $writeRoot = [IO.Path]::GetFullPath([string]$_).TrimEnd('\')
                            $resolvedParent.Equals($writeRoot,[StringComparison]::OrdinalIgnoreCase) -or
                                $resolvedParent.StartsWith($writeRoot + '\',[StringComparison]::OrdinalIgnoreCase)
                        }).Count -gt 0
                        if (-not $insideWritableRoot -and -not @($configuredTraverse | Where-Object { $_.Equals($resolvedParent,[StringComparison]::OrdinalIgnoreCase) })) {
                            throw "workspace_traverse_roots"
                        }
                        $parent = Split-Path -Parent $resolvedParent
                    }
                }
                # windows_security performs the cryptographic profile-digest match,
                # machine-authorization validation, live service identity check, and
                # service self-test validation.  This is required when the protected
                # scheduled task is invisible, and retained for visible tasks so M4
                # has one fail-closed verification path in both privilege modes.
                $attestationOutput = & $VenvPython -B -m agent_core.windows_security --workspace ([string]$profile.workspace_container) 2>&1
                if ($LASTEXITCODE -ne 0) { throw ("security_attestation: " + ($attestationOutput -join " ")) }

                # Attestation sends a live executor probe.  Read service_state only
                # after that probe so an intentionally idle service is not rejected
                # merely because its previous request heartbeat is old.
                $serviceStatePath = Join-Path ([string]$profile.executor_endpoint) "service_state.json"
                $serviceState = Get-Content -LiteralPath $serviceStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
                $heartbeatAge = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) - [double]$serviceState.heartbeat_at
                if ($serviceState.schema -ne "RESTRICTED_EXECUTOR_SERVICE_STATE_V1") { throw "service_schema" }
                if (-not ([string]$serviceState.user).Equals([string]$profile.executor_user,[StringComparison]::OrdinalIgnoreCase)) { throw "service_identity" }
                if ($serviceState.is_admin -ne $false) { throw "service_elevation" }
                if ([string]::IsNullOrWhiteSpace([string]$serviceState.profile_digest)) { throw "service_profile_digest" }
                if ($serviceState.self_test.passed -ne $true -or $heartbeatAge -lt -2 -or $heartbeatAge -gt 10) { throw "service" }
            } catch { return New-Result $Id "FAIL" "SECURITY_VERIFY_FAILED" "Security profile or restricted executor task verification failed." }
            $detail = if (Test-SamePath $profilePath $localProfilePath) {
                "Restricted executor security is configured by this SmartAgent folder."
            } else {
                "Restricted executor security is shared from this computer's existing authorization."
            }
            return New-Result $Id "PASS" "OK" $detail
        }
        "M5" {
            foreach ($required in @("M0", "M1", "M2", "M3", "M4")) {
                $prior = Get-MilestoneResult $required
                if ($prior.status -notin @("PASS", "SKIPPED")) { return New-Result $Id "BLOCKED" ("{0}_REQUIRED" -f $required) "Complete $required first." }
            }
            $state = Get-AttemptState
            if (-not $state.ContainsKey("M5") -or $state["M5"].status -ne "PASS") { return New-Result $Id "PENDING" "FINAL_VALIDATION_NOT_RUN" "Run the deterministic final validation." }
            $provisioningPath = Join-Path $MetadataDir "provisioning_state.json"
            try {
                $provisioning = Get-Content -LiteralPath $provisioningPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($provisioning.status -ne "COMPLETED") { throw "status" }
            } catch { return New-Result $Id "PENDING" "PROVISIONING_NOT_COMPLETED" "Provisioning state must be finalized again." }
            if (Test-Path -LiteralPath (Join-Path $InstallerDir "deploy_public_layout.ps1")) {
                $controlRoot = Join-Path (Get-PublicDesktopRoot) "SmartAgent"
                foreach ($launcher in @("install_smart_agent.bat", "ACLstatus.bat", "adapterUI.bat", "reinstall_smart_agent.bat", "force_stop_all_agents.bat", "Edit_workspace.bat", "InstallCheckList.bat", "launch_remote_agent.bat", "launch_webcopilot_chatgpt.bat")) {
                    $required = Join-Path $controlRoot $launcher
                    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { return New-Result $Id "PENDING" "PUBLIC_LAYOUT_MISSING" "Public desktop controls must contain all nine SmartAgentv1 launchers." }
                }
                if (-not (Test-Path -LiteralPath ([string]$options.security_workspace_container) -PathType Container)) { return New-Result $Id "PENDING" "PUBLIC_LAYOUT_MISSING" "Public workspace must be deployed again." }
            }
            return New-Result $Id "PASS" "OK" "Final SmartAgent validation passed."
        }
        default { throw "Unknown milestone: $Id" }
    }
}

function Resolve-Milestone([string]$Value) {
    $candidate = ([string]$Value).Trim().ToUpperInvariant()
    if ($candidate -match '^M?[0-5]$') {
        if (-not $candidate.StartsWith("M")) { $candidate = "M" + $candidate }
        return $candidate
    }
    throw "Invalid milestone '$Value'. Expected M0-M5 or 0-5."
}

function Get-AllResults {
    $results = @()
    foreach ($id in $MilestoneOrder) { $results += Get-MilestoneResult $id }
    return $results
}

function Get-Summary([object[]]$Results) {
    $next = @($Results | Where-Object { $_.status -notin @("PASS", "SKIPPED") } | Select-Object -First 1)
    return [pscustomobject][ordered]@{
        schema = "SMARTAGENT_INSTALL_CHECKLIST_V1"
        overall = if ($next.Count -eq 0) { "PASS" } else { "INCOMPLETE" }
        next_milestone = if ($next.Count -eq 0) { $null } else { $next[0].id }
        milestones = @($Results)
    }
}

function Show-Summary([object]$Summary) {
    Write-Host ""
    Write-Host "SmartAgent Installation Checklist"
    Write-Host ""
    foreach ($item in $Summary.milestones) {
        Write-Host ("[{0}] {1,-18} {2}" -f $item.id, $item.status, $item.name)
        if ($item.status -notin @("PASS", "SKIPPED")) { Write-Host ("     {0}: {1}" -f $item.reason_code, $item.detail) }
    }
    Write-Host ""
    Write-Host ("Overall: {0}" -f $Summary.overall)
    if ($Summary.next_milestone) { Write-Host ("Next milestone: {0}" -f $Summary.next_milestone) }
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FailureMessage (exit=$LASTEXITCODE)" }
}

function Install-Milestone([string]$Id) {
    $before = Get-MilestoneResult $Id
    if ($before.status -in @("PASS", "SKIPPED")) {
        Write-InstallLog "[$Id] $($before.status): $($before.detail)"
        return 0
    }
    foreach ($required in $MilestoneOrder) {
        if ($required -eq $Id) { break }
        if (($Id -eq "M3") -and ($required -in @("M1", "M2"))) { continue }
        if (($Id -eq "M4") -and ($required -in @("M2", "M3"))) { continue }
        $prior = Get-MilestoneResult $required
        if ($prior.status -notin @("PASS", "SKIPPED")) {
            Save-Attempt $Id "BLOCKED" ("{0}_REQUIRED" -f $required) "Complete $required first."
            Write-InstallLog "[$Id] BLOCKED: complete $required first."
            return 1
        }
    }
    $options = Get-Options
    try {
        Write-InstallLog "[$Id] Installing $($MilestoneNames[$Id])..."
        switch ($Id) {
            "M0" {
                throw "NEEDS_USER_ACTION: M0 must run before WebGPT. Re-run install_smart_agent.bat."
            }
            "M1" {
                Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip") "pip update failed"
                Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", (Join-Path $InstallerDir "requirements.txt")) "Full dependency installation failed"
            }
            "M2" { Invoke-Checked $VenvPython @("-m", "playwright", "install", "chromium") "Chromium installation failed" }
            "M3" { Write-InstallLog "[M3] Lightweight WebGPT runtime active." }
            "M4" {
                if (-not (Test-Administrator)) { throw "NEEDS_ADMINISTRATOR: rerun M4 from an elevated terminal." }
                if ($NonInteractive) { throw "NEEDS_USER_ACTION: run powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install_smart_agent\install_milestones.ps1 -Action Install -ProjectRoot . -Milestone M4 interactively and type APPLY after reviewing the ACL plan." }
                $script = Join-Path $InstallerDir "configure_security.ps1"
                $arguments = @{
                    WorkspaceContainer = [string]$options.security_workspace_container
                    AdditionalWriteRoots = @($options.security_additional_write_roots)
                    RuntimeRoot = [string]$options.security_runtime_root
                    DeniedWriteRoots = @($options.security_denied_write_roots)
                    ReadOnlyRoots = @($options.security_read_only_roots)
                    SkillRoot = [string]$options.security_skill_root
                    SkillSourcePath = [string]$options.security_skill_source_path
                    ProfilePath = (Join-Path $ProjectRoot "localdata\secure\windows_security\security_profile.json")
                    ExecutorPython = $VenvPython
                    Apply = $true
                }
                & $script @arguments
                if ($LASTEXITCODE -ne 0) { throw "Restricted executor security configuration failed (exit=$LASTEXITCODE)." }
            }
            "M5" {
                $verify = Join-Path $InstallerDir "verify_environment.py"
                $verifyArgs = @($verify, "--project-root", $ProjectRoot, "--check-browser")
                Invoke-Checked $VenvPython $verifyArgs "SmartAgent environment verification failed"
                $requiredTests = @("tests\validate_stage8.py", "tests\validate_stage9.py", "tests\validate_stage10.py", "tests\validate_cross_session_routing.py", "source\WebAgent\tests\validate_bootstrap_provisioning.py")
                if (Test-Path -LiteralPath (Join-Path $ProjectRoot "tests")) {
                    foreach ($test in $requiredTests) {
                        $testPath = Join-Path $ProjectRoot $test
                        if (-not (Test-Path -LiteralPath $testPath)) { throw "Required development self-test is missing: $test" }
                        Invoke-Checked $VenvPython @($testPath) "SmartAgent self-test failed: $test"
                    }
                } else {
                    Write-InstallLog "[M5] Release layout detected; packaged environment and finalization verifiers are authoritative."
                }
                $layoutScript = Join-Path $InstallerDir "deploy_public_layout.ps1"
                if (Test-Path -LiteralPath $layoutScript) {
                    & $layoutScript -ProjectRoot $ProjectRoot -WorkspaceRoot ([string]$options.security_workspace_container) -DesktopRoot (Get-PublicDesktopRoot)
                    if ($LASTEXITCODE -ne 0) { throw "Public desktop layout deployment failed (exit=$LASTEXITCODE)." }
                }
                $legacy = [ordered]@{
                    installer_version = 2; updated_at = [DateTimeOffset]::Now.ToString("o")
                    python_ready = $true; venv_ready = $true; dependencies_ready = $true
                    chromium_ready = $true
                    model_mode = "webgpt"
                    validation_passed = $true; security_configured = [bool]$options.configure_security
                }
                Write-JsonNoBom $LegacyStatePath $legacy
                $finalizer = Join-Path $InstallerDir "finalize_provisioning.py"
                $finalArgs = @($finalizer, "--project-root", $ProjectRoot)
                Invoke-Checked $VenvPython $finalArgs "Final provisioning state update failed"
            }
        }
        Save-Attempt $Id "PASS" "OK" "Installation action completed."
        $after = Get-MilestoneResult $Id
        if ($after.status -notin @("PASS", "SKIPPED")) { throw "Post-check failed: $($after.reason_code): $($after.detail)" }
        Write-InstallLog "[$Id] $($after.status): $($after.detail)"
        return 0
    } catch {
        $message = $_.Exception.Message
        $code = if ($message -like "NEEDS_ADMINISTRATOR:*") { "NEEDS_ADMINISTRATOR" } elseif ($message -like "NEEDS_USER_ACTION:*") { "NEEDS_USER_ACTION" } else { "INSTALL_FAILED" }
        Save-Attempt $Id "FAIL" $code $message
        Write-InstallLog "[$Id] FAIL: $message"
        if ($code -in @("NEEDS_ADMINISTRATOR", "NEEDS_USER_ACTION")) { return 3 }
        return 1
    }
}

try {
    switch ($Action) {
        "Check" {
            $results = if ([string]::IsNullOrWhiteSpace($Milestone)) { Get-AllResults } else { @(Get-MilestoneResult (Resolve-Milestone $Milestone)) }
            $summary = Get-Summary $results
            if ($Json) { $summary | ConvertTo-Json -Depth 10 } elseif (-not $Quiet) { Show-Summary $summary }
            if ($summary.overall -eq "PASS") { exit 0 }
            if (@($summary.milestones | Where-Object { $_.status -eq "NEEDS_USER_ACTION" }).Count -gt 0) { exit 3 }
            exit 1
        }
        "List" {
            $summary = Get-Summary (Get-AllResults)
            if ($Json) { $summary | ConvertTo-Json -Depth 10 } else { Show-Summary $summary }
            exit 0
        }
        "Install" {
            if ([string]::IsNullOrWhiteSpace($Milestone)) { throw "-Milestone is required for Action Install." }
            exit (Install-Milestone (Resolve-Milestone $Milestone))
        }
        "InstallAll" {
            foreach ($id in $MilestoneOrder) {
                $result = Get-MilestoneResult $id
                if ($result.status -notin @("PASS", "SKIPPED")) {
                    # Install-Milestone invokes external programs whose stdout is part of
                    # PowerShell's success stream. Capture the complete stream, replay all
                    # diagnostic output to the host, and treat only the function's final
                    # explicit integer as its exit code. Without this normalization,
                    # verbose pip/playwright output makes `$code -ne 0` truthy and can
                    # terminate InstallAll immediately after M1 while still surfacing an
                    # accidental process exit code of 0.
                    $installOutput = @(Install-Milestone $id)
                    if ($installOutput.Count -eq 0) {
                        throw "Install-Milestone $id returned no status code."
                    }
                    if ($installOutput.Count -gt 1) {
                        for ($outputIndex = 0; $outputIndex -lt ($installOutput.Count - 1); $outputIndex++) {
                            Write-Host ([string]$installOutput[$outputIndex])
                        }
                    }
                    try {
                        $code = [int]$installOutput[$installOutput.Count - 1]
                    } catch {
                        throw "Install-Milestone $id returned an invalid status code: $($installOutput[$installOutput.Count - 1])"
                    }
                    if ($code -ne 0) { exit $code }
                }
            }
            $summary = Get-Summary (Get-AllResults)
            if ($Json) { $summary | ConvertTo-Json -Depth 10 } elseif (-not $Quiet) { Show-Summary $summary }
            if ($summary.overall -eq "PASS") { exit 0 } else { exit 1 }
        }
        "Menu" {
            $summary = Get-Summary (Get-AllResults)
            Show-Summary $summary
            Write-Host ""
            $selection = Read-Host "Select milestone number (0-5), or Q to quit"
            if ($selection -match '^[qQ]$') { exit 0 }
            exit (Install-Milestone (Resolve-Milestone $selection))
        }
    }
} catch {
    if ($Json) {
        [pscustomobject]@{ schema = "SMARTAGENT_INSTALL_ERROR_V1"; error = $_.Exception.Message } | ConvertTo-Json -Depth 4
    } else {
        Write-Error $_.Exception.Message
    }
    exit 2
}
