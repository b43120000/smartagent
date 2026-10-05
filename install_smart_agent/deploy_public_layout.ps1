[CmdletBinding()]

param(

    [Parameter(Mandatory=$true)][string]$ProjectRoot,

    [string]$WorkspaceRoot = "",

    [string]$DesktopRoot = ""

)

$ErrorActionPreference = "Stop"

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)

$Desktop = if ([string]::IsNullOrWhiteSpace($DesktopRoot)) {

    [Environment]::GetFolderPath('Desktop')

} else {

    [IO.Path]::GetFullPath($DesktopRoot)

}

if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {

    $WorkspaceRoot = Join-Path $Desktop "SmartAgentWorkspace\default"

}

$WorkspaceRoot = [IO.Path]::GetFullPath($WorkspaceRoot)

$ControlRoot = Join-Path $Desktop "SmartAgent"

New-Item -ItemType Directory -Force -Path $ControlRoot,$WorkspaceRoot | Out-Null



$PublicLaunchers = @(
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

foreach ($required in $PublicLaunchers) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $required))) {
        throw "Required application launcher missing: $required"
    }
}

$canonicalProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$canonicalControlRoot = [IO.Path]::GetFullPath($ControlRoot).TrimEnd('\')
$controlAliasesProject = $canonicalControlRoot.Equals($canonicalProjectRoot, [StringComparison]::OrdinalIgnoreCase)

if ($controlAliasesProject) {
    Write-Host "[SmartAgent] Desktop control folder is the installed application folder; preserving canonical launchers." -ForegroundColor Yellow
} else {
    foreach ($launcher in $PublicLaunchers) {
        $target = Join-Path $ProjectRoot $launcher
        $wrapper = @"
@echo off
setlocal
if not exist "$target" (
  echo [SmartAgent] Installed application launcher was not found: $target
  exit /b 2
)
call "$target" %*
"@
        [IO.File]::WriteAllText((Join-Path $ControlRoot $launcher), $wrapper, (New-Object Text.UTF8Encoding($false)))
    }
}

Write-Host "[SmartAgent] Desktop control folder: $ControlRoot" -ForegroundColor Green

Write-Host "[SmartAgent] Default writable workspace: $WorkspaceRoot" -ForegroundColor Green
