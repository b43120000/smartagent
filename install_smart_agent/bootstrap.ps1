[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$PackageAcceptance,
    [switch]$UpdateAcceptance,
    [switch]$NonInteractive,
    [switch]$ConfigureSecurity,
    [string]$SecurityWorkspaceContainer = "",
    [string[]]$SecurityAdditionalWriteRoots = @(),
    [string]$SecurityRuntimeRoot = "",
    [string[]]$SecurityDeniedWriteRoots = @("$env:PUBLIC", "$env:WINDIR\Temp"),
    [string[]]$SecurityReadOnlyRoots = @(),
    [string]$SecuritySkillRoot = "",
    [string]$SecuritySkillSourcePath = "$env:USERPROFILE\.codex\skills"
)
$ErrorActionPreference = "Stop"
$SourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$InstallRoot = $SourceRoot
if ([string]::IsNullOrWhiteSpace($SecurityRuntimeRoot)) {
    $SecurityRuntimeRoot = Join-Path $InstallRoot "localdata\runtime\security"
}
if ([string]::IsNullOrWhiteSpace($SecuritySkillRoot)) {
    $SecuritySkillRoot = Join-Path $InstallRoot "localdata\secure\windows_security\skills"
}
$ExistingSecurityProfile = Join-Path $InstallRoot "localdata\secure\windows_security\security_profile.json"
if ($ConfigureSecurity) {
    throw "INSTALL_SECURITY_MODE_UNSUPPORTED: install_smart_agent.bat always completes in ACL OFF mode; run ACLstatus.bat on after installation"
}
if (Test-Path -LiteralPath $ExistingSecurityProfile -PathType Leaf) {
    Write-Host "[SmartAgent Bootstrap] Existing security profile detected. The installer will proceed only when its local ACL mode is already OFF." -ForegroundColor Yellow
}

if ($UpdateAcceptance) {
    $expectedRootFiles = @(
        'install_smart_agent.bat',
        'ACLstatus.bat',
        'update.bat',
        'adapterUI.bat',
        'reinstall_smart_agent.bat',
        'force_stop_all_agents.bat',
        'Edit_workspace.bat',
        'InstallCheckList.bat',
        'launch_remote_agent.bat',
        'launch_webcopilot_chatgpt.bat'
    )
    $actualRootFiles = @(Get-ChildItem -LiteralPath $InstallRoot -File -Filter '*.bat' | ForEach-Object { $_.Name } | Sort-Object)
    if ((Compare-Object ($expectedRootFiles | Sort-Object) $actualRootFiles).Count -ne 0) {
        throw "Update acceptance root BAT contract mismatch."
    }
    foreach ($relative in @('source','install_smart_agent','config','defaultworkspace','localdata')) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $relative) -PathType Container)) {
            throw "Update acceptance required directory is missing: $relative"
        }
    }
    foreach ($relative in @('.git','.agents','tests','testscript','tools')) {
        if (Test-Path -LiteralPath (Join-Path $InstallRoot $relative)) {
            throw "Update acceptance forbidden release artifact exists: $relative"
        }
    }
    Write-Host "SMARTAGENTV1_UPDATE_DEPLOY_ACCEPTANCE_PASS" -ForegroundColor Green
    exit 0
}
if ($PackageAcceptance) {
    $expectedRootFiles = @(
        'install_smart_agent.bat',
        'ACLstatus.bat',
        'update.bat',
        'adapterUI.bat',
        'reinstall_smart_agent.bat',
        'force_stop_all_agents.bat',
        'Edit_workspace.bat',
        'InstallCheckList.bat',
        'launch_remote_agent.bat',
        'launch_webcopilot_chatgpt.bat'
    )
    $actualRootFiles = @(Get-ChildItem -LiteralPath $InstallRoot -File -Filter '*.bat' | ForEach-Object { $_.Name } | Sort-Object)
    if ((Compare-Object ($expectedRootFiles | Sort-Object) $actualRootFiles).Count -ne 0) {
        throw "Package acceptance root BAT contract mismatch."
    }
    foreach ($relative in @('source','install_smart_agent','config','defaultworkspace','localdata')) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $relative) -PathType Container)) {
            throw "Package acceptance required directory is missing: $relative"
        }
    }
    foreach ($relative in @('.git','.venv','.agents','tests','testscript','tools')) {
        if (Test-Path -LiteralPath (Join-Path $InstallRoot $relative)) {
            throw "Package acceptance forbidden release artifact exists: $relative"
        }
    }
    $activeLocaldata = @(Get-ChildItem -LiteralPath (Join-Path $InstallRoot 'localdata') -Recurse -File -Force -ErrorAction SilentlyContinue)
    if ($activeLocaldata.Count -ne 0) {
        throw "Package acceptance localdata is not empty."
    }
    Write-Host "SMARTAGENTV1_PACKAGE_DEPLOY_ACCEPTANCE_PASS" -ForegroundColor Green
    exit 0
}

$bootstrap = Join-Path $InstallRoot "install_smart_agent\bootstrap_webdirect.ps1"
if (-not (Test-Path -LiteralPath $bootstrap)) { throw "SmartAgent WebDirect bootstrap is missing: $bootstrap" }
$forward = @{
    ProjectRoot = $InstallRoot
    ValidateOnly = $ValidateOnly
    NonInteractive = $NonInteractive
    ConfigureSecurity = $false
    SecurityAdditionalWriteRoots = $SecurityAdditionalWriteRoots
    SecurityRuntimeRoot = $SecurityRuntimeRoot
    SecurityDeniedWriteRoots = $SecurityDeniedWriteRoots
    SecurityReadOnlyRoots = $SecurityReadOnlyRoots
    SecuritySkillRoot = $SecuritySkillRoot
    SecuritySkillSourcePath = $SecuritySkillSourcePath
    LaunchProvisioning = $false
}
if (-not [string]::IsNullOrWhiteSpace($SecurityWorkspaceContainer)) {
    $forward.SecurityWorkspaceContainer = $SecurityWorkspaceContainer
}
& $bootstrap @forward
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($ValidateOnly) { exit 0 }

$completion = Join-Path $InstallRoot "install_smart_agent\complete_install.ps1"
if (-not (Test-Path -LiteralPath $completion -PathType Leaf)) {
    throw "SmartAgent deterministic installer is missing: $completion"
}
Write-Host "[SmartAgent] Phase 0 ready. Running deterministic local M1-M5 installation..." -ForegroundColor Cyan
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $completion -ProjectRoot $InstallRoot
$code = $LASTEXITCODE
if ($code -ne 0) { exit $code }

$statePath = Join-Path $InstallRoot "localdata\metadata\provisioning_state.json"
if (-not (Test-Path -LiteralPath $statePath)) { throw "Provisioning state was not produced." }
$state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$state.status -ne "COMPLETED") { throw "Provisioning did not reach COMPLETED state." }
Write-Host "[SmartAgent] Installation complete. Run Edit_workspace.bat." -ForegroundColor Green
exit 0

