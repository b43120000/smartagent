# Read-only Telegram/Runtime Workspace vs Turbo Broker enrollment diagnosis.
# Does not modify ACL, policy, task binding or AppContainer grants.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [string]$Workspace=''
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$python=@(
    (Join-Path $root 'python_runtime\python.exe'),
    (Join-Path $root 'python_runtime\Scripts\python.exe'),
    (Join-Path $root 'python\python.exe')
) | Where-Object {Test-Path -LiteralPath $_ -PathType Leaf} | Select-Object -First 1
if(-not $python) {
    $cmd=Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if($cmd){$python=[string]$cmd.Source}
}
if(-not $python){throw 'TURBO_BINDING_DIAGNOSTIC_PYTHON_MISSING'}
$env:PYTHONPATH=Join-Path $root 'source'
if($Workspace){
    & $python -m agent_core.turbo_binding_diagnostics --workspace $Workspace
}else{
    & $python -m agent_core.turbo_binding_diagnostics
}
if($LASTEXITCODE -ne 0){throw 'TURBO_BINDING_DIAGNOSTIC_FAILED'}
