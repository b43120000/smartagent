# Git62 post-pull transaction finalizer. This does NOT perform git pull,
# overwrite the protected updater, or assert rollback to an unbacked prior checkout.
# It verifies the exact checked-out commit and complete managed file manifest
# before running any privileged mutation. Fail-closed on every Native Root.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Commit,
 [string]$ProjectRoot='',
 [switch]$Elevated,
 [switch]$VerifyOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$stage='INPUT'
$authorizationUpdated=$false
function Say([string]$message){Write-Host ('[SmartAgent Update] '+$message)}
function Require([bool]$ok,[string]$reason){if(-not $ok){throw $reason}}
try {
 Require ($Commit -cmatch '^[0-9a-fA-F]{40}$') 'UPDATE_COMMIT_SHA_INVALID'
 $Commit=$Commit.ToLowerInvariant()
 if([string]::IsNullOrWhiteSpace($ProjectRoot)){
   $ProjectRoot=Split-Path -Parent $PSScriptRoot
 }
 $root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
 Require (Test-Path -LiteralPath $root -PathType Container) 'UPDATE_INSTALL_ROOT_MISSING'
 Require (Test-Path -LiteralPath (Join-Path $root '.git') -PathType Container) 'UPDATE_POST_PULL_GIT_CHECKOUT_REQUIRED'
 $python=Join-Path $root '.venv\Scripts\python.exe'
 Require (Test-Path -LiteralPath $python -PathType Leaf) 'UPDATE_INSTALLED_PYTHON_MISSING'
 $manifest=Join-Path $root 'config\protocol_manifest.json'
 Require (Test-Path -LiteralPath $manifest -PathType Leaf) 'UPDATE_MANIFEST_MISSING'
 $env:PYTHONPATH=(Join-Path $root 'source')+';'+[string]$env:PYTHONPATH
 $stage='VERIFY_COMMIT_AND_FILES'
 $validate='import sys; from pathlib import Path; from agent_core.force_stop_all_agents import _verify_upgrade_commit; _verify_upgrade_commit(Path(sys.argv[1]),sys.argv[2])'
 & $python -c $validate $root $Commit
 Require ($LASTEXITCODE -eq 0) 'UPDATE_COMMIT_OR_PROTOCOL_MANIFEST_INVALID'
 $expectedHash=(Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
 Say ('CHECKOUT_VERIFIED commit='+$Commit+' manifest_sha256='+$expectedHash)
 $stage='ELEVATION'
 $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
 $principal=[Security.Principal.WindowsPrincipal]::new($identity)
 if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
   Require (-not $Elevated) 'UPDATE_ELEVATION_NOT_GRANTED'
   $exe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
   $arg='-NoProfile -ExecutionPolicy Bypass -File "'+$PSCommandPath+'" -Commit '+$Commit+' -ProjectRoot "'+$root+'" -Elevated'
   if($VerifyOnly){$arg+=' -VerifyOnly'}
   Say 'Requesting one administrator authorization for native precheck and manifest rebind.'
   $child=Start-Process -FilePath $exe -ArgumentList $arg -Verb RunAs -Wait -PassThru -ErrorAction Stop
   Require ($child.ExitCode -eq 0) ('UPDATE_ELEVATED_WORKER_FAILED exit_code='+$child.ExitCode)
   exit 0
 }
 $stage='CHECK_EXISTING_SECURITY_OWNER'
 Require (-not [string]::IsNullOrWhiteSpace([string]$env:ProgramData)) 'UPDATE_PROGRAMDATA_MISSING'
 $security=Join-Path $root 'localdata\secure\windows_security'
 $modePath=Join-Path $security 'acl_mode.json'
 $policyPath=Join-Path $security 'workspace_access_policy.json'
 $machinePath=Join-Path $env:ProgramData 'SmartAgent\machine_authorization.json'
 foreach($p in @($modePath,$policyPath,$machinePath)){
   Require (Test-Path -LiteralPath $p -PathType Leaf) ('UPDATE_REQUIRED_SECURITY_STATE_MISSING:'+ $p)
 }
 $mode=Get-Content -LiteralPath $modePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
 $policy=Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
 $machine=Get-Content -LiteralPath $machinePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
 Require ($mode.schema -eq 'SMARTAGENT_ACL_MODE_V1' -and $mode.mode -eq 'on' -and
   [string]$mode.install_root -ieq $root) 'UPDATE_REQUIRES_EXISTING_ACL_ON'
 Require ($policy.schema -eq 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -and
   $policy.turbo_requested -eq $true) 'UPDATE_REQUIRES_ACTIVE_MULTI_ROOT_POLICY'
 Require ($machine.schema -eq 'SMARTAGENT_MACHINE_AUTHORIZATION_V1' -and
   [string]$machine.install_root -ieq $root -and
   [string]$machine.protocol_manifest_sha256 -match '^[0-9a-fA-F]{64}$') 'UPDATE_MACHINE_OWNER_NOT_TRUSTED'
 $machineAcl=Get-Acl -LiteralPath $machinePath -ErrorAction Stop
 Require ($machineAcl.AreAccessRulesProtected) 'UPDATE_MACHINE_AUTHORIZATION_ACL_UNPROTECTED'
 $pinnedUpdater=Join-Path $root 'install_smart_agent\\update.ps1'
 $pinnedPolicy=Join-Path $root 'install_smart_agent\\security_acl_policy.ps1'
 Require (Test-Path -LiteralPath $pinnedUpdater -PathType Leaf) 'UPDATE_PINNED_UPDATER_MISSING'
 Require (Test-Path -LiteralPath $pinnedPolicy -PathType Leaf) 'UPDATE_PINNED_POLICY_MISSING'
 Require ([string]$machine.updater_sha256 -ieq (Get-FileHash -LiteralPath $pinnedUpdater -Algorithm SHA256).Hash) 'UPDATE_PINNED_UPDATER_CHANGED'
 Require ([string]$machine.updater_policy_sha256 -ieq (Get-FileHash -LiteralPath $pinnedPolicy -Algorithm SHA256).Hash) 'UPDATE_PINNED_UPDATER_POLICY_CHANGED'
 Require (Test-Path -LiteralPath ([string]$machine.updater_path) -PathType Leaf) 'UPDATE_MACHINE_PINNED_UPDATER_MISSING'
 Require ([string]$machine.updater_sha256 -ieq (Get-FileHash -LiteralPath ([string]$machine.updater_path) -Algorithm SHA256).Hash) 'UPDATE_MACHINE_PINNED_UPDATER_CHANGED'
 $roots=@($policy.turbo_roots)
 Require ($roots.Count -gt 1) 'UPDATE_SINGLE_ROOT_REQUIRES_SEPARATE_VERIFIED_MIGRATION'
 $previousHash=([string]$machine.protocol_manifest_sha256).ToLowerInvariant()
 $rebind=Join-Path $root 'install_smart_agent\turbo_multi_root_rebind.ps1'
 Require (Test-Path -LiteralPath $rebind -PathType Leaf) 'UPDATE_NATIVE_PRECHECK_HELPER_MISSING'
 $stage='NATIVE_ROOT_PRECHECK'
 Say ('PRECHECK_ROOTS count='+$roots.Count)
 & $rebind -ProjectRoot $root -VerifyOnly
 Require ($?) 'UPDATE_NATIVE_ROOT_PRECHECK_FAILED'
 if($VerifyOnly){
   Say ('UPDATE_VERIFY_PASS manifest_match='+[string]($previousHash -eq $expectedHash)+'; roots='+$roots.Count)
   exit 0
 }
 $stage='STOP_AGENTS'
 Say 'Stopping agents with normal force-stop (no authorization mutation).'
 & $python -m agent_core.force_stop_all_agents --root $root
 Require ($LASTEXITCODE -eq 0) 'UPDATE_FORCE_STOP_FAILED'
 $stage='RECHECK_COMMIT'
 & $python -c $validate $root $Commit
 Require ($LASTEXITCODE -eq 0) 'UPDATE_COMMIT_CHANGED_DURING_STOP'
 Require ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expectedHash) 'UPDATE_MANIFEST_CHANGED_DURING_STOP'
 $stage='REBIND_MACHINE_AUTHORIZATION'
 if($previousHash -ne $expectedHash){
   Say 'Running existing protected-updater/SID-verified admin manifest rebind.'
   $upgrade=Join-Path $root 'install_smart_agent\turbo_multi_root_upgrade_authorization.ps1'
   Require (Test-Path -LiteralPath $upgrade -PathType Leaf) 'UPDATE_NATIVE_AUTHORIZATION_HELPER_MISSING'
   & $upgrade -ProjectRoot $root
   Require ($?) 'UPDATE_MACHINE_REBIND_FAILED'
   $authorizationUpdated=$true
 } else {
   Say 'MACHINE_MANIFEST_ALREADY_MATCHED'
 }
 $stage='POSTCHECK_MACHINE'
 $published=Get-Content -LiteralPath $machinePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
 Require ([string]$published.protocol_manifest_sha256 -ieq $expectedHash -and
   [string]$published.executor_sid -eq [string]$machine.executor_sid -and
   [string]$published.install_root -ieq $root) 'UPDATE_MACHINE_POSTCONDITION_MISMATCH'
 & $python -c 'from agent_core.windows_security import load_machine_authorization; assert load_machine_authorization() is not None'
 Require ($LASTEXITCODE -eq 0) 'UPDATE_MACHINE_AUTHORIZATION_NOT_TRUSTED'
 $stage='POSTCHECK_NATIVE_ROOTS'
 & $rebind -ProjectRoot $root -VerifyOnly
 Require ($?) 'UPDATE_NATIVE_POSTCHECK_FAILED'
 $stage='POSTCHECK_REMOTE'
 & $python -m agent_core.security_preflight --interface remote
 Require ($LASTEXITCODE -eq 0) 'UPDATE_REMOTE_PREFLIGHT_FAILED'
 $stage='COMPLETE'
 Say ('UPDATE_STATUS=PASS commit='+$Commit+' manifest_sha256='+$expectedHash+' roots='+$roots.Count)
 exit 0
} catch {
 Say ('UPDATE_STATUS=FAIL stage='+$stage+' reason='+$_.Exception.Message)
 if($authorizationUpdated){
   Say 'RECOVERY_REQUIRED: authorization was rebound, but postcheck failed. Agents remain fail-closed; no blind hash rollback.'
 } else {
   Say 'NO_AUTHORIZATION_COMMIT: precheck or rebind failed before a successful publication.'
 }
 Say 'POST_PULL_MODE: checkout was already changed by git pull; automatic rollback of pre-pull program bytes is NOT guaranteed.'
 exit 20
}
