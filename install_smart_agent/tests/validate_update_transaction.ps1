$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$installerRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $installerRoot 'update.ps1') -LibraryOnly

foreach ($name in @(
    'Invoke-ExecutorCodeSwap',
    'Invoke-ExecutorDirectorySwap',
    'Restore-ExecutorCodeSwap',
    'Assert-ExistingPathChainNoReparsePoint',
    'Remove-CompletedExecutorArtifacts',
    'Invoke-CompletedArtifactCleanupForState',
    'Confirm-UnsignedBootstrap',
    'Grant-SmartAgentAclLease',
    'Set-SmartAgentExecutorCodeAcl',
    'Test-SmartAgentNoInteractiveWriteAcl'
)) {
    if (-not (Get-Command -Name $name -ErrorAction SilentlyContinue)) { throw "missing updater hardening function: $name" }
}
$updaterText = Get-Content -LiteralPath (Join-Path $installerRoot 'update.ps1') -Raw
$launcherText = Get-Content -LiteralPath (Join-Path (Split-Path -Parent $installerRoot) 'update.bat') -Raw
if ($launcherText -notmatch '%SystemRoot%\\System32\\WindowsPowerShell\\v1\.0\\powershell\.exe') { throw 'update launcher does not pin System32 Windows PowerShell' }
if ($launcherText -match '(?m)^powershell\.exe\s') { throw 'update launcher still resolves PowerShell through PATH' }
if ($updaterText -match '(?m)^\s*return\s+\(Get-FileHash\b') { throw 'updater hash verification still depends on a PowerShell module' }
if ($updaterText -match 'Remove-Item -LiteralPath \(Join-Path \$profileCodeRoot') { throw 'legacy in-place executor deletion remained' }
if ($updaterText -notmatch 'executor_code\.next\.') { throw 'executor staging path missing' }
if ($updaterText -notmatch 'executor_code\.retired\.') { throw 'executor rollback path missing' }
if ($updaterText -match '\$statePath\s*=\s*Join-Path \$endpoint') { throw 'transaction StatePath is shadowed by service state path' }
if ($updaterText -notmatch '\$serviceStatePath\s*=\s*Join-Path \$endpoint') { throw 'service state path was not renamed' }
if ($updaterText -notmatch "'EXECUTOR_QUIESCING'") { throw 'pre-stop quiescing state missing' }
if ($updaterText -notmatch '\[switch\]\$AllowUnsignedBootstrap') { throw 'unsigned bootstrap explicit opt-in switch missing' }
if ($updaterText -notmatch 'bootstrap_opt_in_required') { throw 'unsigned bootstrap fail-closed guard missing' }
if ($updaterText -match "'-NonInteractive', '-BootstrapProtectedUpdater'") { throw 'parent unsigned bootstrap launch suppresses elevated confirmation' }
if ($updaterText -match "Start-Process -FilePath 'powershell\.exe'") { throw 'UAC launch does not use System32 PowerShell path' }
if ($updaterText -notmatch '\$sourceHash \+ ''-'' \+ \$policyHash') { throw 'protected updater bundle is not dual-hash versioned' }
if ($updaterText -match 'Grant-SmartAgentAclLease \$codeRoot') { throw 'active executor tree receives a temporary ACL lease' }
if ($updaterText -notmatch 'Test-UpdateManifest \$InstallRoot \$targetRelease -AllowUnlisted -AllowContentDrift') { throw 'authorized target drift recovery is not scoped to target compatibility' }
$policyText = Get-Content -LiteralPath (Join-Path $installerRoot 'security_acl_policy.ps1') -Raw
if ($policyText -match "'/T'") { throw 'ACL policy uses icacls recursive mutation instead of an explicit staged seal' }
if ($policyText -match 'ControllerIdentity') { throw 'canonical executor ACL grants an interactive controller identity' }

$temp = Join-Path ([IO.Path]::GetTempPath()) ('smartagent-update-test-' + [Guid]::NewGuid().ToString('N'))
$source = Join-Path $temp 'source-release'
$target = Join-Path $temp 'installed-release'
$backup = Join-Path $target 'localdata\persistent\updates\test'
$junction = Join-Path $source 'linked-outside'

try {
    New-Item -ItemType Directory -Force -Path $temp | Out-Null
    $hashProbe = Join-Path $temp 'sha256-probe.txt'
    [IO.File]::WriteAllText($hashProbe, 'abc', (New-Object Text.UTF8Encoding($false)))
    if ((Get-FileSha256 $hashProbe) -ne 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad') { throw 'framework SHA-256 implementation mismatch' }

    $driftRoot = Join-Path $temp 'drifted-release'
    New-Item -ItemType Directory -Force -Path (Join-Path $driftRoot 'config') | Out-Null
    [IO.File]::WriteAllText((Join-Path $driftRoot 'payload.txt'), 'changed', (New-Object Text.UTF8Encoding($false)))
    $driftManifest = [ordered]@{
        schema='SMARTAGENT_UPDATE_MANIFEST_V1'; release_id='drift-test'; release_sequence=1
        protocol_family='SMARTAGENT_V9'; protocol_version=9; security_policy_version=1
        requirements=[ordered]@{}; files=[ordered]@{ 'payload.txt'=('0' * 64) }
    }
    Write-AtomicJson (Join-Path $driftRoot 'config\update_manifest.json') $driftManifest
    $driftProtocol = [ordered]@{ protocol_family='SMARTAGENT_V9'; protocol_version=9 }
    $strictDriftRejected = $false
    try { Test-UpdateManifest $driftRoot $driftProtocol -AllowUnlisted | Out-Null } catch { $strictDriftRejected = $_.Exception.Message -like 'update_full_manifest_hash_mismatch:payload.txt;*' }
    if (-not $strictDriftRejected) { throw 'source-style manifest validation accepted content drift' }
    $driftResult = Test-UpdateManifest $driftRoot $driftProtocol -AllowUnlisted -AllowContentDrift
    if (@($driftResult.content_drift).Count -ne 1 -or [string]$driftResult.content_drift[0] -ne 'payload.txt') { throw 'target drift recovery did not report the mismatched file' }

    New-Item -ItemType Directory -Force -Path `
        (Join-Path $source 'source\agent_core'), `
        (Join-Path $source 'config'), `
        (Join-Path $source 'doc'), `
        (Join-Path $target 'source\agent_core'), `
        (Join-Path $target 'legacy_module'), `
        (Join-Path $target 'config'), `
        (Join-Path $target 'localdata\secure'), `
        (Join-Path $target '.venv') | Out-Null

    [IO.File]::WriteAllText((Join-Path $source 'source\agent_core\new.py'), 'new-code')
    [IO.File]::WriteAllText((Join-Path $source 'doc\new.txt'), 'new-doc')
    [IO.File]::WriteAllText((Join-Path $source 'config\protocol_manifest.json'), 'new-manifest')
    [IO.File]::WriteAllText((Join-Path $source 'config\debug_config.json'), 'source-debug-must-not-copy')
    [IO.File]::WriteAllText((Join-Path $source 'update.bat'), 'new-launcher')

    [IO.File]::WriteAllText((Join-Path $target 'source\agent_core\new.py'), 'old-code')
    [IO.File]::WriteAllText((Join-Path $target 'source\agent_core\stale.py'), 'stale-code')
    [IO.File]::WriteAllText((Join-Path $target 'legacy_module\removed.py'), 'removed-code')
    [IO.File]::WriteAllText((Join-Path $target 'config\protocol_manifest.json'), 'old-manifest')
    [IO.File]::WriteAllText((Join-Path $target 'config\debug_config.json'), 'user-debug')
    [IO.File]::WriteAllText((Join-Path $target 'localdata\secure\telegram.enc'), 'credential')
    [IO.File]::WriteAllText((Join-Path $target '.venv\pyvenv.cfg'), 'venv')

    $oldManifest = [ordered]@{ files=@('source/agent_core/new.py','source/agent_core/stale.py','legacy_module/removed.py') }
    $newManifest = [ordered]@{ files=@('source/agent_core/new.py') }
    $record = Backup-ManagedPayload $source $target $backup $oldManifest
    Install-ManagedPayload $source $target
    Remove-StaleManagedFiles $target $oldManifest $newManifest

    $updatedCode = Get-Content -LiteralPath (Join-Path $target 'source\agent_core\new.py') -Raw
    if ($updatedCode -ne 'new-code') { throw "updated code missing: <$updatedCode>" }
    if (Test-Path -LiteralPath (Join-Path $target 'source\agent_core\stale.py')) { throw 'stale managed file survived mirror' }
    if (Test-Path -LiteralPath (Join-Path $target 'legacy_module\removed.py')) { throw 'removed top-level managed file survived manifest cleanup' }
    if ((Get-Content -LiteralPath (Join-Path $target 'config\debug_config.json') -Raw) -ne 'user-debug') { throw 'debug config changed' }
    if ((Get-Content -LiteralPath (Join-Path $target 'localdata\secure\telegram.enc') -Raw) -ne 'credential') { throw 'credential changed' }
    if ((Get-Content -LiteralPath (Join-Path $target '.venv\pyvenv.cfg') -Raw) -ne 'venv') { throw 'venv changed' }
    if (-not (Test-Path -LiteralPath (Join-Path $target 'update.bat'))) { throw 'new root launcher missing' }

    Restore-ManagedPayload $target $backup $record

    if ((Get-Content -LiteralPath (Join-Path $target 'source\agent_core\new.py') -Raw) -ne 'old-code') { throw 'rollback did not restore old code' }
    if (-not (Test-Path -LiteralPath (Join-Path $target 'source\agent_core\stale.py'))) { throw 'rollback did not restore stale file' }
    if (-not (Test-Path -LiteralPath (Join-Path $target 'legacy_module\removed.py'))) { throw 'rollback did not restore removed top-level managed file' }
    if (Test-Path -LiteralPath (Join-Path $target 'update.bat')) { throw 'rollback did not remove new root launcher' }
    if ((Get-Content -LiteralPath (Join-Path $target 'config\debug_config.json') -Raw) -ne 'user-debug') { throw 'rollback changed debug config' }
    if ((Get-Content -LiteralPath (Join-Path $target 'localdata\secure\telegram.enc') -Raw) -ne 'credential') { throw 'rollback changed credential' }

    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $aclCases = @(
        @{ name='users-rx'; sid='S-1-5-32-545'; rights=[Security.AccessControl.FileSystemRights]::ReadAndExecute; reject=$false },
        @{ name='authenticated-rx'; sid='S-1-5-11'; rights=[Security.AccessControl.FileSystemRights]::ReadAndExecute; reject=$false },
        @{ name='users-modify'; sid='S-1-5-32-545'; rights=[Security.AccessControl.FileSystemRights]::Modify; reject=$true },
        @{ name='authenticated-full'; sid='S-1-5-11'; rights=[Security.AccessControl.FileSystemRights]::FullControl; reject=$true },
        @{ name='users-change-permissions'; sid='S-1-5-32-545'; rights=[Security.AccessControl.FileSystemRights]::ChangePermissions; reject=$true },
        @{ name='authenticated-take-ownership'; sid='S-1-5-11'; rights=[Security.AccessControl.FileSystemRights]::TakeOwnership; reject=$true }
    )
    foreach ($aclCase in $aclCases) {
        $probe = Join-Path $temp ('acl-' + [string]$aclCase.name)
        New-Item -ItemType Directory -Force -Path $probe | Out-Null
        $probeAcl = New-Object Security.AccessControl.DirectorySecurity
        $probeAcl.SetAccessRuleProtection($true, $false)
        [void]$probeAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($currentIdentity, [Security.AccessControl.FileSystemRights]::FullControl, $allow)))
        $broadSid = New-Object Security.Principal.SecurityIdentifier([string]$aclCase.sid)
        [void]$probeAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($broadSid, $aclCase.rights, $allow)))
        Set-Acl -LiteralPath $probe -AclObject $probeAcl
        $rejected = $false
        try { Test-SmartAgentNoInteractiveWriteAcl $probe } catch { $rejected = $_.Exception.Message -like 'update_acl_interactive_write_forbidden:*' }
        if ([bool]$aclCase.reject -ne $rejected) { throw "broad ACL case produced the wrong result: $($aclCase.name)" }
    }

    $outside = Join-Path $temp 'outside'
    New-Item -ItemType Directory -Force -Path $outside | Out-Null
    New-Item -ItemType Junction -Path $junction -Target $outside | Out-Null
    $reparseRejected = $false
    try { Assert-ManagedTreesSafe $source 'test' } catch { $reparseRejected = $_.Exception.Message -like 'update_test_reparse_point_forbidden:*' }
    if (-not $reparseRejected) { throw 'managed reparse point was not rejected' }
    Remove-Item -LiteralPath $junction -Force

    $swapRoot = Join-Path $temp 'executor-swap'
    $active = Join-Path $swapRoot 'executor_code'
    $next = Join-Path $swapRoot 'executor_code.next.TEST'
    $retired = Join-Path $swapRoot 'executor_code.retired.TEST'
    New-Item -ItemType Directory -Force -Path $active,$next | Out-Null
    [IO.File]::WriteAllText((Join-Path $active 'version.txt'), 'old')
    [IO.File]::WriteAllText((Join-Path $next 'version.txt'), 'new')
    Invoke-ExecutorDirectorySwap -Active $active -Next $next -Retired $retired
    if ((Get-Content -LiteralPath (Join-Path $active 'version.txt') -Raw) -ne 'new') { throw 'staged executor swap did not activate new tree' }
    if ((Get-Content -LiteralPath (Join-Path $retired 'version.txt') -Raw) -ne 'old') { throw 'staged executor swap did not retain old tree' }
    $swapState = [pscustomobject]@{ update_id='TEST'; executor_swap=[pscustomobject]@{ active=$active; next=$next; retired=$retired } }
    Restore-ExecutorCodeSwap $swapState
    if ((Get-Content -LiteralPath (Join-Path $active 'version.txt') -Raw) -ne 'old') { throw 'executor swap recovery did not restore old tree' }
    if (-not (Test-Path -LiteralPath $swapState.executor_swap.failed -PathType Container)) { throw 'executor swap recovery did not quarantine failed tree' }

    $chainRoot = Join-Path $temp 'path-chain'
    $outsideChain = Join-Path $temp 'path-chain-outside'
    $chainLink = Join-Path $chainRoot 'localdata'
    New-Item -ItemType Directory -Force -Path $chainRoot,$outsideChain | Out-Null
    New-Item -ItemType Junction -Path $chainLink -Target $outsideChain | Out-Null
    $chainRejected = $false
    try { Assert-ExistingPathChainNoReparsePoint $chainRoot (Join-Path $chainLink 'secure\windows_security\executor_code') 'test_chain' } catch { $chainRejected = $_.Exception.Message -like 'update_test_chain_path_component_reparse_point:*' }
    if (-not $chainRejected) { throw 'install-to-executor path chain reparse point was not rejected' }
    Remove-Item -LiteralPath $chainLink -Force

    $cleanupInstall = Join-Path $temp 'cleanup-install'
    $cleanupActive = Join-Path $cleanupInstall 'localdata\secure\windows_security\executor_code'
    $cleanupId = 'UPDATE-CLEANUP-TEST'
    $cleanupRetired = $cleanupActive + '.retired.' + $cleanupId
    New-Item -ItemType Directory -Force -Path $cleanupActive,$cleanupRetired | Out-Null
    [IO.File]::WriteAllText((Join-Path $cleanupRetired 'old.py'), 'retired')
    $validCompletedState = [pscustomobject]@{
        status='COMPLETED'; update_id=$cleanupId; install_root=$cleanupInstall
        executor_swap=[pscustomobject]@{ active=$cleanupActive; retired=$cleanupRetired }
    }
    Invoke-CompletedArtifactCleanupForState $validCompletedState
    if (Test-Path -LiteralPath $cleanupRetired) { throw 'valid transaction-owned retired executor tree was not deleted' }
    if ($validCompletedState.status -ne 'COMPLETED') { throw 'successful completed cleanup rewrote terminal transaction status' }

    $corruptCompletedState = [pscustomobject]@{
        status='COMPLETED'; update_id=$cleanupId; install_root=$cleanupInstall
        executor_swap=[pscustomobject]@{ active=$cleanupActive; retired=$outsideChain }
    }
    Invoke-CompletedArtifactCleanupForState $corruptCompletedState
    if (-not (Test-Path -LiteralPath $outsideChain -PathType Container)) { throw 'outside corrupt cleanup state was not preserved' }
    if ($corruptCompletedState.status -ne 'COMPLETED') { throw 'corrupt completed cleanup rewrote terminal transaction status' }

    $confirmationRejected = $false
    try { Confirm-UnsignedBootstrap -NonInteractive -PromptReader { 'BOOTSTRAP' } | Out-Null } catch { $confirmationRejected = $_.Exception.Message -eq 'update_protected_updater_bootstrap_interactive_confirmation_required' }
    if (-not $confirmationRejected) { throw 'noninteractive unsigned bootstrap confirmation was accepted' }
    $confirmationCancelled = $false
    try { Confirm-UnsignedBootstrap -PromptReader { 'NO' } | Out-Null } catch { $confirmationCancelled = $_.Exception.Message -eq 'update_protected_updater_bootstrap_cancelled' }
    if (-not $confirmationCancelled) { throw 'incorrect unsigned bootstrap confirmation was accepted' }
    if (-not (Confirm-UnsignedBootstrap -PromptReader { 'BOOTSTRAP' })) { throw 'explicit unsigned bootstrap confirmation was not accepted' }

    Write-Output 'SMARTAGENT_UPDATE_TRANSACTION_TEST_OK'
} finally {
    if (Test-Path -LiteralPath $junction) { Remove-Item -LiteralPath $junction -Force }
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
