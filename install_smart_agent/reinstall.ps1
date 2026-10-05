[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [switch]$NonInteractive,
    [switch]$ConfirmReset
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath($SourceRoot).TrimEnd("\")
$Desktop = [Environment]::GetFolderPath("Desktop")
$ControlRoot = [IO.Path]::GetFullPath((Join-Path $Desktop "SmartAgent"))
$BrowserProfileRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA "WebLLMScraper"))
$MachineAuthorizationRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'SmartAgent'))
$MachineAuthorizationPath = Join-Path $MachineAuthorizationRoot 'machine_authorization.json'
$PendingAclJournalPath = Join-Path $MachineAuthorizationRoot 'pending_acl_transaction.json'
$DefaultExecutorUser = 'SmartAgentExecutor'
$DefaultTaskName = 'SmartAgent Restricted Executor'

foreach ($relative in @('install_smart_agent.bat','source','install_smart_agent','config','localdata')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $relative))) {
        throw "Refusing to reset an invalid SmartAgent root; missing: $relative"
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'SmartAgent full reset requires an elevated administrator window.'
}

Write-Host "[SmartAgent Reinstall] Application root: $ProjectRoot" -ForegroundColor Cyan
Write-Host "[SmartAgent Reinstall] This removes computer-wide executor authorization." -ForegroundColor Yellow
Write-Host "[SmartAgent Reinstall] Telegram pairing, Workspace files, bindings, and browser profile are preserved." -ForegroundColor Green
Write-Host "[SmartAgent Reinstall] Browser profile: $BrowserProfileRoot" -ForegroundColor Green
if (-not $ConfirmReset) {
    if ($NonInteractive) { throw 'ConfirmReset is required for non-interactive full reset.' }
    $answer = Read-Host 'Type RESET to revoke all Windows restricted-executor authorization'
    if ($answer -cne 'RESET') { throw 'Reset cancelled.' }
}

$forceStop = Join-Path $ProjectRoot "force_stop_all_agents.bat"
if ((Test-Path -LiteralPath $forceStop -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -PathType Leaf)) {
    & $forceStop --no-pause
    Start-Sleep -Milliseconds 750
}
$currentPid = $PID
$escapedRoot = [Regex]::Escape($ProjectRoot)
$targets = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $currentPid -and (
        [string]$_.CommandLine -match $escapedRoot -or
        [string]$_.ExecutablePath -match $escapedRoot
    )
})
foreach ($process in $targets) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
if ($targets.Count -gt 0) { Start-Sleep -Milliseconds 1000 }

function Test-EndsWithPath([string]$Path, [string]$Suffix) {
    return [IO.Path]::GetFullPath($Path).TrimEnd('\').EndsWith(
        $Suffix.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase
    )
}

function Remove-ManagedSecurityTree([string]$Path, [string]$ExpectedSuffix) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not (Test-EndsWithPath $full $ExpectedSuffix)) {
        throw "Refusing to remove unexpected managed security path: $full"
    }
    if (-not (Test-Path -LiteralPath $full)) { return }
    $item = Get-Item -LiteralPath $full -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to recursively remove a managed security reparse point: $full"
    }
    Remove-Item -LiteralPath $full -Recurse -Force
    Write-Host "[SmartAgent Reinstall] Removed managed security tree: $full" -ForegroundColor Green
}

function Remove-ExecutorAce([string]$Path, [string[]]$Identities) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) { return }
    foreach ($executorIdentity in @($Identities | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)) {
        & icacls.exe $Path /remove:g $executorIdentity | Out-Null
        & icacls.exe $Path /remove:d $executorIdentity | Out-Null
    }
}

$machineAuthorization = $null
if (Test-Path -LiteralPath $MachineAuthorizationPath -PathType Leaf) {
    try {
        $machineAuthorization = Get-Content -LiteralPath $MachineAuthorizationPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($machineAuthorization.schema -ne 'SMARTAGENT_MACHINE_AUTHORIZATION_V1') { throw 'schema' }
    } catch {
        Write-Warning "Machine authorization record is unreadable; clearing known authorization objects: $MachineAuthorizationPath"
        $machineAuthorization = $null
    }
}
$pendingAclJournal = $null
if (Test-Path -LiteralPath $PendingAclJournalPath -PathType Leaf) {
    try {
        $pendingAclJournal = Get-Content -LiteralPath $PendingAclJournalPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($pendingAclJournal.schema -ne 'SMARTAGENT_PENDING_ACL_TRANSACTION_V1') { throw 'schema' }
    } catch {
        Write-Warning "Pending ACL journal is unreadable and will not be trusted for path cleanup: $PendingAclJournalPath"
        $pendingAclJournal = $null
    }
}

$profilePaths = @((Join-Path $ProjectRoot 'localdata\secure\windows_security\security_profile.json'))
if ($machineAuthorization -and -not [string]::IsNullOrWhiteSpace([string]$machineAuthorization.security_profile)) {
    $profilePaths += [IO.Path]::GetFullPath([string]$machineAuthorization.security_profile)
}
$profiles = @()
foreach ($profilePath in @($profilePaths | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $profilePath -PathType Leaf) {
        try {
            $profile = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($profile.schema -notin @('SMARTAGENT_WINDOWS_SECURITY_V1','SMARTAGENT_WINDOWS_SECURITY_V2')) { throw 'schema' }
            $profiles += [pscustomobject]@{ Path = $profilePath; Value = $profile }
        } catch {
            Write-Warning "Security profile is unreadable; clearing its managed directory without using untrusted ACL paths: $profilePath"
        }
    }
}

$taskNames = @($DefaultTaskName)
$executorUsers = @($DefaultExecutorUser)
$executorSids = @()
if ($machineAuthorization) {
    $taskNames += [string]$machineAuthorization.executor_task_name
    $executorUsers += [string]$machineAuthorization.executor_user
    $executorSids += [string]$machineAuthorization.executor_sid
}
if ($pendingAclJournal) {
    $executorUsers += [string]$pendingAclJournal.executor_user
    $executorSids += [string]$pendingAclJournal.executor_sid
}
foreach ($entry in $profiles) {
    $taskNames += [string]$entry.Value.executor_task_name
    $executorUsers += [string]$entry.Value.executor_user
}
$taskNames = @($taskNames | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
$executorUsers = @($executorUsers | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)

foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "[SmartAgent Reinstall] Removed scheduled task: $taskName" -ForegroundColor Green
    }
}

$resolvedIdentities = @()
foreach ($executorUser in $executorUsers) {
    $localUser = Get-LocalUser -Name $executorUser -ErrorAction SilentlyContinue
    if ($localUser) {
        $executorSids += [string]$localUser.SID
        $resolvedIdentities += "$env:COMPUTERNAME\$executorUser"
    }
}
$resolvedIdentities += @($executorSids | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { "*$_" })

if ($pendingAclJournal) {
    foreach ($root in @($pendingAclJournal.workspace_traverse_roots | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique)) {
        Remove-ExecutorAce ([string]$root) $resolvedIdentities
    }
}
Remove-Item -LiteralPath $PendingAclJournalPath -Force -ErrorAction SilentlyContinue

foreach ($entry in $profiles) {
    $profile = $entry.Value
    $aclRoots = @($profile.authorized_workspace_roots) + @($profile.workspace_traverse_roots) + @($profile.read_only_roots) +
        @($profile.denied_write_roots) + @($profile.root_create_denied) +
        @($profile.runtime_root,$profile.executor_endpoint,$profile.controller_state_root,$profile.executor_code_root) +
        @($profile.skill_roots)
    foreach ($root in @($aclRoots | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique)) {
        Remove-ExecutorAce ([string]$root) $resolvedIdentities
    }
}

foreach ($executorUser in $executorUsers) {
    if (Get-LocalUser -Name $executorUser -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $executorUser
        Write-Host "[SmartAgent Reinstall] Removed local executor account: $executorUser" -ForegroundColor Green
    }
}

$managedTrees = @()
foreach ($entry in $profiles) {
    $managedTrees += [pscustomobject]@{ Path = (Split-Path -Parent $entry.Path); Suffix = 'localdata\secure\windows_security' }
    $managedTrees += [pscustomobject]@{ Path = [string]$entry.Value.runtime_root; Suffix = 'localdata\runtime\security' }
}
$managedTrees += [pscustomobject]@{ Path = (Join-Path $ProjectRoot 'localdata\secure\windows_security'); Suffix = 'localdata\secure\windows_security' }
$managedTrees += [pscustomobject]@{ Path = (Join-Path $ProjectRoot 'localdata\runtime\security'); Suffix = 'localdata\runtime\security' }
foreach ($tree in @($managedTrees | Sort-Object Path -Unique)) {
    Remove-ManagedSecurityTree ([string]$tree.Path) ([string]$tree.Suffix)
}

Remove-Item -LiteralPath $MachineAuthorizationPath -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $MachineAuthorizationRoot -PathType Container) {
    $remainingMachineFiles = @(Get-ChildItem -LiteralPath $MachineAuthorizationRoot -Force)
    if ($remainingMachineFiles.Count -eq 0) { Remove-Item -LiteralPath $MachineAuthorizationRoot -Force }
}
Write-Host "[SmartAgent Reinstall] Cleared machine authorization pointer." -ForegroundColor Green

$venv = Join-Path $ProjectRoot '.venv'
if (Test-Path -LiteralPath $venv -PathType Container) {
    $venvItem = Get-Item -LiteralPath $venv -Force
    if ($venvItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to recursively remove venv reparse point: $venv"
    }
    Remove-Item -LiteralPath $venv -Recurse -Force
}

foreach ($name in @('runtime','cache','logs','temp')) {
    $directory = Join-Path $ProjectRoot ("localdata\" + $name)
    if (Test-Path -LiteralPath $directory -PathType Container) {
        Get-ChildItem -LiteralPath $directory -Force | Remove-Item -Recurse -Force
    } else {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
}

$metadataRoot = Join-Path $ProjectRoot 'localdata\metadata'
New-Item -ItemType Directory -Force -Path $metadataRoot | Out-Null
foreach ($name in @('bootstrap_state.json','provisioning_options.json','provisioning_state.json','milestone-state.json','install-state.json')) {
    Remove-Item -LiteralPath (Join-Path $metadataRoot $name) -Force -ErrorAction SilentlyContinue
}

$managedControls = @('install_smart_agent.bat','ACLstatus.bat','update.bat','adapterUI.bat','reinstall_smart_agent.bat','force_stop_all_agents.bat','Edit_workspace.bat','InstallCheckList.bat','launch_remote_agent.bat','launch_webcopilot_chatgpt.bat')
foreach ($name in $managedControls) {
    $path = Join-Path $ControlRoot $name
    if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path -Force }
}
if (Test-Path -LiteralPath $ControlRoot -PathType Container) {
    $remaining = @(Get-ChildItem -LiteralPath $ControlRoot -Force)
    if ($remaining.Count -eq 0) { Remove-Item -LiteralPath $ControlRoot -Force }
}

Write-Host "SMARTAGENT_FULL_AUTHORIZATION_RESET_PASS" -ForegroundColor Green
exit 0
