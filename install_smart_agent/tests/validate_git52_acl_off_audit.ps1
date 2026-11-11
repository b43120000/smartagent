# CI only. Uses a unique disposable fixture, never touches real SmartAgent ACL.
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$audit = Join-Path $ProjectRoot 'install_smart_agent\verify_acl_off.ps1'
$testRoot = Join-Path $env:RUNNER_TEMP ('git52-acl-off-' + [Guid]::NewGuid().ToString('N'))
$install = Join-Path $testRoot 'install'
$machine = Join-Path $testRoot 'machine'
$sec = Join-Path $install 'localdata\secure\windows_security'
$modePath = Join-Path $sec 'acl_mode.json'
$authPath = Join-Path $machine 'machine_authorization.json'
$pendingPath = Join-Path $sec 'executor_install_pending.json'
$managed = Join-Path $testRoot 'managed'
$script:cases = 0

function Save-Json([string]$Path, $Data) {
    [IO.File]::WriteAllText($Path, ($Data | ConvertTo-Json -Depth 10),
                           (New-Object Text.UTF8Encoding($false)))
}
function Verify-Report([string]$ExpectedStatus, [int]$ExpectedExit, [string]$Name,
                       [string]$ExpectedCheck, [string]$ExpectedCheckStatus,
                       [string]$Sid = '', [switch]$Recurse) {
    $params = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', $audit, '-ProjectRoot', $install,
                '-TestFixture', '-FixtureMachineRoot', $machine)
    if ($Sid) { $params += @('-ExpectedExecutorSid', $Sid) }
    if ($Recurse) { $params += @('-ManagedPaths', $managed, '-RecurseManagedPaths') }
    $output = & powershell.exe @params
    $code = $LASTEXITCODE
    if ($code -ne $ExpectedExit) {
        throw ('CI EXIT MISMATCH (' + $Name + '): ' + $code + '; ' + ($output | Out-String))
    }
    $result = ($output | Out-String) | ConvertFrom-Json -ErrorAction Stop
    if ($result.status -ne $ExpectedStatus -or $result.full_os_verification -ne $false -or
        $result.fixture_only -ne $true) {
        throw ('CI STATUS MISMATCH (' + $Name + '): ' + ($result | ConvertTo-Json -Depth 6))
    }
    $match = @($result.checks | Where-Object {
        $_.name -eq $ExpectedCheck -and $_.status -eq $ExpectedCheckStatus
    })
    if ($match.Count -lt 1) {
        throw ('CI CHECK MISMATCH (' + $Name + '): ' + ($result | ConvertTo-Json -Depth 6))
    }
    $script:cases += 1
    Write-Host ('PASS: ' + $Name)
}
try {
    New-Item -ItemType Directory -Force -Path $sec,$machine,$managed | Out-Null
    $mode = @{
        schema = 'SMARTAGENT_ACL_MODE_V1'
        mode = 'off'
        install_root = $install
    }
    $policy = @{
        schema = 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision = 1
        writable_workspaces = @($managed)
        read_only_roots = @()
        denied_write_roots = @()
        turbo_roots = @()
        turbo_requested = $false
    }
    Save-Json $modePath $mode
    Save-Json (Join-Path $sec 'workspace_access_policy.json') $policy

    Verify-Report 'INCOMPLETE' 2 'Committed OFF is not full proof' 'acl_mode' 'PASS'
    $mode.mode = 'transitioning_on'
    Save-Json $modePath $mode
    Verify-Report 'FAIL' 1 'Interrupted ON must fail closed' 'acl_mode' 'FAIL'
    $mode.mode = 'off'
    $mode.install_root = 'D:\other\installation'
    Save-Json $modePath $mode
    Verify-Report 'FAIL' 1 'Cross-install mode identity must fail' 'acl_mode' 'FAIL'
    $mode.install_root = $install
    Save-Json $modePath $mode

    Save-Json $pendingPath @{
        schema='SMARTAGENT_EXECUTOR_INSTALL_PENDING_V1'; install_root=$install
    }
    Verify-Report 'FAIL' 1 'Pending executor journal must fail' 'executor_install_pending' 'FAIL'
    Remove-Item -LiteralPath $pendingPath -Force

    Save-Json $authPath @{
        schema = 'SMARTAGENT_MACHINE_AUTHORIZATION_V1'; install_root=$install
    }
    Verify-Report 'FAIL' 1 'Bound machine authorization must fail' 'machine_authorization' 'FAIL'
    Save-Json $authPath @{
        schema = 'SMARTAGENT_MACHINE_AUTHORIZATION_V1'; install_root='D:\other\installation'
    }
    Verify-Report 'INCOMPLETE' 2 'Other-install authorization is not ours' 'machine_authorization' 'INFO'
    Remove-Item -LiteralPath $authPath -Force

    $policy.turbo_requested = $true
    Save-Json (Join-Path $sec 'workspace_access_policy.json') $policy
    Verify-Report 'FAIL' 1 'Turbo policy cannot survive OFF' 'turbo_policy' 'FAIL'
    $policy.turbo_requested = $false
    Save-Json (Join-Path $sec 'workspace_access_policy.json') $policy

    # The live kernel ACL, not model text, must reveal a matching user ACE.
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $managed /grant ('*' + $sid + ':(RX)') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'CI_ICACLS_GRANT_FAILED' }
    Verify-Report 'FAIL' 1 'Native Windows executor SID ACE is detected' 'executor_ace' 'FAIL' -Sid $sid -Recurse

    Write-Host ('GIT52_ACL_OFF_AUDIT_FIXTURE_TESTS_PASS count=' + $script:cases)
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction Stop
    }
}
