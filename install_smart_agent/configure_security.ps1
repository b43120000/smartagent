param(
    [string]$WorkspaceContainer = "$env:USERPROFILE\SmartAgentWorkspaces\default",
    [string[]]$AdditionalWriteRoots = @(),
    [string]$RuntimeRoot = "",
    [string[]]$DeniedWriteRoots = @("$env:PUBLIC", "$env:WINDIR\Temp"),
    [string[]]$ReadOnlyRoots = @(),
    [string]$AccessRootsBase64 = "",
    [string]$SkillRoot = "",
    [string]$SkillSourcePath = "$env:USERPROFILE\.codex\skills",
    [string]$ExecutorUser = "SmartAgentExecutor",
    [string]$ProfilePath = "",
    [string]$ExecutorPython = "",
    [string]$ControllerSid = "",
    [switch]$RepairExecutorCredential,
    [switch]$SkipAclModeRestore,
    [switch]$NonInteractive,
    [switch]$Apply
)
$ErrorActionPreference = "Stop"
$script:aclTransactionCommitted = $false
$pendingAclJournalPath = ""
$policyPath = Join-Path $PSScriptRoot 'security_acl_policy.ps1'
. $policyPath
$env:PYTHONSAFEPATH = "1"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = Join-Path $projectRoot "localdata\runtime\security"
}
if ([string]::IsNullOrWhiteSpace($SkillRoot)) {
    $SkillRoot = Join-Path $projectRoot "localdata\secure\windows_security\skills"
}
if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    $ProfilePath = Join-Path $projectRoot "localdata\secure\windows_security\security_profile.json"
}
if ([string]::IsNullOrWhiteSpace($WorkspaceContainer)) {
    $WorkspaceContainer = Join-Path $env:USERPROFILE "SmartAgentWorkspaces\default"
}
if (-not [string]::IsNullOrWhiteSpace($AccessRootsBase64)) {
    try {
        $accessRootsJson = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($AccessRootsBase64)
        )
        $accessRootsPayload = $accessRootsJson | ConvertFrom-Json
        $requiredAccessFields = @('additional_write_roots', 'read_only_roots', 'denied_write_roots')
        foreach ($field in $requiredAccessFields) {
            if ($accessRootsPayload.PSObject.Properties.Name -notcontains $field) {
                throw "missing field: $field"
            }
        }
        $AdditionalWriteRoots = @(
            @($accessRootsPayload.additional_write_roots) |
                Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
                ForEach-Object { [string]$_ }
        )
        $ReadOnlyRoots = @(
            @($accessRootsPayload.read_only_roots) |
                Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
                ForEach-Object { [string]$_ }
        )
        $DeniedWriteRoots = @(
            @($accessRootsPayload.denied_write_roots) |
                Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } |
                ForEach-Object { [string]$_ }
        )
    } catch {
        throw "Invalid AccessRootsBase64 payload: $($_.Exception.Message)"
    }
}
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentName = if ($ControllerSid -match '^S-1-') { "*$ControllerSid" } else { $currentIdentity.Name }
$principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
$workspace = [IO.Path]::GetFullPath($WorkspaceContainer)
$additionalWriteRoots = @($AdditionalWriteRoots | ForEach-Object {[IO.Path]::GetFullPath($_)} | Select-Object -Unique)
$authorizedWriteRoots = @($workspace) + @($additionalWriteRoots) | Select-Object -Unique
$runtime = [IO.Path]::GetFullPath($RuntimeRoot)
$endpoint = Join-Path $runtime "executor_queue"
$securityRoot = [IO.Path]::GetFullPath((Split-Path -Parent -Path $ProfilePath)).TrimEnd('\')
$controllerState = Join-Path $securityRoot "controller_state"
$codeRoot = Join-Path $securityRoot "executor_code"
$skillsRoot = [IO.Path]::GetFullPath($SkillRoot)
$skillsSource = if ($SkillSourcePath) { [IO.Path]::GetFullPath($SkillSourcePath) } else { "" }
$executorAccountName = $ExecutorUser.Trim()
if ([string]::IsNullOrWhiteSpace($executorAccountName) -or $executorAccountName -notmatch '^[A-Za-z0-9._-]{1,20}$') {
    throw "ExecutorUser must be a short local account name, not a path: $ExecutorUser"
}
$executorProfilePath = Join-Path ([Environment]::GetFolderPath('UserProfile') | Split-Path) $executorAccountName
$taskName = "SmartAgent Restricted Executor"
$protectedSkillsRoot = [IO.DirectoryInfo]::new((Join-Path $securityRoot 'skills')).FullName.TrimEnd('\')
$normalizedSkillsRoot = [IO.DirectoryInfo]::new($skillsRoot).FullName.TrimEnd('\')
if (-not ($normalizedSkillsRoot -ieq $protectedSkillsRoot) -and
    -not ($normalizedSkillsRoot.StartsWith($protectedSkillsRoot + '\',[StringComparison]::OrdinalIgnoreCase))) {
    throw "SkillRoot must remain below the protected SmartAgent security root: $skillsRoot"
}
function Invoke-Icacls {
    param([Parameter(Mandatory=$true)][string[]]$Arguments)
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed (exit=$LASTEXITCODE): $($Arguments -join ' ')" }
}
function Test-PathOverlap {
    param([string]$Left, [string]$Right)
    $leftPath = [IO.Path]::GetFullPath($Left).TrimEnd('\')
    $rightPath = [IO.Path]::GetFullPath($Right).TrimEnd('\')
    return $leftPath.Equals($rightPath,[StringComparison]::OrdinalIgnoreCase) -or
        $leftPath.StartsWith($rightPath + '\',[StringComparison]::OrdinalIgnoreCase) -or
        $rightPath.StartsWith($leftPath + '\',[StringComparison]::OrdinalIgnoreCase)
}
function Get-FilesystemType {
    param([Parameter(Mandatory=$true)][string]$Path)
    $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path))
    return ([IO.DriveInfo]::new($root)).DriveFormat.ToUpperInvariant()
}
function Get-ExecutorTraverseAclState {
    param(
        [Parameter(Mandatory=$true)][Security.AccessControl.DirectorySecurity]$Acl,
        [Parameter(Mandatory=$true)][Security.Principal.SecurityIdentifier]$ExecutorSid
    )
    $rules = @($Acl.GetAccessRules(
        $true,$true,[Security.Principal.SecurityIdentifier]
    ) | Where-Object { $_.IdentityReference.Value -eq $ExecutorSid.Value }
    )
    if ($rules.Count -eq 0) { return 'NONE' }
    if (@($rules | Where-Object { $_.IsInherited }).Count -gt 0) { return 'CUSTOM' }
    $mutationMask = [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    $expectedAllow = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
        [Security.AccessControl.FileSystemRights]::Synchronize
    $expectedDeny = $mutationMask
    $legacyDeny = $mutationMask -bor [Security.AccessControl.FileSystemRights]::Synchronize
    $allowRules = @($rules | Where-Object {
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow
    })
    $denyRules = @($rules | Where-Object {
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny
    })
    $canonicalDeny = $denyRules.Count -eq 1 -and
        $denyRules[0].InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None -and
        $denyRules[0].PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None -and
        [int64]$denyRules[0].FileSystemRights -eq [int64]$expectedDeny
    if ($rules.Count -eq 1 -and $allowRules.Count -eq 0 -and $canonicalDeny) { return 'SAFE_PARTIAL_DENY' }
    $legacyCanonicalDeny = $denyRules.Count -eq 1 -and
        $denyRules[0].InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None -and
        $denyRules[0].PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None -and
        [int64]$denyRules[0].FileSystemRights -eq [int64]$legacyDeny
    if ($rules.Count -eq 1 -and $allowRules.Count -eq 0 -and $legacyCanonicalDeny) { return 'LEGACY_SAFE_PARTIAL_DENY' }
    $canonical = $rules.Count -eq 2 -and $allowRules.Count -eq 1 -and $canonicalDeny -and
        $allowRules[0].InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None -and
        $allowRules[0].PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None -and
        [int64]$allowRules[0].FileSystemRights -eq [int64]$expectedAllow
    if ($canonical) { return 'MANAGED_ROOT_ONLY' }
    $legacyCanonical = $rules.Count -eq 2 -and $allowRules.Count -eq 1 -and $denyRules.Count -eq 1 -and
        $allowRules[0].InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None -and
        $allowRules[0].PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None -and
        $denyRules[0].InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None -and
        $denyRules[0].PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None -and
        [int64]$allowRules[0].FileSystemRights -eq [int64]$expectedAllow -and
        [int64]$denyRules[0].FileSystemRights -eq [int64]$legacyDeny
    if ($legacyCanonical) { return 'LEGACY_SYNC_DENY' }
    return 'CUSTOM'
}
function Assert-WorkspaceTraverseAcl {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][Security.Principal.SecurityIdentifier]$ExecutorSid
    )
    if ((Get-ExecutorTraverseAclState -Acl (Get-Acl -LiteralPath $Path) -ExecutorSid $ExecutorSid) -ne 'MANAGED_ROOT_ONLY') {
        throw "Workspace ancestor ACL is not root-only RX/no-write: $Path"
    }
}
function Set-ExecutorTraverseDeny {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][Security.Principal.SecurityIdentifier]$ExecutorSid,
        [switch]$IncludeSynchronize,
        [switch]$Remove
    )
    $acl = Get-Acl -LiteralPath $Path
    $explicitDenyRules = @($acl.GetAccessRules(
        $true,$false,[Security.Principal.SecurityIdentifier]
    ) | Where-Object {
        $_.IdentityReference.Value -eq $ExecutorSid.Value -and
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Deny
    })
    foreach ($rule in $explicitDenyRules) { $acl.RemoveAccessRuleSpecific($rule) }
    if (-not $Remove) {
        $rights = [Security.AccessControl.FileSystemRights]::Write -bor
            [Security.AccessControl.FileSystemRights]::Delete -bor
            [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
            [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
            [Security.AccessControl.FileSystemRights]::TakeOwnership
        if ($IncludeSynchronize) { $rights = $rights -bor [Security.AccessControl.FileSystemRights]::Synchronize }
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $ExecutorSid,$rights,[Security.AccessControl.AccessControlType]::Deny
        )
        $acl.AddAccessRule($rule) | Out-Null
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Restore-ExecutorTraverseAcl {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][Security.Principal.SecurityIdentifier]$ExecutorSid,
        [Parameter(Mandatory=$true)][ValidateSet('NONE','MANAGED_ROOT_ONLY','LEGACY_SYNC_DENY')][string]$State
    )
    $currentState = Get-ExecutorTraverseAclState -Acl (Get-Acl -LiteralPath $Path) -ExecutorSid $ExecutorSid
    if ($currentState -eq $State) { return }
    if ($currentState -eq 'CUSTOM') { throw "Refusing to replace custom/inherited executor ACE: $Path" }
    $sidIdentity = "*$($ExecutorSid.Value)"
    if ($currentState -in @('MANAGED_ROOT_ONLY','LEGACY_SYNC_DENY') -and $State -eq 'NONE') {
        Invoke-Icacls @($Path,'/remove:g',$sidIdentity)
        Set-ExecutorTraverseDeny -Path $Path -ExecutorSid $ExecutorSid -IncludeSynchronize:$false -Remove
    } elseif ($currentState -eq 'SAFE_PARTIAL_DENY' -and $State -eq 'NONE') {
        Set-ExecutorTraverseDeny -Path $Path -ExecutorSid $ExecutorSid -IncludeSynchronize:$false -Remove
    } elseif ($currentState -eq 'LEGACY_SAFE_PARTIAL_DENY' -and $State -eq 'NONE') {
        Set-ExecutorTraverseDeny -Path $Path -ExecutorSid $ExecutorSid -IncludeSynchronize:$false -Remove
    } elseif ($currentState -eq 'NONE' -and $State -eq 'MANAGED_ROOT_ONLY') {
        Set-ExecutorTraverseDeny -Path $Path -ExecutorSid $ExecutorSid -IncludeSynchronize:$false
        Invoke-Icacls @($Path,'/grant:r',"${sidIdentity}:(RX)")
    } elseif ($currentState -eq 'SAFE_PARTIAL_DENY' -and $State -eq 'MANAGED_ROOT_ONLY') {
        Invoke-Icacls @($Path,'/grant:r',"${sidIdentity}:(RX)")
    } elseif ($currentState -eq 'LEGACY_SYNC_DENY' -and $State -eq 'MANAGED_ROOT_ONLY') {
        Set-ExecutorTraverseDeny -Path $Path -ExecutorSid $ExecutorSid -IncludeSynchronize:$false
    } elseif ($currentState -eq 'MANAGED_ROOT_ONLY' -and $State -eq 'LEGACY_SYNC_DENY') {
        Set-ExecutorTraverseDeny -Path $Path -ExecutorSid $ExecutorSid -IncludeSynchronize
    } elseif ($currentState -eq 'LEGACY_SAFE_PARTIAL_DENY' -and $State -eq 'LEGACY_SYNC_DENY') {
        Invoke-Icacls @($Path,'/grant:r',"${sidIdentity}:(RX)")
    } else {
        throw "Unsupported executor ACL transition: $currentState -> $State on $Path"
    }
}
function Get-WorkspaceTraverseRoots {
    param([Parameter(Mandatory=$true)][string[]]$WorkspaceRoots)
    $result = @()
    foreach ($root in $WorkspaceRoots) {
        $resolved = [IO.Path]::GetFullPath($root).TrimEnd('\')
        $volumeRoot = [IO.Path]::GetPathRoot($resolved).TrimEnd('\')
        $parent = Split-Path -Parent $resolved
        while (-not [string]::IsNullOrWhiteSpace($parent)) {
            $normalizedParent = [IO.Path]::GetFullPath($parent).TrimEnd('\')
            if ($normalizedParent.Equals($volumeRoot,[StringComparison]::OrdinalIgnoreCase)) { break }
            $result += $normalizedParent
            $parent = Split-Path -Parent $normalizedParent
        }
    }
    return @($result | Select-Object -Unique | Where-Object {
        $candidate = [IO.Path]::GetFullPath([string]$_).TrimEnd('\')
        -not @($WorkspaceRoots | Where-Object {
            $writeRoot = [IO.Path]::GetFullPath([string]$_).TrimEnd('\')
            $candidate.Equals($writeRoot,[StringComparison]::OrdinalIgnoreCase) -or
                $candidate.StartsWith($writeRoot + '\',[StringComparison]::OrdinalIgnoreCase)
        })
    })
}
function Grant-BatchLogonRight {
    param([Parameter(Mandatory=$true)][Security.Principal.SecurityIdentifier]$Sid)
    if (-not ('SmartAgent.Security.LsaRights' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
namespace SmartAgent.Security {
    public static class LsaRights {
        [StructLayout(LayoutKind.Sequential)]
        private struct LSA_OBJECT_ATTRIBUTES {
            public int Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct LSA_UNICODE_STRING {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }
        [DllImport("advapi32.dll", SetLastError=true)]
        private static extern uint LsaOpenPolicy(IntPtr systemName, ref LSA_OBJECT_ATTRIBUTES attributes, uint access, out IntPtr handle);
        [DllImport("advapi32.dll")]
        private static extern uint LsaAddAccountRights(IntPtr handle, IntPtr sid, LSA_UNICODE_STRING[] rights, uint count);
        [DllImport("advapi32.dll")]
        private static extern uint LsaNtStatusToWinError(uint status);
        [DllImport("advapi32.dll")]
        private static extern uint LsaClose(IntPtr handle);
        public static void GrantBatchLogon(SecurityIdentifier sid) {
            const uint POLICY_LOOKUP_NAMES = 0x00000800;
            const uint POLICY_CREATE_ACCOUNT = 0x00000010;
            var attributes = new LSA_OBJECT_ATTRIBUTES();
            attributes.Length = Marshal.SizeOf(attributes);
            IntPtr policy;
            uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, POLICY_LOOKUP_NAMES | POLICY_CREATE_ACCOUNT, out policy);
            if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
            byte[] sidBytes = new byte[sid.BinaryLength];
            sid.GetBinaryForm(sidBytes, 0);
            var pin = GCHandle.Alloc(sidBytes, GCHandleType.Pinned);
            IntPtr rightBuffer = Marshal.StringToHGlobalUni("SeBatchLogonRight");
            try {
                var right = new LSA_UNICODE_STRING {
                    Buffer = rightBuffer,
                    Length = (ushort)("SeBatchLogonRight".Length * 2),
                    MaximumLength = (ushort)(("SeBatchLogonRight".Length + 1) * 2)
                };
                status = LsaAddAccountRights(policy, pin.AddrOfPinnedObject(), new [] { right }, 1);
                if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
            } finally {
                Marshal.FreeHGlobal(rightBuffer);
                pin.Free();
                LsaClose(policy);
            }
        }
    }
}
'@
    }
    [SmartAgent.Security.LsaRights]::GrantBatchLogon($Sid)
}
if (-not $ExecutorPython) {
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $ExecutorPython = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { (Get-Command python.exe -ErrorAction Stop).Source }
}
$ExecutorPython = [IO.Path]::GetFullPath($ExecutorPython)
$drive = [IO.Path]::GetPathRoot($workspace)
$workspaceTraverseRoots = @(Get-WorkspaceTraverseRoots -WorkspaceRoots $authorizedWriteRoots)
if ((Get-FilesystemType $workspace) -ne "NTFS") { throw "WorkspaceContainer must be on NTFS: $workspace" }
foreach ($path in $additionalWriteRoots) {
    if (-not (Test-Path -LiteralPath $path)) { throw "AdditionalWriteRoot does not exist: $path" }
    if ((Get-FilesystemType $path) -ne "NTFS") { throw "AdditionalWriteRoot must be on NTFS: $path" }
}
$retiredWorkspaceRoots = @()
$previousProfile = $null
if (Test-Path -LiteralPath $ProfilePath) {
    try {
        $previousProfile = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json
        $previousRoots = @(
            $previousProfile.authorized_workspace_roots |
                Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
        )
        if ($previousRoots.Count -eq 0 -and $previousProfile.workspace_container) {
            $previousRoots = @($previousProfile.workspace_container)
        }
        foreach ($previousRoot in $previousRoots) {
            if (-not $previousRoot) { continue }
            $resolvedPrevious = [IO.Path]::GetFullPath([string]$previousRoot)
            $currentWriteOverlap = $false
            foreach ($currentWriteRoot in $authorizedWriteRoots) {
                if (Test-PathOverlap $resolvedPrevious $currentWriteRoot) { $currentWriteOverlap = $true; break }
            }
            if ((Test-Path -LiteralPath $resolvedPrevious) -and -not $currentWriteOverlap) {
                $retiredWorkspaceRoots += $resolvedPrevious
            }
        }
    } catch { throw "Unable to read previous security profile: $($_.Exception.Message)" }
}
$DeniedWriteRoots = @($DeniedWriteRoots) + @($retiredWorkspaceRoots)
$retiredReadGrantRoots = @()
$retiredDeniedRoots = @()
$retiredTraverseRoots = @()
if ($previousProfile) {
    $activeGrantRoots = @($authorizedWriteRoots) + @($ReadOnlyRoots) + @($skillsRoot,$codeRoot)
    foreach ($previousRoot in @($previousProfile.read_only_roots)) {
        if ([string]::IsNullOrWhiteSpace([string]$previousRoot)) { continue }
        $resolvedPrevious = [IO.Path]::GetFullPath([string]$previousRoot)
        $stillGranted = $false
        foreach ($activeRoot in $activeGrantRoots) {
            if (Test-PathOverlap $resolvedPrevious $activeRoot) { $stillGranted = $true; break }
        }
        if ((Test-Path -LiteralPath $resolvedPrevious) -and -not $stillGranted) { $retiredReadGrantRoots += $resolvedPrevious }
    }
    foreach ($previousRoot in @($previousProfile.denied_write_roots)) {
        if ([string]::IsNullOrWhiteSpace([string]$previousRoot)) { continue }
        $resolvedPrevious = [IO.Path]::GetFullPath([string]$previousRoot)
        $stillDenied = $false
        foreach ($activeRoot in @($DeniedWriteRoots) + @($executorProfilePath)) {
            if (Test-PathOverlap $resolvedPrevious $activeRoot) { $stillDenied = $true; break }
        }
        if ((Test-Path -LiteralPath $resolvedPrevious) -and -not $stillDenied) { $retiredDeniedRoots += $resolvedPrevious }
    }
    foreach ($previousRoot in @($previousProfile.workspace_traverse_roots)) {
        if ([string]::IsNullOrWhiteSpace([string]$previousRoot)) { continue }
        $resolvedPrevious = [IO.Path]::GetFullPath([string]$previousRoot).TrimEnd('\')
        $stillRequired = @($workspaceTraverseRoots | Where-Object {
            ([IO.Path]::GetFullPath([string]$_).TrimEnd('\')).Equals($resolvedPrevious,[StringComparison]::OrdinalIgnoreCase)
        }).Count -gt 0
        if ((Test-Path -LiteralPath $resolvedPrevious -PathType Container) -and -not $stillRequired) {
            $retiredTraverseRoots += $resolvedPrevious
        }
    }
}
$allowedDeploymentRoots = @($authorizedWriteRoots) + @($runtime,$securityRoot,$controllerState,$codeRoot,$skillsRoot)
foreach ($authorizedRoot in $authorizedWriteRoots) {
    if (Test-PathOverlap $authorizedRoot $projectRoot) {
        throw "Writable workspace must not overlap the SmartAgent install folder: $authorizedRoot <-> $projectRoot"
    }
}
foreach ($candidate in @($ReadOnlyRoots) + @($DeniedWriteRoots)) {
    $resolvedCandidate = [IO.Path]::GetFullPath($candidate)
    if (-not (Test-Path -LiteralPath $resolvedCandidate)) { throw "Security root does not exist: $resolvedCandidate" }
    foreach ($allowedRoot in $allowedDeploymentRoots) {
        if (Test-PathOverlap $resolvedCandidate $allowedRoot) {
            throw "Security root overlaps an executor deployment root: $resolvedCandidate <-> $allowedRoot"
        }
    }
}
Write-Host "SmartAgent security plan"
Write-Host "  executor user : $executorAccountName"
Write-Host "  workspace     : $workspace (Modify)"
Write-Host "  write roots   : $($authorizedWriteRoots -join ', ')"
Write-Host "  traverse roots: $($workspaceTraverseRoots -join ', ') (this folder only RX)"
Write-Host "  runtime       : $runtime (Modify)"
Write-Host "  control state : $controllerState (executor cannot write)"
Write-Host "  profile       : $ProfilePath"
Write-Host "  executor code : $codeRoot (read-only to executor)"
Write-Host "  skills        : $skillsRoot (read-only to executor)"
Write-Host "  skill source  : $skillsSource"
Write-Host "  scheduled task: $taskName (stored task credential, limited token)"
Write-Host "  denied roots  : $($DeniedWriteRoots -join ', ')"
Write-Host "  read-only     : $($ReadOnlyRoots -join ', ')"
Write-Host "No disk will be formatted. Existing personal folders are not modified."
$existingExecutor = Get-LocalUser -Name $executorAccountName -ErrorAction SilentlyContinue
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$installMarker = Join-Path $securityRoot 'executor_install_pending.json'
$pendingInstall = $null
if (Test-Path -LiteralPath $installMarker) {
    try { $pendingInstall = Get-Content -LiteralPath $installMarker -Raw | ConvertFrom-Json }
    catch { throw "executor_install_marker_invalid; remove only after verifying the local executor account" }
}
$initialRecovery = (
    $null -ne $existingExecutor -and $null -eq $existingTask -and
    $null -ne $pendingInstall -and
    [string]$pendingInstall.executor_user -eq $executorAccountName -and
    ([string]::IsNullOrWhiteSpace([string]$pendingInstall.executor_sid) -or
     [string]$pendingInstall.executor_sid -eq [string]$existingExecutor.SID)
)
$credentialRepair = (
    $RepairExecutorCredential -and
    $null -ne $existingExecutor -and $null -eq $existingTask
)
if ($null -ne $existingExecutor -and $null -eq $existingTask -and -not $initialRecovery) {
    Write-Warning "A system-wide $executorAccountName account exists, but the $taskName task is missing."
    Write-Warning "This is incomplete machine authorization, not a reusable authorization."
}
if (-not $Apply) { Write-Host "Preview only. Re-run with -Apply after reviewing these exact paths."; exit 0 }
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "-Apply requires an elevated PowerShell window" }
if ($null -ne $existingExecutor -and $null -eq $existingTask -and -not $initialRecovery -and -not $RepairExecutorCredential) {
    if ($NonInteractive) {
        throw "executor_account_task_state_inconsistent:orphaned_account_requires_explicit_repair"
    }
    $repairAnswer = Read-Host "Type REPAIR to rotate the orphaned executor credential and rebuild its task"
    if ($repairAnswer -cne "REPAIR") {
        throw "executor_account_task_state_inconsistent:orphaned_account_requires_explicit_repair"
    }
    $RepairExecutorCredential = $true
    $credentialRepair = $true
}
if ($RepairExecutorCredential -and -not $credentialRepair) {
    throw "executor_credential_repair_requires_orphaned_account_without_task"
}
if ($null -eq $existingExecutor -and $null -ne $existingTask) {
    throw "executor_account_task_state_inconsistent:task_without_account"
}
if ($null -ne $existingExecutor -and $null -eq $existingTask -and -not $initialRecovery -and -not $credentialRepair) {
    throw "executor_account_task_state_inconsistent:orphaned_account_requires_explicit_repair"
}
if (-not $NonInteractive) {
    $answer = Read-Host "Type APPLY to create/update the account and ACL"
    if ($answer -cne "APPLY") { throw "Cancelled" }
}
# A failed first install can leave child directories with non-inheriting ACLs
# before the profile/task transaction commits.  Repair only this managed
# security tree, and only while no completed profile exists.
if (($initialRecovery -or $credentialRepair) -and
    (Test-Path -LiteralPath $securityRoot -PathType Container) -and
    -not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) {
    & takeown.exe /F $securityRoot /A /R /D Y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to take ownership of incomplete security tree (exit=$LASTEXITCODE)" }
    & icacls.exe $securityRoot /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to recover incomplete security tree ACL (exit=$LASTEXITCODE)" }
}
$initialInstall = ($null -eq $existingTask)
$needsBatchLogonGrant = ($null -eq $existingExecutor) -or $initialRecovery -or $credentialRepair
Write-Host "[security] account/task validation complete (initial_install=$initialInstall, grant_batch_logon=$needsBatchLogonGrant)"
$passwordPlain = $null
$passwordSecure = $null
if ($initialInstall) {
    $passwordBytes = New-Object byte[] 48
    $passwordGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $passwordGenerator.GetBytes($passwordBytes)
    $passwordGenerator.Dispose()
    $passwordPlain = [Convert]::ToBase64String($passwordBytes)
    $passwordSecure = ConvertTo-SecureString $passwordPlain -AsPlainText -Force
    if ($initialRecovery -or $credentialRepair) {
        if ($credentialRepair) {
            New-Item -ItemType Directory -Force -Path $securityRoot | Out-Null
            $markerPayload = [ordered]@{
                schema = 'SMARTAGENT_EXECUTOR_INSTALL_PENDING_V1'
                executor_user = $executorAccountName
                executor_sid = [string]$existingExecutor.SID
                created_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            } | ConvertTo-Json
            [IO.File]::WriteAllText($installMarker, $markerPayload, (New-Object Text.UTF8Encoding($false)))
        }
        Set-LocalUser -Name $executorAccountName -Password $passwordSecure -PasswordNeverExpires $true
    } else {
        New-Item -ItemType Directory -Force -Path $securityRoot | Out-Null
        $markerPayload = [ordered]@{
            schema = 'SMARTAGENT_EXECUTOR_INSTALL_PENDING_V1'
            executor_user = $executorAccountName
            executor_sid = ''
            created_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        } | ConvertTo-Json
        [IO.File]::WriteAllText($installMarker, $markerPayload, (New-Object Text.UTF8Encoding($false)))
        New-LocalUser -Name $executorAccountName -Password $passwordSecure -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
        $existingExecutor = Get-LocalUser -Name $executorAccountName -ErrorAction Stop
        $markerPayload = [ordered]@{
            schema = 'SMARTAGENT_EXECUTOR_INSTALL_PENDING_V1'
            executor_user = $executorAccountName
            executor_sid = [string]$existingExecutor.SID
            created_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        } | ConvertTo-Json
        [IO.File]::WriteAllText($installMarker, $markerPayload, (New-Object Text.UTF8Encoding($false)))
    }
} else {
    $taskActions = @($existingTask.Actions)
    $taskPrincipal = [string]$existingTask.Principal.UserId
    try {
        $taskPrincipalSid = if ($taskPrincipal -match '^S-1-') {
            [Security.Principal.SecurityIdentifier]::new($taskPrincipal)
        } else {
            [Security.Principal.NTAccount]::new($taskPrincipal).Translate([Security.Principal.SecurityIdentifier])
        }
    } catch {
        throw "executor_task_principal_unresolvable:$taskPrincipal"
    }
    if ($taskActions.Count -ne 1 -or
        -not ([IO.Path]::GetFullPath([string]$taskActions[0].Execute)).Equals($ExecutorPython,[StringComparison]::OrdinalIgnoreCase) -or
        [string]$taskActions[0].Arguments -ne '-m agent_core.restricted_executor_service' -or
        -not ([IO.Path]::GetFullPath([string]$taskActions[0].WorkingDirectory)).Equals($codeRoot,[StringComparison]::OrdinalIgnoreCase) -or
        -not $taskPrincipalSid.Equals($existingExecutor.SID)) {
        throw "executor_task_definition_mismatch; run explicit credential repair"
    }
    if (-not $existingExecutor.Enabled) { Enable-LocalUser -Name $executorAccountName }
}
$executorIdentity = "$env:COMPUTERNAME\$executorAccountName"
$executorAccount = Get-LocalUser -Name $executorAccountName -ErrorAction Stop
# A preserved scheduled task proves this identity was already provisioned with
# batch-logon rights. Avoid recompiling the LSA helper during ordinary ACL
# updates; first install and explicit credential repair still grant the right.
if ($needsBatchLogonGrant) {
    Write-Host "[security] granting batch-logon right"
    Grant-BatchLogonRight -Sid $executorAccount.SID
}
Write-Host "[security] enforcing executor password policy"
& net.exe user $executorAccountName /passwordreq:yes | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Unable to require a password for $executorAccountName (exit=$LASTEXITCODE)" }
Write-Host "[security] preparing ACL transaction journal"
$machineAuthorizationRoot = Join-Path ([string]$env:ProgramData) 'SmartAgent'
$pendingAclJournalPath = Join-Path $machineAuthorizationRoot 'pending_acl_transaction.json'
New-Item -ItemType Directory -Force -Path $machineAuthorizationRoot | Out-Null
Invoke-Icacls @($machineAuthorizationRoot,'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F','*S-1-5-32-545:(OI)(CI)RX')
Write-Host "[security] machine authorization root ACL ready"
if (Test-Path -LiteralPath $pendingAclJournalPath -PathType Leaf) {
    try {
        Write-Host "[security] recovering interrupted ACL transaction"
        $staleJournal = Get-Content -LiteralPath $pendingAclJournalPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($staleJournal.schema -ne 'SMARTAGENT_PENDING_ACL_TRANSACTION_V1') { throw 'schema' }
        $staleSid = [Security.Principal.SecurityIdentifier]::new([string]$staleJournal.executor_sid)
        foreach ($snapshot in @($staleJournal.acl_snapshots)) {
            if (-not (Test-Path -LiteralPath ([string]$snapshot.path) -PathType Container)) { continue }
            Write-Host "[security] restoring ACL snapshot: $([string]$snapshot.path)"
            $priorState = [string]$snapshot.executor_acl_state
            if ([string]::IsNullOrWhiteSpace($priorState) -and $snapshot.sddl) {
                $priorAcl = New-Object Security.AccessControl.DirectorySecurity
                $priorAcl.SetSecurityDescriptorSddlForm([string]$snapshot.sddl)
                $priorState = Get-ExecutorTraverseAclState -Acl $priorAcl -ExecutorSid $staleSid
            }
            if ($priorState -eq 'CUSTOM') { throw "custom executor ACE cannot be restored safely: $($snapshot.path)" }
            Restore-ExecutorTraverseAcl -Path ([string]$snapshot.path) -ExecutorSid $staleSid -State $priorState
        }
        Remove-Item -LiteralPath $pendingAclJournalPath -Force
        Write-Host "[security] interrupted ACL transaction recovered"
    } catch {
        throw "Unable to recover pending ACL transaction; run reinstall first: $($_.Exception.Message)"
    }
}
$aclSnapshots = @()
foreach ($path in @($workspaceTraverseRoots) + @($retiredTraverseRoots) | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { continue }
    $priorState = Get-ExecutorTraverseAclState -Acl (Get-Acl -LiteralPath $path) -ExecutorSid $executorAccount.SID
    if ($priorState -eq 'CUSTOM') { throw "Refusing to replace custom executor ACE on workspace ancestor: $path" }
    $aclSnapshots += [ordered]@{path=[IO.Path]::GetFullPath($path);executor_acl_state=$priorState}
}
$pendingAclJournal = [ordered]@{
    schema = 'SMARTAGENT_PENDING_ACL_TRANSACTION_V1'
    executor_user = $executorAccountName
    executor_sid = [string]$executorAccount.SID
    executor_identity = $executorIdentity
    workspace_traverse_roots = @($workspaceTraverseRoots)
    acl_snapshots = @($aclSnapshots)
    created_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}
[IO.File]::WriteAllText($pendingAclJournalPath, ($pendingAclJournal | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
Invoke-Icacls @($pendingAclJournalPath,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F')
trap {
    $originalError = $_
    if (-not $script:aclTransactionCommitted -and
        -not [string]::IsNullOrWhiteSpace([string]$pendingAclJournalPath) -and
        (Test-Path -LiteralPath $pendingAclJournalPath -PathType Leaf)) {
        try {
            $journal = Get-Content -LiteralPath $pendingAclJournalPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($journal.schema -eq 'SMARTAGENT_PENDING_ACL_TRANSACTION_V1') {
                foreach ($snapshot in @($journal.acl_snapshots)) {
                    if (-not (Test-Path -LiteralPath ([string]$snapshot.path) -PathType Container)) { continue }
                    $rollbackState = [string]$snapshot.executor_acl_state
                    if ($rollbackState -notin @('NONE','MANAGED_ROOT_ONLY','LEGACY_SYNC_DENY')) { throw "invalid rollback ACL state" }
                    Restore-ExecutorTraverseAcl -Path ([string]$snapshot.path) -ExecutorSid $executorAccount.SID -State $rollbackState
                }
            }
            Remove-Item -LiteralPath $pendingAclJournalPath -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Warning "ACL rollback was incomplete; reinstall will consume: $pendingAclJournalPath"
        }
    }
    throw $originalError
}
foreach ($path in @($retiredWorkspaceRoots) + @($retiredReadGrantRoots) | Select-Object -Unique) {
    Invoke-Icacls @($path,'/remove:g',$executorIdentity)
}
foreach ($path in @($retiredReadGrantRoots) + @($retiredDeniedRoots) | Select-Object -Unique) {
    Invoke-Icacls @($path,'/remove:d',$executorIdentity)
}
foreach ($path in @($retiredTraverseRoots | Select-Object -Unique)) {
    Restore-ExecutorTraverseAcl -Path $path -ExecutorSid $executorAccount.SID -State 'NONE'
}
New-Item -ItemType Directory -Force -Path $workspace,$runtime,$endpoint,$securityRoot,$controllerState,$codeRoot,$skillsRoot,$executorProfilePath | Out-Null
# ACLstatus off grants the restricted executor Full Control while the package
# is in maintenance mode. Re-sealing removes those grants from the entire
# installation before rebuilding the narrowly scoped permissions below.
Invoke-Icacls @($projectRoot,'/remove:g',$executorIdentity,'/T','/C')
# localdata\secure\windows_security is the administrator-owned security boundary.  The normal
# controller and restricted executor may traverse/read it, but cannot replace
# the profile or protected code.  The sibling Telegram credential remains
# controller-writable and is not coupled to Windows executor reconfiguration.
Invoke-Icacls @($securityRoot,'/inheritance:r','/grant:r',"${currentName}:(OI)(CI)RX",'*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',"${executorIdentity}:(OI)(CI)RX")
# The dedicated workspace has a closed ACL: the controller and administrators
# retain Full Control while the restricted executor receives Modify only here.
Invoke-Icacls @($workspace,'/remove:d',$executorIdentity)
Invoke-Icacls @($workspace,'/inheritance:r','/grant:r',"${currentName}:(OI)(CI)F",'*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',"${executorIdentity}:(OI)(CI)M")
foreach ($path in $additionalWriteRoots) {
    Invoke-Icacls @($path,'/remove:d',$executorIdentity)
    Invoke-Icacls @($path,'/grant:r',"${executorIdentity}:(OI)(CI)M")
}
# Build tools may enumerate workspace ancestors while resolving executables.
# This RX ACE applies to each ancestor itself only; it never inherits to siblings.
foreach ($path in $workspaceTraverseRoots) {
    Restore-ExecutorTraverseAcl -Path $path -ExecutorSid $executorAccount.SID -State 'MANAGED_ROOT_ONLY'
    Assert-WorkspaceTraverseAcl -Path $path -ExecutorSid $executorAccount.SID
}
Invoke-Icacls @($runtime,'/inheritance:r','/grant:r',"${currentName}:(OI)(CI)F",'*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',"${executorIdentity}:(OI)(CI)M")
Invoke-Icacls @($controllerState,'/remove:d',$executorIdentity)
Invoke-Icacls @($controllerState,'/inheritance:r','/grant:r',"${currentName}:(OI)(CI)F",'/deny',"${executorIdentity}:(OI)(CI)(W,D,DC,WDAC,WO)")
Invoke-Icacls @($executorProfilePath,'/remove:d',$executorIdentity)
Invoke-Icacls @($executorProfilePath,'/inheritance:r','/grant:r',"${currentName}:(OI)(CI)F",'*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',"${executorIdentity}:(OI)(CI)RX",'/deny',"${executorIdentity}:(OI)(CI)(W,D,DC,WDAC,WO)")
Copy-Item -LiteralPath (Join-Path $projectRoot "source\agent_core") -Destination $codeRoot -Recurse -Force
Set-SmartAgentExecutorCodeAcl -Path $codeRoot -ExecutorSid ([string]$executorAccount.SID)
Test-SmartAgentNoInteractiveWriteAcl -Path $codeRoot
if ($skillsSource -and (Test-Path -LiteralPath $skillsSource)) {
    $skillEntries = @(
        Get-ChildItem -LiteralPath $skillsSource -Directory -Force |
            Where-Object { $_.Name -notin @('.agents','__pycache__') }
    )
    foreach ($skillEntry in $skillEntries) {
        $reparsePoint = (@($skillEntry) + @(
            Get-ChildItem -LiteralPath $skillEntry.FullName -Recurse -Force
        )) | Where-Object {
            $_.Attributes -band [IO.FileAttributes]::ReparsePoint
        } | Select-Object -First 1
        if ($reparsePoint) { throw "Skill source contains a reparse point: $($reparsePoint.FullName)" }
    }
    Get-ChildItem -LiteralPath $skillsRoot -Force | Remove-Item -Recurse -Force
    $skillEntries | Copy-Item -Destination $skillsRoot -Recurse -Force
}
Invoke-Icacls @($skillsRoot,'/inheritance:r','/grant:r',"${currentName}:(OI)(CI)F",'*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F',"${executorIdentity}:(OI)(CI)RX")
$pythonExecutableDir = Split-Path $ExecutorPython -Parent
$pythonRoots = @($pythonExecutableDir)
$possibleVenvRoot = Split-Path $pythonExecutableDir -Parent
if (Test-Path -LiteralPath (Join-Path $possibleVenvRoot "pyvenv.cfg")) {
    $pythonRoots += $possibleVenvRoot
}
try {
    $basePrefix = (& $ExecutorPython -c "import sys;print(sys.base_prefix)").Trim()
    if ($basePrefix) { $pythonRoots += [IO.Path]::GetFullPath($basePrefix) }
} catch { throw "Unable to resolve executor Python runtime: $ExecutorPython" }
foreach ($pythonRoot in ($pythonRoots | Select-Object -Unique)) {
    Invoke-Icacls @($pythonRoot,'/grant:r',"${executorIdentity}:(OI)(CI)RX")
}
# Default ACLs let ordinary users create top-level directories in these roots.
# Root-only deny ACEs do not propagate into the explicitly writable runtime.
$rootCreateDenied = @($drive,$securityRoot) | Select-Object -Unique
foreach ($rootPath in $rootCreateDenied) {
    Invoke-Icacls @($rootPath,'/remove:d',$executorIdentity)
    Invoke-Icacls @($rootPath,'/deny',"${executorIdentity}:(WD,AD)")
}
foreach ($path in $ReadOnlyRoots) {
    $resolved = [IO.Path]::GetFullPath($path)
    if ((Get-FilesystemType $resolved) -ne "NTFS") { throw "ReadOnlyRoot must be NTFS: $resolved" }
    Invoke-Icacls @($resolved,'/remove:d',$executorIdentity)
    Invoke-Icacls @($resolved,'/grant:r',"${executorIdentity}:(OI)(CI)RX")
    # An inherited Users/Authenticated Users allow can otherwise keep this
    # root writable even after an explicit RX grant.  Deny only mutation
    # rights for the restricted executor identity while preserving reads.
    Invoke-Icacls @($resolved,'/deny',"${executorIdentity}:(OI)(CI)(W,D,DC,WDAC,WO)")
}
foreach ($path in $DeniedWriteRoots) {
    $resolved = [IO.Path]::GetFullPath($path)
    if ((Get-FilesystemType $resolved) -ne "NTFS") { throw "DeniedWriteRoot must be NTFS: $resolved" }
    Invoke-Icacls @($resolved,'/remove:d',$executorIdentity)
    Invoke-Icacls @($resolved,'/deny',"${executorIdentity}:(OI)(CI)(W,D,DC,WDAC,WO)")
}
$effectiveDeniedRoots = @($DeniedWriteRoots | ForEach-Object {[IO.Path]::GetFullPath($_)}) + @($executorProfilePath)
$effectiveReadOnlyRoots = @($ReadOnlyRoots | ForEach-Object {[IO.Path]::GetFullPath($_)}) + @($skillsRoot,$codeRoot)
# Build-tool execution trust is profile-owned and hash-pinned.  Only known Android
# compiler/linker/CMake/Ninja executable names below an authorized workspace's
# .android-build-sdk\sdk tree are enrolled.  Replacing/updating a tool changes
# its hash and therefore requires an explicit Security re-apply before execution.
$trustedExecutables = @()
$trustedToolNames = @('clang.exe','clang++.exe','ld.lld.exe','lld.exe','cmake.exe','ninja.exe')
foreach ($writeRoot in $authorizedWriteRoots) {
    $sdkRoot = Join-Path $writeRoot '.android-build-sdk\sdk'
    if (-not (Test-Path -LiteralPath $sdkRoot -PathType Container)) { continue }
    foreach ($candidate in Get-ChildItem -LiteralPath $sdkRoot -Recurse -File -ErrorAction Stop) {
        if ($trustedToolNames -notcontains $candidate.Name.ToLowerInvariant()) { continue }
        $full = [IO.Path]::GetFullPath($candidate.FullName)
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $full).Hash.ToLowerInvariant()
        $trustedExecutables += [ordered]@{path=$full;sha256=$hash;kind='ANDROID_BUILD_TOOL'}
    }
}
$profile = [ordered]@{schema="SMARTAGENT_WINDOWS_SECURITY_V2";executor_user=$executorAccountName;executor_sid=[string]$executorAccount.SID;authorized_workspace_roots=@($authorizedWriteRoots);workspace_container=$workspace;workspace_traverse_roots=@($workspaceTraverseRoots);skill_roots=@($skillsRoot);runtime_root=$runtime;executor_endpoint=$endpoint;controller_state_root=$controllerState;executor_python=$ExecutorPython;executor_code_root=$codeRoot;executor_task_name=$taskName;read_only_roots=@($effectiveReadOnlyRoots | Select-Object -Unique);denied_write_roots=@($effectiveDeniedRoots | Select-Object -Unique);root_create_denied=@($rootCreateDenied);trusted_executables=@($trustedExecutables);created_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}
$profileJson = $profile | ConvertTo-Json
$profileTemp = "$ProfilePath.tmp"
[IO.File]::WriteAllText($profileTemp, $profileJson, (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $profileTemp -Destination $ProfilePath -Force
# Atomic profile publication is the transaction commit point. From here onward
# the published profile and ancestor ACLs describe the same authorization. A
# later service/task failure is repaired by re-applying this profile; it must
# not roll ACLs back behind the already-published state.
$script:aclTransactionCommitted = $true
Remove-Item -LiteralPath $pendingAclJournalPath -Force -ErrorAction SilentlyContinue
# The controller profile is administrator-owned, but the interactive manager
# must be able to read its authorized roots. Keep ordinary users read-only.
Invoke-Icacls @($ProfilePath,'/inheritance:r','/grant:r',"${currentName}:F",'*S-1-5-18:F','*S-1-5-32-544:F','*S-1-5-32-545:R',"${executorIdentity}:R")
$action = New-ScheduledTaskAction -Execute $ExecutorPython -Argument "-m agent_core.restricted_executor_service" -WorkingDirectory $codeRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable
if ($initialInstall) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -User $executorIdentity -Password $passwordPlain -RunLevel Limited -Settings $settings -Force | Out-Null
    $passwordPlain = $null
    $passwordSecure.Dispose()
} else {
    # Workspace/ACL updates must preserve the Task Scheduler credential.  The
    # stored secret cannot be exported, so never rotate the account password or
    # unregister the task during an ordinary access-scope update.
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $stopDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $stopDeadline) {
        if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    }
    if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        throw "Unable to stop existing restricted executor task"
    }
    # Preserve the stored task credential while refreshing the executable and
    # protected-code working directory after an update or security re-seal.
    Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
}
$statePath = Join-Path $endpoint 'service_state.json'
Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
$serviceStartedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
& schtasks.exe /Run /TN $taskName | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Unable to start restricted executor task (exit=$LASTEXITCODE)" }
$ready = $false
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ($state.self_test.passed -eq $true -and
                [double]$state.heartbeat_at -ge $serviceStartedAt -and
                ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$state.heartbeat_at) -lt 10) {
                $ready = $true
                break
            }
        } catch { }
    }
}
if (-not $ready) {
    $task = Get-ScheduledTaskInfo -TaskName $taskName
    throw "Restricted executor did not become ready (last_task_result=$($task.LastTaskResult))"
}
$previousPythonPath = [string]$env:PYTHONPATH
$env:PYTHONPATH = $codeRoot
Push-Location $codeRoot
try {
    & $ExecutorPython -m agent_core.windows_security --workspace $workspace
    if ($LASTEXITCODE -ne 0) { throw "Restricted executor attestation failed" }
} finally {
    Pop-Location
    $env:PYTHONPATH = $previousPythonPath
}
$programDataRoot = [string]$env:ProgramData
if ([string]::IsNullOrWhiteSpace($programDataRoot)) {
    throw "ProgramData is unavailable; cannot publish machine authorization"
}
$protocolManifestPath = Join-Path $projectRoot 'config\protocol_manifest.json'
$protocolManifest = Get-Content -LiteralPath $protocolManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$protocolManifest.protocol_family) -or
    [int]$protocolManifest.protocol_version -le 0) {
    throw "Protocol manifest is invalid; cannot publish machine authorization"
}
$machineAuthorizationPath = Join-Path $machineAuthorizationRoot 'machine_authorization.json'
New-Item -ItemType Directory -Force -Path $machineAuthorizationRoot | Out-Null
Invoke-Icacls @($machineAuthorizationRoot,'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F','*S-1-5-32-545:(OI)(CI)RX')
$machineAuthorization = [ordered]@{
    schema = 'SMARTAGENT_MACHINE_AUTHORIZATION_V1'
    protocol_family = [string]$protocolManifest.protocol_family
    protocol_version = [int]$protocolManifest.protocol_version
    protocol_manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $protocolManifestPath).Hash.ToLowerInvariant()
    install_root = $projectRoot
    security_profile = [IO.Path]::GetFullPath($ProfilePath)
    executor_user = $executorAccountName
    executor_sid = [string]$executorAccount.SID
    executor_task_name = $taskName
    executor_endpoint = $endpoint
    updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}
$updaterSource = Join-Path $projectRoot 'install_smart_agent\update.ps1'
$updaterPolicySource = Join-Path $projectRoot 'install_smart_agent\security_acl_policy.ps1'
if (-not ((Test-Path -LiteralPath $updaterSource -PathType Leaf) -and (Test-Path -LiteralPath $updaterPolicySource -PathType Leaf))) {
    throw 'Protected updater source or ACL policy is missing'
}
$updaterHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $updaterSource).Hash.ToLowerInvariant()
$updaterPolicyHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $updaterPolicySource).Hash.ToLowerInvariant()
$updaterRoot = Join-Path $machineAuthorizationRoot 'updater'
$updaterReleaseRoot = Join-Path $updaterRoot ('releases\' + $updaterHash + '-' + $updaterPolicyHash)
New-Item -ItemType Directory -Force -Path $updaterReleaseRoot | Out-Null
Invoke-Icacls @($updaterRoot,'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F','*S-1-5-32-545:(OI)(CI)RX')
$protectedUpdaterPath = Join-Path $updaterReleaseRoot 'apply_update.ps1'
$protectedPolicyPath = Join-Path $updaterReleaseRoot 'security_acl_policy.ps1'
Copy-Item -LiteralPath $updaterSource -Destination $protectedUpdaterPath -Force
Copy-Item -LiteralPath $updaterPolicySource -Destination $protectedPolicyPath -Force
Invoke-Icacls @($protectedUpdaterPath,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F','*S-1-5-32-545:RX')
Invoke-Icacls @($protectedPolicyPath,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F','*S-1-5-32-545:RX')
$machineAuthorization.updater_path = $protectedUpdaterPath
$machineAuthorization.updater_sha256 = $updaterHash
$machineAuthorization.updater_policy_sha256 = $updaterPolicyHash
$machineAuthorizationTemp = "$machineAuthorizationPath.tmp"
[IO.File]::WriteAllText($machineAuthorizationTemp, ($machineAuthorization | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $machineAuthorizationTemp -Destination $machineAuthorizationPath -Force
Invoke-Icacls @($machineAuthorizationPath,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F','*S-1-5-32-545:R')
$previousPythonPath = [string]$env:PYTHONPATH
$env:PYTHONPATH = $codeRoot
try {
    $bindingPath = (& $ExecutorPython -m agent_core.path_cli remote_binding --root $projectRoot | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $bindingPath) { throw "Unable to resolve SmartAgent remote binding path" }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
if (Test-Path -LiteralPath $bindingPath) {
    $binding = Get-Content -LiteralPath $bindingPath -Raw | ConvertFrom-Json
    $binding.workspace = $workspace
    $binding.skill_path = $skillsRoot
    $bindingTemp = "$bindingPath.tmp"
    [IO.File]::WriteAllText($bindingTemp, ($binding | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $bindingTemp -Destination $bindingPath -Force
}
if (-not $SkipAclModeRestore) {
    $aclModePath = Join-Path $securityRoot 'acl_mode.json'
    if (Test-Path -LiteralPath $aclModePath -PathType Leaf) {
        $aclModeState = Get-Content -LiteralPath $aclModePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($aclModeState.schema -ne 'SMARTAGENT_ACL_MODE_V1' -or $aclModeState.mode -notin @('on','off')) {
            throw 'acl_mode_invalid'
        }
        if ($aclModeState.mode -eq 'off') {
            & (Join-Path $PSScriptRoot 'acl_status.ps1') -Mode off -ProjectRoot $projectRoot
        }
    }
}
Write-Host "Configured, started, and attested restricted executor."
Remove-Item -LiteralPath $installMarker -Force -ErrorAction SilentlyContinue
