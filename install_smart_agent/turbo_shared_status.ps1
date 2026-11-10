[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProjectRoot)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot)
$python = @(
    (Join-Path $root 'python_runtime\\python.exe'),
    (Join-Path $root 'python_runtime\\Scripts\\python.exe'),
    (Join-Path $root 'python\\python.exe')
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) {
    $candidate=Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $python=[string]$candidate.Source }
}
if (-not $python) {
    Write-Host 'SmartAgent ACL/Turbo shared state: STATUS_UNAVAILABLE_NO_PYTHON' -ForegroundColor Yellow
    exit 2
}
$env:PYTHONPATH=Join-Path $root 'source'
try {
    $raw=& $python -m agent_core.turbo_sandbox 2>&1
    $state=($raw | Out-String | ConvertFrom-Json -ErrorAction Stop)
    if ($state.schema -ne 'SMARTAGENT_TURBO_SANDBOX_STATE_V1') {
        throw 'TURBO_SHARED_STATUS_INVALID_SCHEMA'
    }
    Write-Host ('ACL Mode: ' + $state.acl_mode)
    Write-Host ('Turbo Requested: ' + $state.turbo_requested)
    Write-Host ('Turbo Effective: ' + $state.turbo_effective)
    Write-Host ('Sandbox Status: ' + $state.sandbox_status)
    Write-Host ('Reason Code: ' + $state.reason_code)
    Write-Host ('Policy Revision: ' + $state.policy_revision)
    if ($state.turbo_effective -ne 'ON') {
        Write-Host 'Turbo OS Full-Access: NOT ENABLED' -ForegroundColor Yellow
    }
    exit 0
} catch {
    Write-Host ('SmartAgent ACL/Turbo shared status: UNAVAILABLE ' + $_.Exception.GetType().Name) -ForegroundColor Yellow
    exit 2
}
