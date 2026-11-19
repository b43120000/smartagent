[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$PrivateRoot
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$phase = 'INITIALIZE'
try {
    $project = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $allowed = [IO.Path]::GetFullPath((Join-Path $project 'localdata\diagnostics')).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($PrivateRoot).TrimEnd('\')
    $prefix = $allowed + '\it10_GIT67_'
    if (-not $root.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'PRIVATE_ROOT_OUTSIDE_IT10_DIAGNOSTICS'
    }
    $suffix = $root.Substring($prefix.Length)
    if ($suffix -notmatch '^[A-Za-z0-9_-]{8,80}$' -or $suffix.Contains('\')) {
        throw 'PRIVATE_ROOT_NAME_INVALID'
    }
    $phase = 'CHECK_REPARSE_PARENTS'
    foreach ($item in @($project, (Join-Path $project 'localdata'),
                        $allowed)) {
        if (Test-Path -LiteralPath $item) {
            $entry = Get-Item -LiteralPath $item -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'REPARSE_PARENT_NOT_ALLOWED'
            }
        }
    }
    if (Test-Path -LiteralPath $root) {
        throw 'PRIVATE_ROOT_ALREADY_EXISTS_USE_FRESH_INIT'
    }
    $phase = 'CREATE_PRIVATE_DIRECTORY'
    [IO.Directory]::CreateDirectory($root) | Out-Null
    $phase = 'GET_IDENTITY'
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $allowedSids = @($current.Value, $system.Value, $admins.Value)
    $phase = 'REBUILD_ACL'
    $phase = 'REBUILD_GET_ACL'
    $acl = [System.IO.Directory]::GetAccessControl($root)
    # Do not inherit permissions from the general-purpose diagnostics dir.
    $phase = 'REBUILD_PROTECT'
    $acl.SetAccessRuleProtection($true, $false)
    $phase = 'REBUILD_LIST_RULES'
    foreach ($rule in @($acl.GetAccessRules($true, $true,
                    [Security.Principal.SecurityIdentifier]))) {
        $acl.RemoveAccessRuleSpecific($rule)
    }
    $phase = 'REBUILD_CREATE_RULES'
    foreach ($sid in @($current, $system, $admins)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
             [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $phase = 'REBUILD_ADD_RULE'
        $acl.AddAccessRule($rule)
        $phase = 'REBUILD_CREATE_RULES'
    }
    $phase = 'APPLY_ACL'
    [System.IO.Directory]::SetAccessControl($root, $acl)
    $phase = 'VERIFY_ACL'
    $checked = [System.IO.Directory]::GetAccessControl($root)
    if (-not $checked.AreAccessRulesProtected) {
        throw 'PRIVATE_ACL_INHERITANCE_NOT_DISABLED'
    }
    $rules = @($checked.GetAccessRules($true, $true,
                 [Security.Principal.SecurityIdentifier]))
    foreach ($rule in $rules) {
        if (($allowedSids -notcontains $rule.IdentityReference.Value) -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.IsInherited) {
            throw 'PRIVATE_ACL_UNEXPECTED_ACCESS_RULE'
        }
    }
    foreach ($sid in $allowedSids) {
        if (-not @($rules | Where-Object {$_.IdentityReference.Value -eq $sid}).Count) {
            throw 'PRIVATE_ACL_REQUIRED_PRINCIPAL_MISSING'
        }
    }
    Write-Output 'GIT67_PRIVATE_ACL_VERIFIED=YES'
    exit 0
} catch {
    # Do not emit the user's absolute root, account SID or access rule details.
    $code = ''
    if ($_.Exception -is [System.Management.Automation.CommandNotFoundException]) {
        $name = [string]$_.Exception.CommandName
        if ($name -match '^[A-Za-z][A-Za-z0-9-]{0,55}$') {
            $code = ':' + $name
        }
    }
    Write-Error ('GIT67_INIT_ACL_FAILED:' + $phase + ':' + $_.Exception.GetType().Name + $code)
    exit 4
}
