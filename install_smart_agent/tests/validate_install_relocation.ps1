[CmdletBinding()]
param([string]$ProjectRoot = '')

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
}
$preparer = Join-Path $ProjectRoot 'install_smart_agent\prepare_install_instance.ps1'
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($preparer,[ref]$tokens,[ref]$errors) | Out-Null
if (@($errors).Count -gt 0) { throw "install_relocation_parser_failed:$($errors[0].Message)" }

$temp = Join-Path ([IO.Path]::GetTempPath()) ('smartagent-relocation-test-' + [Guid]::NewGuid().ToString('N'))
$app = Join-Path $temp 'copied-app'
$fakeLocalAppData = Join-Path $temp 'local-app-data'
try {
    $security = Join-Path $app 'localdata\secure\windows_security'
    $metadata = Join-Path $app 'localdata\metadata'
    New-Item -ItemType Directory -Force -Path $security,$metadata,(Join-Path $app '.venv\Scripts') | Out-Null
    [IO.File]::WriteAllText((Join-Path $app '.venv\Scripts\python.exe'), 'relocated-venv')
    [IO.File]::WriteAllText(
        (Join-Path $security 'acl_mode.json'),
        (@{
            schema='SMARTAGENT_ACL_MODE_V1'
            mode='off'
            install_root=$app
            controller_sid='S-1-5-21-111111111-222222222-333333333-4444'
        } | ConvertTo-Json),
        (New-Object Text.UTF8Encoding($false))
    )
    [IO.File]::WriteAllText(
        (Join-Path $app 'localdata\secure\telegram.enc'),
        'copied-secret',
        (New-Object Text.UTF8Encoding($false))
    )
    $priorLocalAppData = $env:LOCALAPPDATA
    $env:LOCALAPPDATA = $fakeLocalAppData
    try {
        & $preparer -ProjectRoot $app
        & $preparer -ProjectRoot $app
    } finally {
        $env:LOCALAPPDATA = $priorLocalAppData
    }
    $instance = Get-Content -LiteralPath (Join-Path $metadata 'install_instance.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($instance.schema -ne 'SMARTAGENT_INSTALL_INSTANCE_V1' -or
        [IO.Path]::GetFullPath([string]$instance.install_root) -ne [IO.Path]::GetFullPath($app) -or
        [string]::IsNullOrWhiteSpace([string]$instance.controller_sid)) {
        throw 'install_relocation_instance_binding_invalid'
    }
    if (Test-Path -LiteralPath (Join-Path $security 'acl_mode.json')) {
        throw 'install_relocation_foreign_acl_state_survived'
    }
    if (Test-Path -LiteralPath (Join-Path $app '.venv')) {
        throw 'install_relocation_virtual_environment_survived'
    }
    $backups = @(Get-ChildItem -LiteralPath (Join-Path $fakeLocalAppData 'SmartAgent\install_backups') -Directory)
    if ($backups.Count -ne 1) { throw "install_relocation_backup_count_invalid:$($backups.Count)" }
    if (-not (Test-Path -LiteralPath (Join-Path $backups[0].FullName 'localdata\secure\telegram.enc') -PathType Leaf)) {
        throw 'install_relocation_sensitive_state_not_quarantined'
    }
    Write-Host 'INSTALL_RELOCATION_VALIDATION_PASS'
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
