[CmdletBinding()]
param([string]$ProjectRoot = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if (-not $ProjectRoot) { $ProjectRoot = Join-Path $PSScriptRoot '..\..' }
$sourceRoot = [IO.Path]::GetFullPath($ProjectRoot)
$scriptPath = Join-Path $sourceRoot 'install_smart_agent\acl_status.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if (@($errors).Count) { throw "acl_status_parse_error:$($errors[0].Message)" }

foreach ($node in $ast.FindAll({
    param($value)
    $value -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)) {
    Invoke-Expression $node.Extent.Text
}
function Assert-Rejected([scriptblock]$Action, [string]$Expected) {
    try { & $Action | Out-Null }
    catch {
        if ($_.Exception.Message -like "*$Expected*") { return }
        throw
    }
    throw "expected_rejection:$Expected"
}

# OS identities and scheduled tasks are modeled. The scenario never invokes
# the production transition or changes any actual task, account or permission.
$script:FakeSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-21-100-200-300-1001')
$script:FakeName = 'SmartAgentExecutor'
$script:FakeEnabled = $true
$script:OtherTasks = @()
function Get-LocalUser {
    param($Name, $SID, $ErrorAction)
    if ($Name -and $Name -ne $script:FakeName) { return }
    if ($SID -and -not $SID.Equals($script:FakeSid)) { return }
    [pscustomobject]@{Name=$script:FakeName; SID=$script:FakeSid; Enabled=$script:FakeEnabled}
}
function Get-ScheduledTask { param($ErrorAction) $script:OtherTasks }
function Get-CimInstance { param($ClassName, $ErrorAction) }
function Get-LocalGroupMember { param($SID, $ErrorAction) }
function Read-Host { param($Prompt) 'CANCEL' }

$parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixture = Join-Path $parent ('smartagent-acl-rebind-' + [guid]::NewGuid().ToString('N'))
try {
    $ProjectRoot = Join-Path $fixture 'current'
    $foreign = Join-Path $fixture 'retired-package'
    $SecurityRoot = Join-Path $ProjectRoot 'localdata\secure\windows_security'
    $MachineRoot = Join-Path $fixture 'machine'
    $RevocationMarker = Join-Path $MachineRoot 'acl_revocation_pending.json'
    $TaskName = 'SmartAgent Restricted Executor'
    $policyRoot = Join-Path $fixture 'workspace'
    $foreignSecurity = Join-Path $foreign 'localdata\secure\windows_security'
    $profilePath = Join-Path $foreignSecurity 'security_profile.json'
    $code = Join-Path $foreignSecurity 'executor_code'
    New-Item -ItemType Directory -Path $SecurityRoot,$MachineRoot,$code,$policyRoot -Force | Out-Null
    $codeFile = Join-Path $code 'service.py'
    Set-Content -LiteralPath $codeFile -Value 'fixture'
    $machine = [pscustomobject]@{
        state='MISSING'; path=(Join-Path $MachineRoot 'machine_authorization.json'); value=$null
    }
    $task = [pscustomobject]@{
        TaskName=$TaskName; TaskPath='\'; State='Ready'
        Actions=@([pscustomobject]@{
            Execute=(Join-Path $foreign '.venv\Scripts\python.exe')
            WorkingDirectory=$code; Arguments='-m agent_core.restricted_executor_service'
        })
        Principal=[pscustomobject]@{UserId=$script:FakeSid.Value; LogonType='Password'; RunLevel='Limited'}
    }
    $plan = Get-ExecutorRevocation $machine $null $task
    if ($plan.root -ne $foreign -or $plan.profile_path -ne $profilePath -or
        $plan.sid -ne $script:FakeSid.Value -or -not $plan.legacy_scope_incomplete) {
        throw 'legacy_foreign_revocation_plan_incorrect'
    }
    Assert-Rejected { Confirm-LegacyExecutorRetirement $plan } 'acl_revocation_legacy_retirement_declined'
    $task.Actions[0].Arguments = '-m other.module'
    Assert-Rejected { Get-ExecutorRevocation $machine $null $task } 'acl_executor_task_ownership_unverified'
    $task.Actions[0].Arguments = '-m agent_core.restricted_executor_service'
    $journalPath = Join-Path $MachineRoot 'pending_acl_transaction.json'
    $journal = [ordered]@{
        schema='SMARTAGENT_PENDING_ACL_TRANSACTION_V1'
        executor_sid=$script:FakeSid.Value; executor_user=$script:FakeName
        managed_acl_roots=@($policyRoot); workspace_traverse_roots=@()
        acl_snapshots=@()
    }
    [IO.File]::WriteAllText($journalPath, ($journal | ConvertTo-Json))
    $plan = Get-ExecutorRevocation $machine $null $task
    if (-not $plan.legacy_scope_incomplete -or $plan.paths -contains $policyRoot) {
        throw 'rootless_journal_granted_revocation_scope'
    }
    $journal.install_root = $ProjectRoot
    [IO.File]::WriteAllText($journalPath, ($journal | ConvertTo-Json))
    Assert-Rejected { Get-ExecutorRevocation $machine $null $task } 'acl_revocation_pending_journal_owner_mismatch'
    $journal.install_root = $foreign
    [IO.File]::WriteAllText($journalPath, ($journal | ConvertTo-Json))
    $plan = Get-ExecutorRevocation $machine $null $task
    if ($plan.legacy_scope_incomplete -or $plan.paths -notcontains $policyRoot) {
        throw 'rooted_journal_scope_was_not_used'
    }
    Remove-Item -LiteralPath $journalPath

    $profile = [ordered]@{
        schema='SMARTAGENT_WINDOWS_SECURITY_V2'; executor_task_name=$TaskName
        executor_user=$script:FakeName; executor_sid=$script:FakeSid.Value
        executor_code_root=$code
        authorized_workspace_roots=@($policyRoot)
        python_acl_roots=@((Join-Path $foreign '.venv'))
    }
    [IO.File]::WriteAllText($profilePath, ($profile | ConvertTo-Json))
    $machine.state = 'FOREIGN'
    $machine.value = [pscustomobject]@{
        install_root=$foreign; security_profile=$profilePath
        executor_task_name=$TaskName; executor_sid=$script:FakeSid.Value
        executor_user=$script:FakeName
    }
    $plan = Get-ExecutorRevocation $machine $null $task
    if ($plan.legacy_scope_incomplete -or $plan.paths -notcontains $policyRoot -or
        $plan.paths -notcontains $codeFile) { throw 'recorded_revocation_scope_incomplete' }
    $machine.value.executor_sid = 'S-1-5-21-100-200-300-1002'
    Assert-Rejected { Get-ExecutorRevocation $machine $null $task } 'acl_revocation_machine_owner_mismatch'
    $machine.value.executor_sid = $script:FakeSid.Value

    $name = Get-InstanceExecutorName $ProjectRoot
    if ($name -notmatch '^SAExec_[a-f0-9]{12}$' -or (Get-InstanceExecutorName $foreign) -eq $name) {
        throw 'executor_name_not_installation_scoped'
    }
    if ((Get-ProvisioningExecutorName $null) -ne $name) { throw 'fresh_install_reused_legacy_account' }
    $script:FakeName = $name
    Assert-Rejected { Get-ProvisioningExecutorName $null } 'acl_executor_instance_account_unverified'
    $retired = [pscustomobject]@{retired_executor_user=$name; retired_executor_sid=$script:FakeSid.Value}
    $script:OtherTasks = @([pscustomobject]@{
        TaskName='OneDrive fixture'; Principal=[pscustomobject]@{UserId=$script:FakeSid.Value;LogonType='Interactive'}
    })
    if ((Get-ProvisioningExecutorName $retired) -ne $name) { throw 'retired_identity_not_reusable' }
    $script:OtherTasks[0].Principal.LogonType = 'Password'
    Assert-Rejected { Get-ProvisioningExecutorName $retired } 'acl_executor_instance_account_used_elsewhere'

    $text = Get-Content -LiteralPath $scriptPath -Raw
    $ready = $text.IndexOf("Wait-ExecutorReady 'on' (")
    $on = $text.IndexOf("Write-ModeState 'on'", $ready)
    $off = $text.LastIndexOf("Write-ModeState 'off'")
    $unregister = $text.IndexOf('Unregister-ScheduledTask')
    if ($ready -lt 0 -or $on -le $ready -or $unregister -lt 0 -or $off -le $unregister) {
        throw 'acl_publication_order_incorrect'
    }
} finally {
    $resolved = [IO.Path]::GetFullPath($fixture)
    if ((Split-Path -Parent $resolved).TrimEnd('\') -ieq $parent.TrimEnd('\') -and
        (Split-Path -Leaf $resolved) -match '^smartagent-acl-rebind-[0-9a-f]{32}$' -and
        (Test-Path -LiteralPath $resolved -PathType Container)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Write-Host 'ACL_REBIND_SECURITY_VALIDATION_PASS'
