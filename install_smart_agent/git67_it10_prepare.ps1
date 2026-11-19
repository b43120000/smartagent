[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$PrivateRoot
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
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
    [IO.Directory]::CreateDirectory($root) | Out-Null
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'
    $admins = New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544'
    $allowedSids = @($current.Value, $system.Value, $admins.Value)
    $acl = Get-Acl -LiteralPath $root
    # Do not inherit permissions from the general-purpose diagnostics dir.
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules($true, $true,
                    [Security.Principal.SecurityIdentifier]))) {
        $acl.RemoveAccessRuleSpecific($rule)
    }
    foreach ($sid in @($current, $system, $admins)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
             [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $root -AclObject $acl
    $checked = Get-Acl -LiteralPath $root
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
    Write-Error ('GIT67_INIT_ACL_FAILED:' + $_.Exception.GetType().Name)
    exit 4
}
