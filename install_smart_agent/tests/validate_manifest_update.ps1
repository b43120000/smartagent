param([string]$PythonExe = 'python')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$updater = Join-Path $projectRoot 'install_smart_agent\manifest_update.ps1'
$builder = Join-Path $projectRoot 'install_smart_agent\build_update_manifest.py'
$launcher = Get-Content -Raw -LiteralPath (Join-Path $projectRoot 'update.bat')
if ($launcher -notmatch 'manifest_update\.ps1') { throw 'update.bat does not select manifest updater' }
if ($launcher -notmatch 'build_update_manifest\.py') { throw 'update.bat does not rebuild update manifest' }
if ($launcher -notmatch 'write_protocol_manifest') { throw 'update.bat does not rebuild protocol manifest' }

$temp = Join-Path ([IO.Path]::GetTempPath()) ('smartagent-manifest-update-' + [Guid]::NewGuid().ToString('N'))
$source = Join-Path $temp 'source-release'
$target = Join-Path $temp 'installed-release'
try {
    foreach ($directory in @(
        (Join-Path $source 'source\agent_core'),
        (Join-Path $source 'install_smart_agent'),
        (Join-Path $source 'config'),
        (Join-Path $source 'localdata'),
        (Join-Path $target 'source\agent_core'),
        (Join-Path $target 'install_smart_agent'),
        (Join-Path $target 'config'),
        (Join-Path $target 'localdata\secure\windows_security'),
        (Join-Path $target '.venv')
    )) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }

    Copy-Item -LiteralPath $updater -Destination (Join-Path $source 'install_smart_agent\manifest_update.ps1')
    [IO.File]::WriteAllText((Join-Path $source 'source\smart_agent.py'), 'entry')
    [IO.File]::WriteAllText((Join-Path $source 'source\agent_core\new.py'), 'new-code')
    [IO.File]::WriteAllText((Join-Path $source 'source\agent_core\added_later.py'), 'new-script')
    [IO.File]::WriteAllText((Join-Path $source 'update.bat'), 'new-launcher')
    [IO.File]::WriteAllText(
        (Join-Path $source 'config\protocol_manifest.json'),
        '{"protocol_family":"SMARTAGENT_V9","protocol_version":9}'
    )
    [IO.File]::WriteAllText((Join-Path $source 'config\debug_config.json'), 'source-debug')
    [IO.File]::WriteAllText((Join-Path $source 'localdata\must-not-deploy.txt'), 'source-local')

    [IO.File]::WriteAllText((Join-Path $target 'source\agent_core\new.py'), 'old-code')
    [IO.File]::WriteAllText((Join-Path $target 'source\agent_core\stale.py'), 'stale-code')
    [IO.File]::WriteAllText((Join-Path $target 'config\debug_config.json'), 'target-debug')
    [IO.File]::WriteAllText((Join-Path $target '.venv\pyvenv.cfg'), 'target-venv')
    $aclState = [ordered]@{
        schema = 'SMARTAGENT_ACL_MODE_V1'
        mode = 'off'
        install_root = [IO.Path]::GetFullPath($target).TrimEnd('\')
    } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $target 'localdata\secure\windows_security\acl_mode.json'), $aclState)

    & $PythonExe $builder --root $source --release-id 'manifest-update-test'
    if ($LASTEXITCODE -ne 0) { throw "manifest builder failed: $LASTEXITCODE" }

    $systemPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $output = & $systemPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $updater -SourceRoot $source -InstallRoot $target 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("manifest updater failed: " + ($output -join "`n")) }
    if (($output -join "`n") -notmatch 'UPDATE_OK') { throw 'manifest updater did not report UPDATE_OK' }

    if ((Get-Content -Raw -LiteralPath (Join-Path $target 'source\agent_core\new.py')) -ne 'new-code') { throw 'changed source was not updated' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $target 'source\agent_core\added_later.py')) -ne 'new-script') { throw 'new script was not automatically deployed' }
    if (Test-Path -LiteralPath (Join-Path $target 'source\agent_core\stale.py')) { throw 'stale managed file was not removed' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $target 'config\debug_config.json')) -ne 'target-debug') { throw 'debug config was overwritten' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $target '.venv\pyvenv.cfg')) -ne 'target-venv') { throw '.venv was overwritten' }
    if (-not (Test-Path -LiteralPath (Join-Path $target 'localdata\secure\windows_security\acl_mode.json'))) { throw 'localdata was removed' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $source 'config\update_manifest.json')).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $target 'config\update_manifest.json')).Hash) { throw 'release manifest was not synchronized' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $source 'install_smart_agent\manifest_update.ps1')).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $target 'install_smart_agent\manifest_update.ps1')).Hash) { throw 'updater was not self-updated into target' }

    Write-Output 'SMARTAGENT_MANIFEST_UPDATE_TEST_OK'
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
