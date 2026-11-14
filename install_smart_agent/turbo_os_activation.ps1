# Git50 P2. Elevated operator-managed activation for a brand-new Turbo root.
# This is a deliberate, limited-scope OS isolation lifecycle. It never grants
# rights to an existing populated tree and never enables Turbo from task JSON.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Activate','Revoke','Status')][string]$Action,
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [string]$Workspace = '',
    [string]$TurboRoot = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$securityRoot = Join-Path $ProjectRoot 'localdata\secure\windows_security'
$modePath = Join-Path $securityRoot 'acl_mode.json'
$policyPath = Join-Path $securityRoot 'workspace_access_policy.json'
$legacyJournal = Join-Path $securityRoot 'turbo_os_activation.json'
$journalPath = $legacyJournal
. (Join-Path $PSScriptRoot 'turbo_multi_root_paths.ps1')
if ($TurboRoot) {
    $candidate=Get-TurboRootJournalPaths $securityRoot $TurboRoot
    if(Test-Path -LiteralPath $candidate.activation -PathType Leaf){
        $journalPath=$candidate.activation
    } elseif($Action -eq 'Activate') {
        $policyPreview=Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if(@($policyPreview.turbo_roots).Count -gt 1){
            $journalPath=$candidate.activation
        }
    }
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw 'GIT50_PROJECT_ROOT_NOT_FOUND'
}
if ($Action -eq 'Status') {
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
        Write-Output '{"schema":"SMARTAGENT_TURBO_OS_ACTIVATION_V1","status":"NOT_CONFIGURED","authorization_granted":false}'
        return
    }
    $status = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($status.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1') {
        throw 'GIT50_ACTIVATION_JOURNAL_SCHEMA_INVALID'
    }
    Write-Output ([ordered]@{
        schema='SMARTAGENT_TURBO_OS_ACTIVATION_STATUS_V1'
        status=[string]$status.status
        turbo_root=[string]$status.turbo_root
        policy_revision=[int]$status.policy_revision
        authorization_granted=$false
        independent_os_attestation_required=$true
    } | ConvertTo-Json -Compress)
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'GIT50_ACTIVATION_REQUIRES_ELEVATED_ADMINISTRATOR'
}
# The same named mutex must eventually protect *every* production broker
# execution and ACLstatus OFF revocation. Until that wiring is complete
# this script provisions a root, but the production execution gate stays OFF.
$hash = [Security.Cryptography.SHA256]::Create()
try {
    $digest = [BitConverter]::ToString(
        $hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToUpperInvariant()))
    ).Replace('-','')
} finally { $hash.Dispose() }
$mutexName = 'Global\SmartAgentTurbo-' + $digest.Substring(0,32)
$mutex = New-Object Threading.Mutex($false, $mutexName)
$held = $false
try {
    try {
        $held = $mutex.WaitOne([TimeSpan]::FromSeconds(320))
    } catch [Threading.AbandonedMutexException] {
        $held = $true
    }
    if (-not $held) { throw 'GIT50_BROKER_MUTEX_TIMEOUT' }

    function Load-Json([string]$Path) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw 'GIT50_REQUIRED_STATE_MISSING'
        }
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop)
    }
    function Assert-Not-Reparse([string]$Path) {
        if (-not (Test-Path -LiteralPath $Path)) { return }
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'GIT50_REPARSE_COMPONENT_FORBIDDEN'
        }
    }
    function Write-Journal($Payload) {
        $temp = $journalPath + '.tmp'
        try {
            [IO.File]::WriteAllText($temp, ($Payload | ConvertTo-Json -Depth 12),
                (New-Object Text.UTF8Encoding($false)))
            & icacls.exe $temp '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'GIT50_JOURNAL_PROTECTION_FAILED' }
            Move-Item -LiteralPath $temp -Destination $journalPath -Force -ErrorAction Stop
        } finally {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
    }
    function Invoke-Verified-Launch($Moniker, $Script, $Root) {
        $raw = [SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
            $Moniker, $Script, $Root, [uint32]30000)
        $response = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($response.sandbox_identity_verified -ne $true -or
            $response.sandbox_backend -ne 'WIN32_APPCONTAINER' -or
            $response.child_process_policy -ne 'JOB_KILL_ON_CLOSE' -or
            $response.os_process_started -ne $true -or
            [int]$response.exit_code -ne 0) {
            throw 'GIT50_OS_TOKEN_OR_EXECUTION_NOT_ATTESTED'
        }
    }
    function Revoke-Recorded-Root($Journal) {
        if ($Journal.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1') {
            throw 'GIT50_REVOKE_JOURNAL_INVALID'
        }
        $root = [IO.Path]::GetFullPath([string]$Journal.turbo_root).TrimEnd('\')
        $ws = [IO.Path]::GetFullPath([string]$Journal.workspace_root).TrimEnd('\')
        if (-not $root.StartsWith($ws + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'GIT50_REVOKE_ROOT_OUTSIDE_RECORDED_WORKSPACE'
        }
        $sidString = [string]$Journal.package_sid
        if ($sidString -notmatch '^S-1-15-2-') {
            throw 'GIT50_REVOKE_PACKAGE_SID_INVALID'
        }
        $Journal.status = 'REVOKING'
        Write-Journal $Journal
        if (Test-Path -LiteralPath $root -PathType Container) {
            Assert-Not-Reparse $root
            $entries = @(Get-Item -LiteralPath $root -Force) +
                       @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)
            foreach ($entry in $entries) {
                if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw 'GIT50_REVOKE_REPARSE_PATH_BLOCKED'
                }
            }
            & icacls.exe $root '/remove:g' ('*' + $sidString) '/T' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'GIT50_REVOKE_PACKAGE_ACE_FAILED' }
            & icacls.exe $root '/setintegritylevel' '(OI)(CI)M' '/T' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'GIT50_REVOKE_MEDIUM_IL_FAILED' }
            foreach ($entry in $entries) {
                $sddl = (Get-Acl -LiteralPath $entry.FullName -ErrorAction Stop).Sddl
                if ($sddl -match [Regex]::Escape($sidString)) {
                    throw 'GIT50_REVOKE_PACKAGE_ACE_REMAINING'
                }
            }
        }
        foreach ($ancestor in @($Journal.traverse_parents)) {
            $ancestorText = [IO.Path]::GetFullPath([string]$ancestor)
            if ($ancestorText -ne $ws -and
                -not $ancestorText.StartsWith($ws + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'GIT50_REVOKE_PARENT_OUTSIDE_WORKSPACE'
            }
            if (Test-Path -LiteralPath $ancestorText -PathType Container) {
                Assert-Not-Reparse $ancestorText
                & icacls.exe $ancestorText '/remove:g' ('*' + $sidString) | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'GIT50_REVOKE_TRAVERSE_ACE_FAILED' }
            }
        }
        try { [SmartAgentGit47AppContainerProbe]::DeleteProfile([string]$Journal.moniker) }
        catch { throw 'GIT50_REVOKE_APPCONTAINER_PROFILE_FAILED' }
        if ($Journal.PSObject.Properties.Name -contains 'negative_sentinel_path') {
            $sentinel = [IO.Path]::GetFullPath([string]$Journal.negative_sentinel_path)
            $expectedParent = [IO.Path]::GetDirectoryName($root)
            if ([IO.Path]::GetDirectoryName($sentinel) -ne $expectedParent -or
                -not [IO.Path]::GetFileName($sentinel).StartsWith('.git50-os-denial-')) {
                throw 'GIT50_REVOKE_SENTINEL_PATH_UNVERIFIED'
            }
            # Never delete a file whose original marker was changed by
            # an independent user process.
            if (Test-Path -LiteralPath $sentinel -PathType Leaf) {
                if ([IO.File]::ReadAllText($sentinel) -ceq 'OUTSIDE_SENTINEL') {
                    Remove-Item -LiteralPath $sentinel -Force -ErrorAction Stop
                }
            }
        }
        Remove-Item -LiteralPath $journalPath -Force -ErrorAction Stop
    }

    if (-not ('SmartAgentGit47AppContainerProbe' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'turbo_appcontainer_probe.cs') -ErrorAction Stop
    }
    if ($Action -eq 'Revoke') {
        if (-not $TurboRoot) {
            # ACL OFF must revoke EVERY independently enrolled AppContainer,
            # even when a single legacy journal still exists.
            $folder=Join-Path $securityRoot 'turbo_root_activations'
            if(Test-Path -LiteralPath $folder -PathType Container){
                foreach($entry in @(Get-ChildItem -LiteralPath $folder -File -Filter 'activation-*.json' -ErrorAction Stop)){
                    if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){
                        throw 'GIT62_REVOKE_JOURNAL_REPARSE'
                    }
                    $multiRecord=Load-Json $entry.FullName
                    if($multiRecord.schema -ne 'SMARTAGENT_TURBO_OS_ACTIVATION_V1' -or
                       [string]::IsNullOrWhiteSpace([string]$multiRecord.turbo_root)){
                        throw 'GIT62_REVOKE_UNKNOWN_ENROLLMENT'
                    }
                    $expected=Get-TurboRootJournalPaths $securityRoot ([string]$multiRecord.turbo_root)
                    if(-not $expected.activation.Equals($entry.FullName,[StringComparison]::OrdinalIgnoreCase)){
                        throw 'GIT62_REVOKE_JOURNAL_KEY_MISMATCH'
                    }
                    & $PSCommandPath -Action Revoke -ProjectRoot $ProjectRoot -TurboRoot ([string]$multiRecord.turbo_root)
                    if(-not $?){throw 'GIT62_REVOKE_ROOT_FAILED'}
                }
            }
            $journalPath=$legacyJournal
        }
        if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
            Write-Output 'GIT50_TURBO_ROOT_NOT_ACTIVE'
            return
        }
        $record = Load-Json $journalPath
        if ($record.PSObject.Properties.Name -contains 'existing_root_import' -and
            $record.existing_root_import -eq $true) {
            # Existing populated roots have immutable per-file SDDL/IL
            # snapshots and MUST use the P3-D3 exact restoration lifecycle.
            # The older native root revoker resets arbitrary files to Medium
            # and may irreversibly destroy original integrity labels.
            $recovery=Join-Path $PSScriptRoot 'turbo_existing_root_import.ps1'
            if (-not (Test-Path -LiteralPath $recovery -PathType Leaf)) {
                throw 'TURBO_D3_REVOKER_UNAVAILABLE_FAIL_CLOSED'
            }
            & $recovery -Action Revoke -ProjectRoot $ProjectRoot -TurboRoot ([string]$record.turbo_root)
            if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
                throw 'TURBO_D3_REVOKE_ACTIVATION_STILL_PRESENT'
            }
            Write-Output 'TURBO_D3_EXISTING_ROOT_REVOKED_AND_RESTORED'
            return
        }
        Revoke-Recorded-Root $record
        Write-Output 'GIT50_TURBO_ROOT_REVOKED'
        return
    }

    if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
        throw 'GIT50_PREVIOUS_TURBO_ACTIVATION_MUST_BE_REVOKED_FIRST'
    }
    if($journalPath -ne $legacyJournal){
        if(Test-Path -LiteralPath $legacyJournal -PathType Leaf){
            throw 'GIT62_LEGACY_SINGLE_BROKER_REQUIRES_VERIFIED_MIGRATION'
        }
        Assert-TurboRootJournalDirectory $securityRoot
    }
    $mode = Load-Json $modePath
    $policy = Load-Json $policyPath
    if ($mode.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or
        $mode.mode -ne 'on' -or
        -not [IO.Path]::GetFullPath([string]$mode.install_root).TrimEnd('\').Equals(
            $ProjectRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_ACL_ON_INSTALLATION_NOT_VERIFIED'
    }
    if ($policy.schema -ne 'SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1' -or
        $policy.turbo_requested -ne $true -or
        [int]$policy.revision -lt 1) {
        throw 'GIT50_TURBO_POLICY_NOT_REQUESTED'
    }
    $ws = [IO.Path]::GetFullPath($Workspace).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($TurboRoot).TrimEnd('\')
    $writable = @($policy.writable_workspaces | ForEach-Object { [IO.Path]::GetFullPath([string]$_).TrimEnd('\') })
    $configured = @($policy.turbo_roots | ForEach-Object { [IO.Path]::GetFullPath([string]$_).TrimEnd('\') })
    if (@($configured | Where-Object {
            $_.Equals($root,[StringComparison]::OrdinalIgnoreCase)
        }).Count -ne 1 -or
        -not @($writable | Where-Object { $_.Equals($ws,[StringComparison]::OrdinalIgnoreCase) }).Count -or
        -not $root.StartsWith($ws + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $root.StartsWith($ProjectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_TURBO_ROOT_POLICY_SCOPE_MISMATCH'
    }
    foreach ($protected in @($policy.read_only_roots) + @($policy.denied_write_roots)) {
        if ([string]::IsNullOrWhiteSpace([string]$protected)) { continue }
        $forbidden = [IO.Path]::GetFullPath([string]$protected).TrimEnd('\')
        if ($protected -in @($policy.read_only_roots) -and $protected -notin @($policy.denied_write_roots) -and ($ws.StartsWith($forbidden + '\',[StringComparison]::OrdinalIgnoreCase))) { continue }
        if ($root.Equals($forbidden,[StringComparison]::OrdinalIgnoreCase) -or
            $root.StartsWith($forbidden + '\',[StringComparison]::OrdinalIgnoreCase) -or
            $forbidden.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) {
            throw 'GIT50_TURBO_ROOT_PROTECTED_OVERLAP'
        }
    }
    if (Test-Path -LiteralPath $root) {
        # Existing trees need protected-child/hardlink/reparse preservation.
        # Do not mutate them through the empty-root safe activation profile.
        throw 'GIT50_ONLY_BRAND_NEW_NONEXISTENT_TURBO_ROOT_SUPPORTED'
    }
    $parent = [IO.Path]::GetDirectoryName($root)
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'GIT50_TURBO_PARENT_MISSING'
    }
    if (([IO.DriveInfo]::new([IO.Path]::GetPathRoot($parent))).DriveFormat -ne 'NTFS') {
        throw 'GIT50_TURBO_REQUIRES_NTFS'
    }
    $traverse = @()
    $cur = $parent
    while ($cur.StartsWith($ws + '\',[StringComparison]::OrdinalIgnoreCase) -or
           $cur.Equals($ws,[StringComparison]::OrdinalIgnoreCase)) {
        Assert-Not-Reparse $cur
        $traverse += $cur
        if ($cur.Equals($ws,[StringComparison]::OrdinalIgnoreCase)) { break }
        $cur = [IO.Path]::GetDirectoryName($cur)
    }
    if (-not $cur.Equals($ws,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'GIT50_WORKSPACE_PARENT_TRAVERSE_NOT_BOUND'
    }

    $moniker = 'SmartAgentTurbo_' + [Guid]::NewGuid().ToString('N')
    $sidText = [SmartAgentGit47AppContainerProbe]::CreateProfile($moniker)
    if ($sidText -notmatch '^S-1-15-2-') {
        throw 'GIT50_NEW_APPCONTAINER_SID_INVALID'
    }
    $record = [ordered]@{
        schema='SMARTAGENT_TURBO_OS_ACTIVATION_V1'
        status='ACTIVATING'
        install_root=$ProjectRoot
        workspace_root=$ws
        turbo_root=$root
        policy_revision=[int]$policy.revision
        moniker=$moniker
        package_sid=$sidText
        traverse_parents=$traverse
        negative_sentinel_path=(Join-Path $parent ('.git50-os-denial-' + [Guid]::NewGuid().ToString('N')))
        created_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        authorization_granted=$false
    }
    Write-Journal $record
    try {
        $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
        $none = [Security.AccessControl.InheritanceFlags]::None
        $propNone = [Security.AccessControl.PropagationFlags]::None
        $inherited = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
        foreach($ancestor in $traverse) {
            $acl = Get-Acl -LiteralPath $ancestor
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,[Security.AccessControl.FileSystemRights]::Traverse,
                $none,$propNone,[Security.AccessControl.AccessControlType]::Allow)
            [void]$acl.AddAccessRule($rule)
            Set-Acl -LiteralPath $ancestor -AclObject $acl -ErrorAction Stop
        }
        New-Item -ItemType Directory -Path $root -ErrorAction Stop | Out-Null
        Assert-Not-Reparse $root
        $acl = Get-Acl -LiteralPath $root
        $rootRights = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
                      [Security.AccessControl.FileSystemRights]::WriteData -bor
                      [Security.AccessControl.FileSystemRights]::AppendData -bor
                      [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
                      [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
                      [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
        $rootAce = New-Object Security.AccessControl.FileSystemAccessRule(
            $sid,$rootRights,$none,$propNone,
            [Security.AccessControl.AccessControlType]::Allow)
        $childAce = New-Object Security.AccessControl.FileSystemAccessRule(
            $sid,[Security.AccessControl.FileSystemRights]::Modify,
            $inherited,[Security.AccessControl.PropagationFlags]::InheritOnly,
            [Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rootAce)
        [void]$acl.AddAccessRule($childAce)
        Set-Acl -LiteralPath $root -AclObject $acl -ErrorAction Stop
        & icacls.exe $root '/setintegritylevel' '(OI)(CI)L' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'GIT50_ROOT_LOW_IL_SETUP_FAILED' }

        # A genuine Windows kernel preflight: fixed broker-authored scripts,
        # not model-supplied evidence. No parent/sibling write is permitted.
        $inside = (Join-Path $root ('.probe-' + [Guid]::NewGuid().ToString('N'))).Replace("'","''")
        $insideScript = "[IO.File]::WriteAllText('$inside','GIT50_ATTACHED');"
        Invoke-Verified-Launch $moniker $insideScript $root
        if (-not (Test-Path -LiteralPath $inside -PathType Leaf)) {
            throw 'GIT50_OS_ROOT_WRITE_FAILED'
        }
        Remove-Item -LiteralPath $inside -Force

        $outside = [string]$record.negative_sentinel_path
        try {
            [IO.File]::WriteAllText($outside,'OUTSIDE_SENTINEL')
            $outsideLiteral = $outside.Replace("'","''")
            $denyScript = @"
try { [IO.File]::WriteAllText('$outsideLiteral','NOT_ALLOWED'); exit 91 }
catch [System.UnauthorizedAccessException] { exit 0 }
"@
            Invoke-Verified-Launch $moniker $denyScript $root
            if ([IO.File]::ReadAllText($outside) -cne 'OUTSIDE_SENTINEL') {
                throw 'GIT50_EXTERNAL_SENTINEL_CORRUPTED'
            }
        } finally {
            # Keep the sentinel until Revoke: every future command must
            # independently prove that the AppContainer cannot read it.
            if ($record.status -ne 'ACTIVATING') {
                Remove-Item -LiteralPath $outside -Force -ErrorAction SilentlyContinue
            }
        }
        # Copy into an admin-controlled code root. Runtime must never
        # execute a mutable broker script from the user workspace.
        $brokerCode = Join-Path $securityRoot 'turbo_broker_code'
        if (-not (Test-Path -LiteralPath $brokerCode -PathType Container)) {
            New-Item -ItemType Directory -Path $brokerCode -ErrorAction Stop | Out-Null
        }
        Assert-Not-Reparse $brokerCode
        foreach ($file in @('turbo_os_execute.ps1','turbo_appcontainer_probe.cs')) {
            $source = Join-Path $PSScriptRoot $file
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
                throw 'GIT50_BROKER_SOURCE_MISSING'
            }
            Copy-Item -LiteralPath $source -Destination (Join-Path $brokerCode $file) -Force -ErrorAction Stop
        }
        & icacls.exe $brokerCode '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'GIT50_BROKER_CODE_PROTECTION_FAILED' }
        foreach ($file in @('turbo_os_execute.ps1','turbo_appcontainer_probe.cs')) {
            $protectedFile = Join-Path $brokerCode $file
            & icacls.exe $protectedFile '/inheritance:r' '/grant:r' '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:R' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'GIT50_BROKER_FILE_PROTECTION_FAILED' }
        }
        $record.broker_entry=(Join-Path $brokerCode 'turbo_os_execute.ps1')
        $record.broker_source_sha256=[string](Get-FileHash -LiteralPath $record.broker_entry -Algorithm SHA256).Hash
        $record.status='ACTIVE_PROVISIONED'
        Write-Journal $record
        Write-Output 'GIT50_APP_CONTAINER_ROOT_OS_PROVISIONED'
        Write-Output 'GIT50_PRODUCTION_EXECUTOR_NOT_CONNECTED'
    } catch {
        try {
            Revoke-Recorded-Root $record
        } catch {
            # Keep the protected pending journal for recovery. NEVER assert
            # ACL OFF if a failed rollback leaves package permissions behind.
            Write-Warning 'GIT50_ACTIVATION_ROLLBACK_FAILED_REVOKE_MANUALLY'
        }
        throw
    }
}
finally {
    if ($held) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
