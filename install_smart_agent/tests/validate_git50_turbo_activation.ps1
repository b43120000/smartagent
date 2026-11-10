# Git50 P2 real-Windows provision/revoke of a disposable *new* Turbo root.
# This is not a user E: or a pre-existing project. Never run against real data.
[CmdletBinding()]
param([string]$FixtureParent = $env:RUNNER_TEMP)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $FixtureParent) { $FixtureParent=[IO.Path]::GetTempPath() }
$fixture = Join-Path ([IO.Path]::GetFullPath($FixtureParent)) ('Git50ActivationIT-' + [Guid]::NewGuid().ToString('N'))
$project = Join-Path $fixture 'project'
$secure = Join-Path $project 'localdata\secure\windows_security'
$workspace = Join-Path $fixture 'workspace'
$root = Join-Path $workspace 'turbo_new'
$activation = Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
try {
    New-Item -ItemType Directory -Force -Path $secure,$workspace | Out-Null
    $mode=[ordered]@{
        schema='SMARTAGENT_ACL_MODE_V1'; mode='on'; install_root=$project
    }
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),
        ($mode | ConvertTo-Json), [Text.Encoding]::UTF8)
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=77
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($root)
        turbo_requested=$true
    }
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),
        ($policy | ConvertTo-Json), [Text.Encoding]::UTF8)
    if (Test-Path -LiteralPath $root) { throw 'GIT50_FIXTURE_NEW_ROOT_EXISTS' }

    & $activation -ProjectRoot $project -Action Activate -Workspace $workspace -TurboRoot $root
    $journalPath = Join-Path $secure 'turbo_os_activation.json'
    if (-not (Test-Path -LiteralPath $root -PathType Container) -or
        -not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
        throw 'GIT50_ACTIVE_ROOT_NOT_CREATED'
    }
    $journal = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
    if ($journal.status -ne 'ACTIVE_PROVISIONED' -or
        $journal.policy_revision -ne 77 -or
        [string]$journal.package_sid -notmatch '^S-1-15-2-') {
        throw 'GIT50_ACTIVATION_JOURNAL_INVALID'
    }
    $sid = [string]$journal.package_sid
    $rootSddl = (Get-Acl -LiteralPath $root).Sddl
    if ($rootSddl -notmatch [Regex]::Escape($sid)) {
        throw 'GIT50_ROOT_PACKAGE_SID_NOT_GRANTED'
    }
    # Check actual native command execution against the provisioned root.
    $marker = Join-Path $root 'actual_turbo_work.txt'
    $code = "[IO.File]::WriteAllText('actual_turbo_work.txt','GIT50_WORK');"
    $r = [SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        [string]$journal.moniker, $code, $root, [uint32]30000) | ConvertFrom-Json
    if (-not $r.sandbox_identity_verified -or
        $r.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
        [int]$r.exit_code -ne 0 -or
        [IO.File]::ReadAllText($marker) -cne 'GIT50_WORK') {
        throw 'GIT50_PROVISIONED_ROOT_CHILD_EXECUTION_FAILED'
    }
    Write-Host 'GIT50_NEW_ROOT_APP_CONTAINER_ACTIVATION_PASS'

    # Mode can transition during task cancellation. Revocation MUST still
    # work if the mode is already transitioning_off (not currently 'on').
    $mode.mode='transitioning_off'
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),
        ($mode | ConvertTo-Json), [Text.Encoding]::UTF8)
    & $activation -ProjectRoot $project -Action Revoke
    if (Test-Path -LiteralPath $journalPath) {
        throw 'GIT50_REVOKE_JOURNAL_STILL_PRESENT'
    }
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw 'GIT50_REVOKE_DELETED_USER_OUTPUT'
    }
    if ((Get-Acl -LiteralPath $root).Sddl -match [Regex]::Escape($sid) -or
        (Get-Acl -LiteralPath $marker).Sddl -match [Regex]::Escape($sid)) {
        throw 'GIT50_REVOKE_APP_CONTAINER_ACE_STILL_PRESENT'
    }
    Write-Host 'GIT50_NEW_ROOT_ACTIVATION_AND_REVOCATION_PASS'

    # The empty-root constraint fails safely without changing a populated tree.
    $populated=Join-Path $workspace 'existing'
    New-Item -ItemType Directory -Path $populated | Out-Null
    [IO.File]::WriteAllText((Join-Path $populated 'do_not_touch.txt'),'ORIGINAL')
    $policy.turbo_roots=@($populated)
    [IO.File]::WriteAllText((Join-Path $secure 'workspace_access_policy.json'),
        ($policy | ConvertTo-Json), [Text.Encoding]::UTF8)
    $mode.mode='on'
    [IO.File]::WriteAllText((Join-Path $secure 'acl_mode.json'),
        ($mode | ConvertTo-Json), [Text.Encoding]::UTF8)
    $rejected=$false
    try { & $activation -ProjectRoot $project -Action Activate -Workspace $workspace -TurboRoot $populated }
    catch {
        $rejected = $_.Exception.Message -match 'GIT50_ONLY_BRAND_NEW'
    }
    if (-not $rejected -or
        [IO.File]::ReadAllText((Join-Path $populated 'do_not_touch.txt')) -cne 'ORIGINAL') {
        throw 'GIT50_EXISTING_ROOT_PRECONDITION_DID_NOT_FAIL_CLOSED'
    }
    Write-Host 'GIT50_EXISTING_TREE_NO_MODIFICATION_PASS'
}
finally {
    if (Test-Path -LiteralPath $project) {
        $journalPath=Join-Path $project 'localdata\secure\windows_security\turbo_os_activation.json'
        if (Test-Path -LiteralPath $journalPath) {
            try { & $activation -ProjectRoot $project -Action Revoke } catch {
                Write-Warning 'GIT50_ACTIVATION_FIXTURE_REVOKE_NEEDS_INSPECTION'
            }
        }
    }
    # Clean fixture only after verifying it is still inside the unique
    # disposable test parent and the journal was safely revoked.
    if ((Test-Path -LiteralPath $fixture) -and
        -not (Test-Path -LiteralPath $journalPath)) {
        Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction Stop
    }
}
