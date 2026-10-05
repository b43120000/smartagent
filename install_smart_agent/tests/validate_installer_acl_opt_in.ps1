[CmdletBinding()]
param([string]$ProjectRoot = '')

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
} else {
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
}

function Assert-Contains([string]$Text, [string]$Expected, [string]$Failure) {
    if (-not $Text.Contains($Expected)) { throw $Failure }
}

$bootstrapPath = Join-Path $ProjectRoot 'install_smart_agent\bootstrap.ps1'
$webdirectBootstrapPath = Join-Path $ProjectRoot 'install_smart_agent\bootstrap_webdirect.ps1'
$installerPath = Join-Path $ProjectRoot 'install_smart_agent\install.ps1'
$milestonesPath = Join-Path $ProjectRoot 'install_smart_agent\install_milestones.ps1'
$bootstrap = Get-Content -LiteralPath $bootstrapPath -Raw -Encoding UTF8
$webdirectBootstrap = Get-Content -LiteralPath $webdirectBootstrapPath -Raw -Encoding UTF8
$installer = Get-Content -LiteralPath $installerPath -Raw -Encoding UTF8
$milestones = Get-Content -LiteralPath $milestonesPath -Raw -Encoding UTF8

foreach ($path in @($bootstrapPath, $webdirectBootstrapPath, $milestonesPath)) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    if (@($errors).Count -gt 0) {
        throw "installer_acl_opt_in_powershell_parse_failed:${path}:$($errors[0].Message)"
    }
}

if ($bootstrap.Contains('$ConfigureSecurity = $true')) {
    throw 'installer_must_not_enable_security_from_profile_presence'
}
Assert-Contains $bootstrap 'install_smart_agent.bat always completes in ACL OFF mode' 'installer_acl_off_contract_missing'
Assert-Contains $bootstrap 'complete_install.ps1' 'installer_local_completion_missing'
if ($bootstrap -match '&\s+\$launcher\s+--bootstrap-provision') {
    throw 'installer_must_not_require_webgpt_for_normal_provisioning'
}
Assert-Contains $webdirectBootstrap 'Ensure-DefaultAclOffMode' 'installer_default_acl_off_state_writer_missing'
Assert-Contains $webdirectBootstrap 'Ensure-DefaultWorkspaceAccessPolicy' 'installer_default_workspace_policy_missing'
Assert-Contains $webdirectBootstrap 'prepare_install_instance.ps1' 'installer_relocation_preparer_missing'
Assert-Contains $webdirectBootstrap 'mode = "off"' 'installer_default_acl_off_mode_missing'

$webdirectTokens = $null
$webdirectErrors = $null
$webdirectAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $webdirectBootstrapPath, [ref]$webdirectTokens, [ref]$webdirectErrors
)
$modeWriter = $webdirectAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Ensure-DefaultAclOffMode'
}, $true)
if (-not $modeWriter) { throw 'installer_default_acl_off_mode_writer_unavailable' }
function Write-JsonNoBom([string]$Path, [object]$Value) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
}
Invoke-Expression $modeWriter.Extent.Text
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("smartagent-acl-mode-test-" + [Guid]::NewGuid().ToString('N'))
try {
    $ProjectRoot = $tempRoot
    $ConfigureSecurity = $false
    Ensure-DefaultAclOffMode
    $modePath = Join-Path $tempRoot 'localdata\secure\windows_security\acl_mode.json'
    $mode = Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($mode.mode -ne 'off' -or [IO.Path]::GetFullPath([string]$mode.install_root) -ne [IO.Path]::GetFullPath($tempRoot)) {
        throw 'installer_default_acl_off_state_invalid'
    }

    $mode.mode = 'on'
    Write-JsonNoBom $modePath $mode
    $rejectedOn = $false
    try { Ensure-DefaultAclOffMode } catch { $rejectedOn = $_.Exception.Message -like 'INSTALL_REQUIRES_ACL_OFF:*' }
    if (-not $rejectedOn) { throw 'installer_must_reject_existing_acl_on_mode' }

    Remove-Item -LiteralPath $modePath -Force
    New-Item -ItemType File -Force -Path (Join-Path (Split-Path -Parent $modePath) 'security_profile.json') | Out-Null
    $rejectedIncomplete = $false
    try { Ensure-DefaultAclOffMode } catch { $rejectedIncomplete = $_.Exception.Message -like 'INSTALL_SECURITY_STATE_INCOMPLETE:*' }
    if (-not $rejectedIncomplete) { throw 'installer_must_reject_profile_without_mode' }

    Remove-Item -LiteralPath (Join-Path (Split-Path -Parent $modePath) 'security_profile.json') -Force
    $ConfigureSecurity = $true
    Ensure-DefaultAclOffMode
    $mode = Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($mode.mode -ne 'off') { throw 'installer_must_always_publish_acl_off' }
} finally {
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}

$securityGate = $installer.IndexOf('if ($ConfigureSecurity)')
$securityCall = $installer.IndexOf('& $securityScript')
if ($securityGate -lt 0 -or $securityCall -le $securityGate) {
    throw 'installer_security_configuration_not_explicitly_gated'
}

$m4Start = $milestones.IndexOf('"M4" {')
$m5Start = $milestones.IndexOf('"M5" {', $m4Start)
if ($m4Start -lt 0 -or $m5Start -lt 0) { throw 'installer_m4_milestone_missing' }
$m4 = $milestones.Substring($m4Start, $m5Start - $m4Start)
$softwareOnlySkip = $m4.IndexOf('ACL_OFF_SOFTWARE_GUARD_ONLY')
$machineAuthorizationLookup = $m4.IndexOf('--resolve-profile')
if ($softwareOnlySkip -lt 0 -or $machineAuthorizationLookup -lt 0 -or
    $softwareOnlySkip -gt $machineAuthorizationLookup) {
    throw 'installer_m4_must_skip_software_only_before_machine_authorization_lookup'
}
Assert-Contains $m4 'ACL_MODE_STATE_INVALID' 'installer_m4_must_fail_closed_on_invalid_local_mode'
Assert-Contains $m4 'if (-not [bool]$options.configure_security' 'installer_m4_must_respect_security_option'

Write-Host 'INSTALLER_ACL_OPT_IN_VALIDATION_PASS'
