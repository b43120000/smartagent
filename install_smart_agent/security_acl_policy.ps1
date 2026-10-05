# Shared ACL policy for the restricted executor.  This file deliberately
# contains no interactive-user write grant: only an elevated updater may open
# the short-lived lease used to replace executor_code.
Set-StrictMode -Version 2.0

function Invoke-SmartAgentIcacls {
    param([Parameter(Mandatory=$true)][string[]]$Arguments, [Parameter(Mandatory=$true)][string]$Failure)
    & icacls.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "${Failure}:exit=$LASTEXITCODE" }
}

function Get-SmartAgentAclSnapshot {
    param([Parameter(Mandatory=$true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "security_acl_reparse_point_forbidden:$Path" }
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    return [ordered]@{ path=[IO.Path]::GetFullPath($Path).TrimEnd('\'); owner=[string]$acl.Owner; sddl=$acl.Sddl }
}

function Restore-SmartAgentAclSnapshot {
    param([Parameter(Mandatory=$true)]$Snapshot)
    if (-not (Test-Path -LiteralPath ([string]$Snapshot.path) -PathType Container)) { return }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetSecurityDescriptorSddlForm([string]$Snapshot.sddl)
    Set-Acl -LiteralPath ([string]$Snapshot.path) -AclObject $acl -ErrorAction Stop
}

function Grant-SmartAgentAclLease {
    param([Parameter(Mandatory=$true)][string]$Path)
    # Administrators/SYSTEM receive the lease.  Do not add the caller, Users,
    # or Authenticated Users; ACLs apply to principals, not to update.ps1.
    # A lease is only ever applied to a newly-created staging directory.  It
    # never recursively touches the active executor tree before the swap.
    Invoke-SmartAgentIcacls -Arguments @($Path,'/setowner','*S-1-5-32-544') -Failure 'update_acl_lease_owner_failed'
    Invoke-SmartAgentIcacls -Arguments @($Path,'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F') -Failure 'update_acl_lease_grant_failed'
}

function Set-SmartAgentExecutorCodeAcl {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$ExecutorSid
    )
    $root = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($root.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "security_acl_reparse_point_forbidden:$Path" }
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $none = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $entries = @(
        @((New-Object -TypeName Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-18'), [Security.AccessControl.FileSystemRights]::FullControl),
        @((New-Object -TypeName Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-544'), [Security.AccessControl.FileSystemRights]::FullControl),
        @((New-Object -TypeName Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-545'), [Security.AccessControl.FileSystemRights]::ReadAndExecute),
        @((New-Object -TypeName Security.Principal.SecurityIdentifier -ArgumentList $ExecutorSid), [Security.AccessControl.FileSystemRights]::ReadAndExecute)
    )
    $items = @($root) + @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop)
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "security_acl_reparse_point_forbidden:$($item.FullName)" }
        if ($item.PSIsContainer) { $acl = New-Object Security.AccessControl.DirectorySecurity } else { $acl = New-Object Security.AccessControl.FileSecurity }
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner((New-Object -TypeName Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-544'))
        foreach ($entry in $entries) {
            $ruleInherit = $(if ($item.PSIsContainer) {$inherit} else {[Security.AccessControl.InheritanceFlags]::None})
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($entry[0], $entry[1], $ruleInherit, $none, $allow)
            [void]$acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $item.FullName -AclObject $acl -ErrorAction Stop
    }
}

function Test-SmartAgentNoInteractiveWriteAcl {
    param([Parameter(Mandatory=$true)][string]$Path)
    # FileSystemRights.Modify and FullControl are composite masks that include
    # read/execute bits. Testing for any overlap with those values therefore
    # misclassifies a legitimate ReadAndExecute ACE as writable. Match only
    # the individual mutation capabilities forbidden to broad principals.
    $mutationMask = [int](
        [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    foreach ($rule in @($acl.Access)) {
        $identity = [string]$rule.IdentityReference.Value
        try {
            $identitySid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        } catch {
            $identitySid = ''
        }
        # Compare immutable well-known SIDs rather than localized NTAccount
        # display names (for example BUILTIN\Users on an English host).
        $isBroadInteractive = $identitySid -in @('S-1-5-32-545','S-1-5-11')
        $writes = ([int]$rule.FileSystemRights -band $mutationMask) -ne 0
        if ($isBroadInteractive -and $writes -and $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            throw "update_acl_interactive_write_forbidden:${Path}:$identity"
        }
    }
}
