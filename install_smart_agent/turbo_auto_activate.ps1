# Git51 safe automatic AppContainer enrollment for an ACL ON installation.
# This is a deliberately LIMITED native OS broker, NOT unrestricted Full Access.
# Existing/inhabited trees are never silently re-ACL'd. Scope stays fail-closed.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [ValidateSet('Apply','Status')][string]$Action='Apply',
    [string]$TurboRoot=''
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$secure=Join-Path $root 'localdata\secure\windows_security'
$modePath=Join-Path $secure 'acl_mode.json'
$policyPath=Join-Path $secure 'workspace_access_policy.json'
$legacyJournal=Join-Path $secure 'turbo_os_activation.json'
$journalPath=$legacyJournal
. (Join-Path $PSScriptRoot 'turbo_multi_root_paths.ps1')
$native=Join-Path $PSScriptRoot 'turbo_os_activation.ps1'
function Read-State([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ('TURBO_AUTOSTART_REQUIRED_STATE_MISSING:'+ $Path)
    }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop)
}
$mode=Read-State $modePath
if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
    -not ([IO.Path]::GetFullPath([string]$mode.install_root).TrimEnd('\')).Equals(
        $root,[StringComparison]::OrdinalIgnoreCase) -or $mode.mode -ne 'on') {
    throw 'TURBO_AUTOSTART_ACL_ON_REQUIRED'
}
$policy=Read-State $policyPath
if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1') {
    throw 'TURBO_AUTOSTART_INVALID_POLICY'
}
if ($policy.turbo_requested -ne $true) {
    Write-Host '[Turbo OS] DISABLED: no Turbo Root requested.'
    return
}
$roots=@($policy.turbo_roots)
if ($roots.Count -lt 1) {throw 'GIT62_TURBO_AUTOSTART_NO_ROOT'}
if($roots.Count -gt 1 -and -not $TurboRoot){
    throw 'GIT62_TURBO_AUTOSTART_EXACT_ROOT_REQUIRED'
}
$target=if($TurboRoot){[IO.Path]::GetFullPath($TurboRoot).TrimEnd('\')}
        else{[IO.Path]::GetFullPath([string]$roots[0]).TrimEnd('\')}
if(@($roots | Where-Object {
   [IO.Path]::GetFullPath([string]$_).TrimEnd('\').Equals(
      $target,[StringComparison]::OrdinalIgnoreCase)
}).Count -ne 1){throw 'GIT62_TURBO_AUTOSTART_ROOT_NOT_REGISTERED'}
$indexed=Get-TurboRootJournalPaths $secure $target
if($roots.Count -gt 1 -or (Test-Path -LiteralPath $indexed.activation -PathType Leaf)){
    $journalPath=$indexed.activation
}
$writable=@($policy.writable_workspaces | ForEach-Object {
    [IO.Path]::GetFullPath([string]$_).TrimEnd('\')
})
$matches=@($writable | Where-Object {
    $target.StartsWith($_ + '\',[StringComparison]::OrdinalIgnoreCase)
})
if ($matches.Count -ne 1) { throw 'TURBO_AUTOSTART_WORKSPACE_NOT_UNIQUE' }
$workspace=$matches[0]
if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
    # Never rewrite a potentially active AppContainer SID or silently
    # reset a stale enrollment. The revoker owns that transition.
    $journal=Read-State $journalPath
    $same=$journal.schema -eq 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -and
        $journal.status -eq 'ACTIVE_PROVISIONED' -and
        [int]$journal.policy_revision -eq [int]$policy.revision -and
        ([IO.Path]::GetFullPath([string]$journal.install_root).TrimEnd('\')).Equals(
            $root,[StringComparison]::OrdinalIgnoreCase) -and
        ([IO.Path]::GetFullPath([string]$journal.workspace_root).TrimEnd('\')).Equals(
            $workspace,[StringComparison]::OrdinalIgnoreCase) -and
        ([IO.Path]::GetFullPath([string]$journal.turbo_root).TrimEnd('\')).Equals(
            $target,[StringComparison]::OrdinalIgnoreCase)
    if (-not $same) { throw 'TURBO_AUTOSTART_STALE_ENROLLMENT_REQUIRES_VERIFIED_REVOCATION' }
    Write-Host '[Turbo OS] Native broker already enrolled. Per-action OS attestation is still required.'
    return
}
if (Test-Path -LiteralPath $target) {
    throw ('TURBO_AUTOSTART_EXISTING_ROOT_IMPORT_UNSUPPORTED: ' + $target +
        '; existing project files and ACLs were NOT changed')
}
if ($Action -eq 'Status') {
    Write-Host '[Turbo OS] Eligible brand-new Root; native enrollment not yet activated.'
    return
}
$principal=[Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'TURBO_AUTOSTART_ADMINISTRATOR_REQUIRED'
}
if (-not (Test-Path -LiteralPath $native -PathType Leaf)) {
    throw 'TURBO_AUTOSTART_NATIVE_ACTIVATOR_MISSING'
}
& $native -Action Activate -ProjectRoot $root -Workspace $workspace -TurboRoot $target
if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
    throw 'TURBO_AUTOSTART_NATIVE_JOURNAL_MISSING'
}
$activated=Read-State $journalPath
if ($activated.status -ne 'ACTIVE_PROVISIONED' -or
    [int]$activated.policy_revision -ne [int]$policy.revision) {
    throw 'TURBO_AUTOSTART_NATIVE_ENROLLMENT_UNVERIFIED'
}
Write-Host '[Turbo OS] OS broker enrolled and native access probes passed.'
Write-Host '[Turbo OS] Per-action attestation REQUIRED; unrestricted Full-Access remains unavailable.'
