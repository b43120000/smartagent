[CmdletBinding()]
param([string]$ProjectRoot = "")
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
} else {
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
}
$engine = Join-Path $ProjectRoot "install_smart_agent\install_milestones.ps1"
if (-not (Test-Path -LiteralPath $engine)) { throw "Missing milestone installer: $engine" }
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $engine -Action InstallAll -ProjectRoot $ProjectRoot -NonInteractive
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$securityRoot = Join-Path $ProjectRoot 'localdata\secure\windows_security'
$modePath = Join-Path $securityRoot 'acl_mode.json'
$policyPath = Join-Path $securityRoot 'workspace_access_policy.json'
$statePath = Join-Path $ProjectRoot 'localdata\metadata\provisioning_state.json'
try {
    $mode = Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $modeRoot = [IO.Path]::GetFullPath([string]$mode.install_root).TrimEnd('\')
    if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $mode.mode -ne 'off' -or
        -not $modeRoot.Equals($ProjectRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'acl_mode_postcondition'
    }
    $policy = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
        @($policy.writable_workspaces).Count -lt 1) {
        throw 'workspace_access_policy_postcondition'
    }
    foreach ($workspace in @($policy.writable_workspaces)) {
        if (-not (Test-Path -LiteralPath ([string]$workspace) -PathType Container)) {
            throw "workspace_access_path_missing:$workspace"
        }
    }
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.schema -ne 'SMARTAGENT_PROVISIONING_STATE_V1' -or $state.status -ne 'COMPLETED') {
        throw 'provisioning_state_postcondition'
    }
    $editWorkspace = Join-Path $ProjectRoot 'Edit_workspace.bat'
    & $editWorkspace --launcher-self-test
    if ($LASTEXITCODE -ne 0) { throw "edit_workspace_self_test_failed:$LASTEXITCODE" }
} catch {
    Write-Error ("SMARTAGENT_INSTALL_POSTCONDITION_FAILED: " + $_.Exception.Message)
    exit 1
}
Write-Host "SMARTAGENT_FULL_PROVISIONING_PASS" -ForegroundColor Green
Write-Host "ACL mode: OFF" -ForegroundColor Green
Write-Host "Next: run Edit_workspace.bat to select the normal workspace and ChatGPT conversation." -ForegroundColor Cyan
exit 0
