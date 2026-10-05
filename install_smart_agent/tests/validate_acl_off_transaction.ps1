[CmdletBinding()]
param([string]$ProjectRoot = '')

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
} else {
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
}

$scriptPath = Join-Path $ProjectRoot 'install_smart_agent\acl_status.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath, [ref]$tokens, [ref]$errors
)
if (@($errors).Count -gt 0) {
    throw "acl_off_transaction_parse_failed:$($errors[0].Message)"
}

$text = Get-Content -LiteralPath $scriptPath -Raw -Encoding UTF8
$postconditionFunction = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Assert-AclOffPostcondition'
}, $true)
if (-not $postconditionFunction) {
    throw 'acl_off_postcondition_function_missing'
}
$ownedRootsFunction = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-AclOffOwnedRoots'
}, $true)
if (-not $ownedRootsFunction) {
    throw 'acl_off_owned_roots_function_missing'
}
$safeInstallRootFunction = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Assert-SafeInstallRoot'
}, $true)
$ancestorFunction = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Assert-ContainedPathHasNoReparseAncestor'
}, $true)
if (-not $safeInstallRootFunction -or -not $ancestorFunction) {
    throw 'acl_off_path_boundary_function_missing'
}

$safeRootCall = $text.IndexOf('Assert-SafeInstallRoot $ProjectRoot')
$offBoundary = $text.IndexOf("`n" + "Write-ModeState 'transitioning_off'")
$transition = $text.IndexOf("Write-ModeState 'transitioning_off'", $offBoundary)
$stopTask = $text.IndexOf('Stop-ScheduledTask -TaskName $TaskName', $offBoundary)
$rootGrant = $text.IndexOf('Invoke-Icacls @($ProjectRoot', $stopTask)
$relaxationLoop = $text.IndexOf('foreach ($path in $mutationRoots)', $rootGrant)
$postcondition = $text.IndexOf('Assert-AclOffPostcondition $callerSid', $offBoundary)
$commit = $text.IndexOf("Write-ModeState 'off'", $offBoundary)
if ($safeRootCall -lt 0 -or $offBoundary -lt $safeRootCall -or $transition -lt $offBoundary -or $stopTask -lt $transition -or
    $rootGrant -lt $stopTask -or $relaxationLoop -lt $rootGrant -or
    $postcondition -lt $relaxationLoop -or $commit -lt $postcondition) {
    throw 'acl_off_transaction_order_invalid'
}
if ($text.IndexOf("Write-ModeState 'off'", $commit + 1) -ge 0) {
    throw 'acl_off_commit_must_be_unique'
}

$offText = $text.Substring($offBoundary)
foreach ($forbidden in @(
    '$profile.authorized_workspace_roots',
    '$profile.read_only_roots',
    '$profile.denied_write_roots',
    '$profile.root_create_denied',
    'GetPathRoot($ProjectRoot)',
    '$driveRoots'
)) {
    if ($offText.Contains($forbidden)) {
        throw "acl_off_external_acl_scope_forbidden:$forbidden"
    }
}

$postconditionText = $postconditionFunction.Extent.Text
foreach ($required in @(
    'Get-Acl -LiteralPath $writableRoot',
    'acl_off_postcondition_controller_full_control_missing',
    '[IO.File]::WriteAllText($probeSource',
    'Move-Item -LiteralPath $probeSource',
    'Remove-Item -LiteralPath $probeSource,$probeDestination'
)) {
    if (-not $postconditionText.Contains($required)) {
        throw "acl_off_postcondition_requirement_missing:$required"
    }
}

foreach ($required in @(
    'Global\SmartAgentAclMode_',
    '$transitionMutex.WaitOne(0)',
    'acl_mode_transition_already_in_progress',
    '$transitionMutex.ReleaseMutex()',
    '$transitionMutex.Dispose()'
)) {
    if (-not $text.Contains($required)) {
        throw "acl_off_mutex_requirement_missing:$required"
    }
}

Invoke-Expression $postconditionFunction.Extent.Text
Invoke-Expression $ownedRootsFunction.Extent.Text
Invoke-Expression $safeInstallRootFunction.Extent.Text
Invoke-Expression $ancestorFunction.Extent.Text
$scopeTestRoot = Join-Path ([IO.Path]::GetTempPath()) 'smartagent-acl-scope-root'
$scopeSecurityRoot = Join-Path $scopeTestRoot 'localdata\secure\windows_security'
$scopeProfile = [pscustomobject]@{
    runtime_root = (Join-Path $scopeTestRoot 'localdata\runtime\security')
    executor_endpoint = (Join-Path $scopeTestRoot 'localdata\runtime\security\executor_queue')
    controller_state_root = (Join-Path $scopeSecurityRoot 'controller_state')
    executor_code_root = (Join-Path $scopeSecurityRoot 'executor_code')
    skill_roots = @((Join-Path $scopeSecurityRoot 'skills'))
    authorized_workspace_roots = @('E:\workspace')
    read_only_roots = @('C:\Users\Public')
    denied_write_roots = @('C:\Windows\Temp','C:\Users\SmartAgentExecutor')
    root_create_denied = @('C:\','E:\')
}
$scopeRoots = @(Get-AclOffOwnedRoots $scopeTestRoot $scopeSecurityRoot $scopeProfile $true)
if ($scopeRoots.Count -ne 2 -or
    -not ($scopeRoots -contains $scopeSecurityRoot) -or
    -not ($scopeRoots -contains $scopeProfile.runtime_root)) {
    throw "acl_off_owned_roots_unexpected:$($scopeRoots -join ';')"
}
$unboundScopeRoots = @(Get-AclOffOwnedRoots $scopeTestRoot $scopeSecurityRoot $scopeProfile $false)
if ($unboundScopeRoots.Count -ne 2 -or
    -not ($unboundScopeRoots -contains $scopeSecurityRoot) -or
    -not ($unboundScopeRoots -contains $scopeProfile.runtime_root)) {
    throw "acl_off_unbound_owned_roots_unexpected:$($unboundScopeRoots -join ';')"
}
foreach ($external in @('E:\workspace','C:\Users\Public','C:\Windows\Temp','C:\Users\SmartAgentExecutor','C:\','E:\')) {
    if ($scopeRoots -contains $external) {
        throw "acl_off_external_root_included:$external"
    }
}

foreach ($unsafeRoot in @('C:\','E:\','\\server\share\')) {
    $rejected = $false
    try { Assert-SafeInstallRoot $unsafeRoot } catch {
        $rejected = $_.Exception.Message -like 'acl_mode_install_root_must_not_be_drive_or_share_root:*'
    }
    if (-not $rejected) { throw "acl_off_unsafe_install_root_not_rejected:$unsafeRoot" }
}

$junctionTestBase = Join-Path ([IO.Path]::GetTempPath()) ("smartagent-acl-junction-" + [Guid]::NewGuid().ToString('N'))
$junctionInstall = Join-Path $junctionTestBase 'install'
$junctionExternal = Join-Path $junctionTestBase 'external'
$junctionParent = Join-Path $junctionInstall 'localdata'
$junctionPath = Join-Path $junctionParent 'runtime'
try {
    New-Item -ItemType Directory -Path (Join-Path $junctionExternal 'security'),$junctionParent -Force | Out-Null
    New-Item -ItemType Junction -Path $junctionPath -Target $junctionExternal -ErrorAction Stop | Out-Null
    $junctionRejected = $false
    try {
        Assert-ContainedPathHasNoReparseAncestor $junctionInstall (Join-Path $junctionPath 'security')
    } catch {
        $junctionRejected = $_.Exception.Message -like 'acl_mode_reparse_point_forbidden:*'
    }
    if (-not $junctionRejected) { throw 'acl_off_ancestor_junction_not_rejected' }
} finally {
    if (Test-Path -LiteralPath $junctionPath) { [IO.Directory]::Delete($junctionPath, $false) }
    if (Test-Path -LiteralPath $junctionTestBase) { Remove-Item -LiteralPath $junctionTestBase -Recurse -Force }
}

$testSecurityRoot = Join-Path ([IO.Path]::GetTempPath()) ("smartagent-acl-off-probe-" + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $testSecurityRoot -Force | Out-Null
    $testSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $testAcl = Get-Acl -LiteralPath $testSecurityRoot
    $testRule = [Security.AccessControl.FileSystemAccessRule]::new(
        $testSid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $testAcl.SetAccessRule($testRule)
    Set-Acl -LiteralPath $testSecurityRoot -AclObject $testAcl
    $SecurityRoot = $testSecurityRoot
    Assert-AclOffPostcondition $testSid.Value @($testSecurityRoot)
    if (@(Get-ChildItem -LiteralPath $testSecurityRoot -Force).Count -ne 0) {
        throw 'acl_off_postcondition_probe_cleanup_failed'
    }
} finally {
    if (Test-Path -LiteralPath $testSecurityRoot) {
        Remove-Item -LiteralPath $testSecurityRoot -Recurse -Force
    }
}

if ($offText.Contains('Start-ScheduledTask') -or $offText.Contains("Wait-ExecutorReady 'off'")) {
    throw 'acl_off_must_not_restart_or_wait_for_restricted_executor'
}

Write-Host 'ACL_OFF_TRANSACTION_VALIDATION_PASS'
