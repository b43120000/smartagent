[CmdletBinding()]
param([string]$ProjectRoot = '')

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
} else {
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
}

$deploy = Join-Path $ProjectRoot 'install_smart_agent\deploy_public_layout.ps1'
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($deploy, [ref]$tokens, [ref]$errors) | Out-Null
if (@($errors).Count -gt 0) { throw "public_layout_parser_failed:$($errors[0].Message)" }

$launchers = @(
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

$temp = Join-Path ([IO.Path]::GetTempPath()) ('smartagent-public-layout-collision-' + [Guid]::NewGuid().ToString('N'))
$desktop = Join-Path $temp 'Desktop'
$app = Join-Path $desktop 'SmartAgent'
$workspace = Join-Path $desktop 'SmartAgentWorkspace\default'
try {
    New-Item -ItemType Directory -Force -Path $app | Out-Null
    foreach ($launcher in $launchers) {
        $marker = "CANONICAL:$launcher"
        [IO.File]::WriteAllText((Join-Path $app $launcher), $marker, (New-Object Text.UTF8Encoding($false)))
    }

    & $deploy -ProjectRoot $app -WorkspaceRoot $workspace -DesktopRoot $desktop

    foreach ($launcher in $launchers) {
        $path = Join-Path $app $launcher
        $actual = [IO.File]::ReadAllText($path)
        $expected = "CANONICAL:$launcher"
        if ($actual -ne $expected) { throw "public_layout_collision_overwrote_canonical_launcher:$launcher" }
        if ($actual -match '(?i)\bcall\b') { throw "public_layout_collision_created_self_call_wrapper:$launcher" }
    }
    if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
        throw 'public_layout_collision_workspace_missing'
    }

    Write-Host 'PUBLIC_LAYOUT_COLLISION_VALIDATION_PASS'
} finally {
    if ([IO.Directory]::Exists($temp)) { [IO.Directory]::Delete($temp, $true) }
}
