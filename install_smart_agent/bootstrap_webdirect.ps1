[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [switch]$ValidateOnly,
    [switch]$NonInteractive,
    [switch]$ConfigureSecurity,
    [bool]$LaunchProvisioning = $false,
    [string]$SecurityWorkspaceContainer = "",
    [string[]]$SecurityAdditionalWriteRoots = @(),
    [string]$SecurityRuntimeRoot = "",
    [string[]]$SecurityDeniedWriteRoots = @("$env:PUBLIC", "$env:WINDIR\Temp"),
    [string[]]$SecurityReadOnlyRoots = @(),
    [string]$SecuritySkillRoot = "",
    [string]$SecuritySkillSourcePath = "$env:USERPROFILE\.codex\skills"
)
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
} else {
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
}
$InstallerDir = Join-Path $ProjectRoot "install_smart_agent"
$SourceDir = Join-Path $ProjectRoot "source"
if ([string]::IsNullOrWhiteSpace($SecurityRuntimeRoot)) {
    $SecurityRuntimeRoot = Join-Path $ProjectRoot "localdata\runtime\security"
}
if ([string]::IsNullOrWhiteSpace($SecuritySkillRoot)) {
    $SecuritySkillRoot = Join-Path $ProjectRoot "localdata\secure\windows_security\skills"
}
$env:PYTHONPATH = $SourceDir + ";" + $env:PYTHONPATH
$env:PYTHONSAFEPATH = "1"
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $InstallerDir "requirements-webdirect.txt"
$Verifier = Join-Path $InstallerDir "verify_webdirect_bootstrap.py"
$BootstrapStatePath = Join-Path $ProjectRoot "localdata\metadata\bootstrap_state.json"
$InstancePreparer = Join-Path $InstallerDir "prepare_install_instance.ps1"

if ($ConfigureSecurity) {
    throw "INSTALL_SECURITY_MODE_UNSUPPORTED: normal installation always completes in ACL OFF mode; run ACLstatus.bat on after installation"
}
if (-not (Test-Path -LiteralPath $InstancePreparer -PathType Leaf)) {
    throw "SmartAgent install-instance preparer is missing: $InstancePreparer"
}
& $InstancePreparer -ProjectRoot $ProjectRoot -ValidateOnly:$ValidateOnly

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FailureMessage (exit=$LASTEXITCODE)" }
}
function Find-CompatiblePython {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidates += ,@($py.Source, "-3.12")
        $candidates += ,@($py.Source, "-3.11")
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { $candidates += ,@($python.Source) }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $prefix = @()
        if ($candidate.Count -gt 1) { $prefix = $candidate[1..($candidate.Count - 1)] }
        try {
            $probe = & $exe @prefix -c "import json,struct,sys; print(json.dumps({'version':sys.version_info[:3],'bits':struct.calcsize('P')*8}))" 2>$null
            $info = $probe | ConvertFrom-Json
            if ($info.bits -eq 64 -and $info.version[0] -eq 3 -and $info.version[1] -ge 11) {
                return @{ Exe = $exe; Prefix = $prefix; Version = ($info.version -join ".") }
            }
        } catch { }
    }
    return $null
}
function Write-JsonNoBom {
    param([string]$Path, [object]$Value)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
}
function Ensure-DefaultAclOffMode {
    $securityRoot = Join-Path $ProjectRoot "localdata\secure\windows_security"
    $profilePath = Join-Path $securityRoot "security_profile.json"
    $modePath = Join-Path $securityRoot "acl_mode.json"
    if ((Test-Path -LiteralPath $profilePath -PathType Leaf) -and
        -not (Test-Path -LiteralPath $modePath -PathType Leaf)) {
        throw "INSTALL_SECURITY_STATE_INCOMPLETE: security profile exists without an ACL mode record; run ACLstatus.bat off or reinstall_smart_agent.bat"
    }
    if (Test-Path -LiteralPath $modePath -PathType Leaf) {
        try {
            $modeState = Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $modeRoot = [IO.Path]::GetFullPath([string]$modeState.install_root).TrimEnd('\')
        } catch {
            throw "Existing ACL mode state is unreadable: $modePath"
        }
        if ($modeState.schema -ne "SMARTAGENT_ACL_MODE_V1" -or
            $modeState.mode -notin @("on", "off") -or
            -not $modeRoot.Equals($ProjectRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw "Existing ACL mode state is invalid or belongs to another install: $modePath"
        }
        if ($modeState.mode -eq "on") {
            throw "INSTALL_REQUIRES_ACL_OFF: run ACLstatus.bat off before reinstalling this existing instance"
        }
    }
    Write-JsonNoBom $modePath ([ordered]@{
        schema = "SMARTAGENT_ACL_MODE_V1"
        mode = "off"
        install_root = $ProjectRoot
        controller_sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    })
}

function Ensure-DefaultWorkspaceAccessPolicy {
    $securityRoot = Join-Path $ProjectRoot "localdata\secure\windows_security"
    $policyPath = Join-Path $securityRoot "workspace_access_policy.json"
    $defaultWorkspace = if (-not [string]::IsNullOrWhiteSpace($SecurityWorkspaceContainer)) {
        [IO.Path]::GetFullPath($SecurityWorkspaceContainer)
    } else {
        Join-Path ([Environment]::GetFolderPath('Desktop')) "SmartAgentWorkspace\default"
    }
    New-Item -ItemType Directory -Force -Path $defaultWorkspace | Out-Null
    if (Test-Path -LiteralPath $policyPath -PathType Leaf) {
        try { $existing = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw "Existing workspace access policy is unreadable: $policyPath" }
        if ($existing.schema -ne "SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1" -or
            @($existing.writable_workspaces).Count -lt 1) {
            throw "Existing workspace access policy is invalid: $policyPath"
        }
        return [IO.Path]::GetFullPath([string]$existing.writable_workspaces[0])
    }
    Write-JsonNoBom $policyPath ([ordered]@{
        schema = "SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1"
        revision = 1
        updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        writable_workspaces = @($defaultWorkspace)
        read_only_roots = @($SecurityReadOnlyRoots)
        denied_write_roots = @($SecurityDeniedWriteRoots)
    })
    return $defaultWorkspace
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw "SmartAgent requires 64-bit Windows." }
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir "WebAgent\controller.py"))) { throw "WebAgent source is missing." }
    $pythonInfo = Find-CompatiblePython
    if (-not $pythonInfo) {
        if ($ValidateOnly) { throw "Python 3.11+ 64-bit is missing." }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw "winget is required to install Python automatically." }
        Invoke-Checked $winget.Source @("install","-e","--id","Python.Python.3.12","--scope","user","--accept-package-agreements","--accept-source-agreements") "Python installation failed"
        Refresh-Path
        $pythonInfo = Find-CompatiblePython
    }
    if (-not $pythonInfo) { throw "Python 3.11+ could not be located after installation." }
    Write-Host "[SmartAgent Bootstrap] Python $($pythonInfo.Version)" -ForegroundColor Green

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if ($ValidateOnly) { throw "The SmartAgent .venv is missing." }
        & $pythonInfo.Exe @($pythonInfo.Prefix) -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv." }
    }

    $dependencyReady = $false
    try {
        & $VenvPython -c "import importlib.metadata as m; assert m.version('playwright')=='1.62.0'" *> $null
        $dependencyReady = $LASTEXITCODE -eq 0
    } catch { }
    if (-not $dependencyReady) {
        if ($ValidateOnly) { throw "Minimal WebDirect Python dependencies are missing." }
        Invoke-Checked $VenvPython @("-m","pip","install","--upgrade","pip") "pip update failed"
        Invoke-Checked $VenvPython @("-m","pip","install","-r",$Requirements) "Minimal WebDirect dependency installation failed"
    }

    if (-not $ValidateOnly) {
        # This command is idempotent. It downloads Chromium only when the
        # matching Playwright browser revision is not already present.
        Invoke-Checked $VenvPython @("-m","playwright","install","chromium") "Playwright Chromium installation failed"
    }
    Invoke-Checked $VenvPython @($Verifier,"--project-root",$ProjectRoot) "WebDirect bootstrap verification failed"

    if (-not $ValidateOnly) {
        Write-JsonNoBom $BootstrapStatePath ([ordered]@{
            schema = "SMARTAGENT_WEBDIRECT_BOOTSTRAP_V1"
            status = "PASS"
            verified_at = [DateTimeOffset]::Now.ToString("o")
            project_root = $ProjectRoot
            venv_python = $VenvPython
            browser_launch_verified = $true
        })
    }

    if (-not $ValidateOnly) {
        $metadata = Join-Path $ProjectRoot "localdata\metadata"
        $optionsPath = Join-Path $metadata "provisioning_options.json"
        $statePath = Join-Path $metadata "provisioning_state.json"
        Ensure-DefaultAclOffMode
        $effectiveWorkspace = Ensure-DefaultWorkspaceAccessPolicy
        $options = [ordered]@{
            schema = "SMARTAGENT_PROVISIONING_OPTIONS_V1"
            non_interactive = $true
            configure_security = $false
            security_workspace_container = $effectiveWorkspace
            security_additional_write_roots = @($SecurityAdditionalWriteRoots)
            security_runtime_root = $SecurityRuntimeRoot
            security_denied_write_roots = @($SecurityDeniedWriteRoots)
            security_read_only_roots = @($SecurityReadOnlyRoots)
            security_skill_root = $SecuritySkillRoot
            security_skill_source_path = $SecuritySkillSourcePath
        }
        Write-JsonNoBom $optionsPath $options
        Write-JsonNoBom $statePath ([ordered]@{
            schema = "SMARTAGENT_PROVISIONING_STATE_V1"
            status = "BOOTSTRAP_READY"
            manifest = (Join-Path $InstallerDir "POST_BOOTSTRAP_SETUP.md")
            next_action = "Run the deterministic local M1-M5 installer."
        })
    }
    if (-not $ValidateOnly -and $LaunchProvisioning) {
        $launcher = Join-Path $ProjectRoot "launch_webcopilot_chatgpt.bat"
        Write-Host "[SmartAgent Bootstrap] Launching WebCopilot provisioning..." -ForegroundColor Cyan
        Start-Process -FilePath $launcher -ArgumentList @("--bootstrap-provision") -WorkingDirectory $ProjectRoot
    }
    Write-Host "WEBDIRECT_BOOTSTRAP_PASS" -ForegroundColor Green
    exit 0
} catch {
    Write-Error ("[SmartAgent Bootstrap] " + $_.Exception.Message)
    exit 1
}
