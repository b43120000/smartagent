#requires -Version 5.1
# Non-deployment StrictMode regression for the e5 NLIT isolated-Python adapter.
# Uses only disposable synthetic paths. Does not launch Python, browser or supervisor.
[CmdletBinding()]
param(
  [string]$AdapterPath = (Join-Path $PSScriptRoot 'Enable-NlitIsolatedPython-v2.ps1')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert([bool]$condition,[string]$label) {
  if (-not $condition) { throw ('NLIT_STRICTMODE_TEST_FAIL:' + $label) }
}

$adapter = (Resolve-Path -LiteralPath $AdapterPath -ErrorAction Stop).ProviderPath
$root = Join-Path ([IO.Path]::GetTempPath()) ('nlit-adapter-strictmode-' + [guid]::NewGuid().ToString('N'))
$test = Join-Path $root 'test-install'
$prod = Join-Path $root 'production-sentinel'
$obs = Join-Path $root 'external-observer'
$backup = Join-Path $root 'private-backup'
$scriptDir = Join-Path $test '.venv\Scripts'
$python = Join-Path $scriptDir 'python.exe'
$pth = Join-Path $scriptDir 'python313._pth'
$utf8 = New-Object System.Text.UTF8Encoding($false)

try {
  foreach ($dir in @($scriptDir, $prod, $obs, $backup)) {
    [void][IO.Directory]::CreateDirectory($dir)
  }
  # A synthetic executable is sufficient: Probe is strictly read-only and
  # must not invoke Python, Enable, Verify or Restore.
  [IO.File]::WriteAllBytes($python, [byte[]](77,90))
  $original = $utf8.GetBytes("python313.zip`r`n.`r`nimport site`r`n")
  [IO.File]::WriteAllBytes($pth, $original)
  Assert ((Get-Item -LiteralPath $test) -is [IO.DirectoryInfo]) 'directory_fixture_type'
  Assert ((Get-Item -LiteralPath $python) -is [IO.FileInfo]) 'python_file_fixture_type'
  Assert ((Get-Item -LiteralPath $pth) -is [IO.FileInfo]) 'pth_file_fixture_type'

  $output = @(& $adapter -Mode Probe -TestInstall $test -ProductionInstall $prod -ObserverDir $obs -BackupDir $backup 2>&1)
  Assert (($output -join "`n") -match 'NLIT_PYTHON_PTH_PROBE_PASS') 'probe_marker'
  Assert ([Convert]::ToBase64String([IO.File]::ReadAllBytes($pth)) -ceq [Convert]::ToBase64String($original)) 'probe_did_not_change_pth'
  Assert (-not (Test-Path -LiteralPath (Join-Path $backup 'nlit_python313._pth.original.bin'))) 'probe_no_backup'
  Assert (-not (Test-Path -LiteralPath (Join-Path $backup 'nlit_python313._pth.state.json'))) 'probe_no_state'
  Write-Output 'NLIT_STRICTMODE_FILEINFO_DIRECTORYINFO_PASS'

  # Both roots and .exe/_pth leaf entries are visited under StrictMode.
  # The original v1 version dereferenced FileInfo.Parent and failed Probe.
  $caught = $false
  try {
    & $adapter -Mode Probe -TestInstall $test -ProductionInstall $test -ObserverDir $obs -BackupDir $backup | Out-Null
  } catch {
    if ($_.Exception.Message -match 'ROOTS_NOT_DISJOINT') { $caught = $true }
    else { throw }
  }
  Assert $caught 'overlap_rejected'
  Write-Output 'NLIT_STRICTMODE_OVERLAP_GUARD_PASS'

  # Probe must reject a bad _pth without trying to repair/execute it.
  [IO.File]::WriteAllBytes($pth, $utf8.GetBytes("python313.zip`n.`n"))
  $caught = $false
  try {
    & $adapter -Mode Probe -TestInstall $test -ProductionInstall $prod -ObserverDir $obs -BackupDir $backup | Out-Null
  } catch {
    if ($_.Exception.Message -match 'IMPORT_SITE_REQUIRED') { $caught = $true }
    else { throw }
  }
  Assert $caught 'import_site_guard'
  Write-Output 'NLIT_STRICTMODE_IMPORT_SITE_GUARD_PASS'
  Write-Output 'NLIT_STRICTMODE_REGRESSION_PASS'
}
finally {
  if (Test-Path -LiteralPath $root) {
    # Delete only the unique temporary fixture created by this invocation.
    [IO.Directory]::Delete($root, $true)
  }
}
