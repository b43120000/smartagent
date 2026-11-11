# P3-D2 disposable Windows integration: old broker -> pre-populated Root ->
# actual native AppContainer write -> exact metadata rollback.
# NEVER a production installer. In particular, Set-Acl/IL code is forbidden
# for real customer directories, regardless of command-line arguments.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or
    [string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    throw 'TURBO_B_P3D2_WINDOWS_CI_ONLY'
}
function Assert([bool]$ok,[string]$msg) {if(-not $ok){throw $msg}}
$parent=[IO.Path]::GetFullPath($env:RUNNER_TEMP)
$fixture=Join-Path $parent ('TurboBFullSwitch-'+[Guid]::NewGuid().ToString('N'))
$project=Join-Path $fixture 'project'
$workspace=Join-Path $fixture 'workspace'
$old=Join-Path $workspace 'fresh_old'
$target=Join-Path $workspace 'populated_target'
$sec=Join-Path $project 'localdata\secure\windows_security'
$native=Join-Path $PSScriptRoot '..\turbo_os_activation.ps1'
$auto=Join-Path $PSScriptRoot '..\turbo_auto_activate.ps1'
$preflight=Join-Path $PSScriptRoot '..\turbo_existing_root_preflight.ps1'
$csharp=Join-Path $PSScriptRoot '..\turbo_appcontainer_probe.cs'
$package=''
$moniker=''
$newFile=''
$testFile=''
$workspaceBaseline=''
$originalHash=''
$restorationFailures=New-Object System.Collections.Generic.List[string]
$targetFiles=@()
$backup=@{}
$journal=Join-Path $sec 'turbo_os_activation.json'
function Snapshot([string]$p) {return [string](Get-Acl -LiteralPath $p -Audit -ErrorAction Stop).Sddl}
function Restore([string]$p,[string]$sddl) {
    # Change ONLY DACL here; the snapshot's SACL will be restored by the
    # native LABEL_SECURITY_INFORMATION path to avoid manufacturing S:PAI.
    $acl=Get-Acl -LiteralPath $p -ErrorAction Stop
    $acl.SetSecurityDescriptorSddlForm($sddl,[Security.AccessControl.AccessControlSections]::Access)
    Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
    [TurboBCiFullRestore]::RestoreLabel($p,$sddl)
    # Do not reset D:AI unconditionally; Windows may discard an original
    # auto-inherited DACL flag on an unnecessary SetFileSecurity DACL call.
    $current=[string](Get-Acl -LiteralPath $p -Audit -ErrorAction Stop).Sddl
    if ($current -cne $sddl) {
        Write-Host ('P3D2_CI_AFTER_SACL_RESTORE: '+$current)
    }
}
if (-not ('TurboBCiFullRestore' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class TurboBCiFullRestore {
 [StructLayout(LayoutKind.Sequential)]
 struct LUID { public UInt32 LowPart; public Int32 HighPart; }
 [StructLayout(LayoutKind.Sequential)]
 struct TOKEN_PRIVILEGES {public UInt32 PrivilegeCount;public LUID Luid;public UInt32 Attributes;}
 [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool CloseHandle(IntPtr h);
 [DllImport("advapi32.dll",SetLastError=true)]
 static extern bool OpenProcessToken(IntPtr process,UInt32 access,out IntPtr token);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern bool LookupPrivilegeValueW(string system,string name,out LUID luid);
 [DllImport("advapi32.dll",SetLastError=true)]
 static extern bool AdjustTokenPrivileges(IntPtr token,bool disableAll,
      ref TOKEN_PRIVILEGES privileges,UInt32 bufferLength,IntPtr previous,IntPtr returned);
 static void EnableSecurityPrivilege() {
   IntPtr token;
   if(!OpenProcessToken(GetCurrentProcess(),0x20u|0x8u,out token))
       throw new Win32Exception(Marshal.GetLastWin32Error(),"B_CI_OPEN_TOKEN_FAILED");
   try {
     LUID luid;
     if(!LookupPrivilegeValueW(null,"SeSecurityPrivilege",out luid))
       throw new Win32Exception(Marshal.GetLastWin32Error(),"B_CI_PRIVILEGE_LOOKUP_FAILED");
     TOKEN_PRIVILEGES tp=new TOKEN_PRIVILEGES();
     tp.PrivilegeCount=1u;tp.Luid=luid;tp.Attributes=2u;
     if(!AdjustTokenPrivileges(token,false,ref tp,0u,IntPtr.Zero,IntPtr.Zero))
       throw new Win32Exception(Marshal.GetLastWin32Error(),"B_CI_PRIVILEGE_ENABLE_FAILED");
     int result=Marshal.GetLastWin32Error();
     if(result==1300) throw new InvalidOperationException("B_CI_SE_SECURITY_PRIVILEGE_NOT_ASSIGNED");
   } finally {CloseHandle(token);}
 }
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(
     string sddl,uint revision,out IntPtr sd,out uint bytes);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern bool SetFileSecurityW(string path,uint info,IntPtr sd);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern UInt32 SetNamedSecurityInfoW(string objectName,int objectType,
     UInt32 information,IntPtr owner,IntPtr group,IntPtr dacl,IntPtr sacl);
 [DllImport("kernel32.dll",SetLastError=true)]
 static extern IntPtr LocalFree(IntPtr sd);
 public static void RestoreLabel(string path,string sddl) {
     EnableSecurityPrivilege();
     IntPtr sd;uint n;
     if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,out sd,out n))
         throw new Win32Exception(Marshal.GetLastWin32Error(),"B_CI_SDDL_PARSE_FAILED");
     try {
       // LABEL_SECURITY_INFORMATION changes the Mandatory Integrity ACE
       // without replacing the whole audit SACL or manufacturing S:PAI.
       if(!SetFileSecurityW(path,16u,sd)) {
         int code=Marshal.GetLastWin32Error();
         throw new Win32Exception(code,"B_CI_LABEL_RESTORE_FAILED_WIN32_"+code.ToString());
       }
     } finally {LocalFree(sd);}
 }
}
'@ -ErrorAction Stop
}
try {
    New-Item -ItemType Directory -Force -Path $sec,$workspace,$target | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixture '.turbo-b-ci-disposable'),'DISPOSABLE_FIXTURE')
    $sub=Join-Path $target 'case_a'
    New-Item -ItemType Directory -Path $sub | Out-Null
    $testFile=Join-Path $sub 'source_data.txt'
    [IO.File]::WriteAllText($testFile,'ORIGINAL_USER_DATA')
    $originalHash=(Get-FileHash -LiteralPath $testFile -Algorithm SHA256).Hash
    $policy=[ordered]@{
        schema='SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1'
        revision=124
        writable_workspaces=@($workspace)
        read_only_roots=@()
        denied_write_roots=@()
        turbo_roots=@($old)
        turbo_requested=$true
    }
    $mode=[ordered]@{schema='SMARTAGENT_ACL_MODE_V1';mode='on';install_root=$project}
    [IO.File]::WriteAllText((Join-Path $sec 'acl_mode.json'),($mode | ConvertTo-Json -Depth 8))
    $policyPath=Join-Path $sec 'workspace_access_policy.json'
    [IO.File]::WriteAllText($policyPath,($policy | ConvertTo-Json -Depth 8))
    & $auto -ProjectRoot $project -Action Apply | Out-Null
    Assert (Test-Path -LiteralPath $journal -PathType Leaf) 'P3D2_OLD_ACTIVATION_JOURNAL_MISSING'
    $oldRecord=Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert ($oldRecord.status -eq 'ACTIVE_PROVISIONED') 'P3D2_OLD_BROKER_NOT_PROVISIONED'
    # Existing target must have exactly its original content after switching.
    # Snapshot AFTER old activation because inherited ACL control flags can
    # be changed by parent-level grants. The transaction must not change them.
    $pre=@(& $preflight -ProjectRoot $project -Workspace $workspace -TurboRoot $target -IncludeSensitiveInventory |
        Where-Object { $_ -is [System.Collections.Specialized.OrderedDictionary] })
    Assert ($pre.Count -eq 1 -and [int]$pre[0].count -eq 3) 'P3D2_TARGET_PRECHECK_FAILED'
    foreach($entry in @($pre[0].entries)) {$backup[[string]$entry.path]=[string]$entry.sddl}
    & $native -Action Revoke -ProjectRoot $project | Out-Null
    Assert (-not (Test-Path -LiteralPath $journal)) 'P3D2_OLD_JOURNAL_SURVIVED_REVOKE'
    # Old Broker revoke must not alter the PREEXISTING target metadata.
    foreach($entry in @($pre[0].entries)) {
        Assert ((Snapshot ([string]$entry.path)) -ceq [string]$entry.sddl) ('P3D2_OLD_REVOKE_CHANGED_TARGET_SDDL:'+$entry.path)
    }
    Write-Host 'TURBO_B_P3D2_OLD_BROKER_REVOKED_AND_TARGET_UNCHANGED_PASS'
    if (-not ('SmartAgentGit47AppContainerProbe' -as [type])) {
        Add-Type -Path $csharp -ErrorAction Stop
    }
    $moniker='TurboB_CiImport_'+[Guid]::NewGuid().ToString('N')
    $package=[SmartAgentGit47AppContainerProbe]::CreateProfile($moniker)
    Assert ($package -match '^S-1-15-2-') 'P3D2_NEW_NATIVE_SID_INVALID'
    $sid=[Security.Principal.SecurityIdentifier]::new($package)
    # Preserve only the post-revocation ancestor ACL; restoring the
    # pre-revocation ACL would resurrect the old package's traverse grant.
    $workspaceBaseline=Snapshot $workspace
    $mutated=New-Object System.Collections.Generic.List[string]
    # Explicit traverse ACE for the workspace. Persist snapshot before change.
    $workspaceAcl=Get-Acl -LiteralPath $workspace -ErrorAction Stop
    $allowTraverse=[Security.AccessControl.FileSystemAccessRule]::new(
        $sid,[Security.AccessControl.FileSystemRights]::Traverse,
        [Security.AccessControl.InheritanceFlags]::None,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow)
    [void]$workspaceAcl.AddAccessRule($allowTraverse)
    Set-Acl -LiteralPath $workspace -AclObject $workspaceAcl -ErrorAction Stop
    [void]$mutated.Add($workspace)
    # Existing files FIRST, ancestor directories LAST. Low IL is mandatory
    # for a low-integrity AppContainer to write an existing Medium-IL file.
    foreach ($item in @($pre[0].entries | Sort-Object {([string]$_.path).Length} -Descending)) {
        $p=[string]$item.path
        $acl=Get-Acl -LiteralPath $p -ErrorAction Stop
        $inherit=if([bool]$item.directory) {
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
        } else {[Security.AccessControl.InheritanceFlags]::None}
        $grant=[Security.AccessControl.FileSystemAccessRule]::new(
            $sid,[Security.AccessControl.FileSystemRights]::Modify,
            $inherit,[Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($grant)
        Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop
        [void]$mutated.Add($p)
        & icacls.exe $p '/setintegritylevel' $(if([bool]$item.directory){'(OI)(CI)L'}else{'L'}) | Out-Null
        Assert ($LASTEXITCODE -eq 0) ('P3D2_LOW_IL_SETUP_FAILED:'+$p)
    }
    $newFile=Join-Path $sub 'appcontainer_written.txt'
    $safeFile=$newFile.Replace("'","''")
    $outsideFile=Join-Path $workspace 'outside_forbidden.txt'
    [IO.File]::WriteAllText($outsideFile,'OUTSIDE_UNCHANGED')
    $outsideSafe=$outsideFile.Replace("'","''")
    $script=@"
try {
    [IO.File]::WriteAllText('$safeFile','IN_NATIVE_APPCONTAINER')
    try { [IO.File]::WriteAllText('$outsideSafe','ILLEGAL');exit 41 }
    catch [System.UnauthorizedAccessException] {exit 0}
} catch {exit 42}
"@
    $proof=[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(
        $moniker,$script,$target,[uint32]30000) | ConvertFrom-Json
    Assert ($proof.sandbox_identity_verified -eq $true -and
        $proof.child_process_policy -eq 'JOB_KILL_ON_CLOSE' -and
        [int]$proof.exit_code -eq 0) 'P3D2_NATIVE_PROCESS_OR_DENIAL_FAILED'
    Assert ([IO.File]::ReadAllText($newFile) -ceq 'IN_NATIVE_APPCONTAINER') 'P3D2_EXISTING_ROOT_NATIVE_WRITE_FAILED'
    Assert ([IO.File]::ReadAllText($outsideFile) -ceq 'OUTSIDE_UNCHANGED') 'P3D2_OUTSIDE_ROOT_CHANGED'
    Write-Host 'TURBO_B_P3D2_POPULATED_TARGET_NATIVE_APPCONTAINER_WRITE_PASS'
} finally {
    # Never leave a disposable ACL broadened after a failed CI probe.
    if ($newFile -and (Test-Path -LiteralPath $newFile -PathType Leaf)) {
        Remove-Item -LiteralPath $newFile -Force -ErrorAction Stop
    }
    if ($backup.Count -gt 0 -and (Test-Path -LiteralPath $target -PathType Container)) {
        # The original Full SDDL/mandatory label restore is the required
        # gate. Failure must FAIL the CI job, never claim success.
        foreach($path in @($backup.Keys | Sort-Object Length)) {
            try {
                Restore $path $backup[$path]
                $actual=Snapshot $path
                if ($actual -cne $backup[$path]) {
                    # Disposable CI fixture only: print exact descriptor delta
                    # to diagnose mandatory-label / inherited DACL flags.
                    Write-Host ('P3D2_CI_EXPECTED_SDDL: '+$backup[$path])
                    Write-Host ('P3D2_CI_ACTUAL_SDDL: '+$actual)
                    throw ('P3D2_TARGET_EXACT_SDDL_ROLLBACK_FAILED:'+ $path)
                }
            } catch { [void]$restorationFailures.Add($path+':'+$_.Exception.Message) }
        }
        if ($workspaceBaseline) {
            try {
                Restore $workspace $workspaceBaseline
            } catch { [void]$restorationFailures.Add('WORKSPACE:'+ $_.Exception.Message) }
        }
        if ($restorationFailures.Count -gt 0) {
            throw ('P3D2_RESTORE_FAILED:' + ($restorationFailures -join '|'))
        }
        Write-Host 'TURBO_B_P3D2_FULL_SDDL_RESTORED_PASS'
    }
    if ($moniker) {
        try {[SmartAgentGit47AppContainerProbe]::DeleteProfile($moniker)}
        catch { throw 'P3D2_NEW_PACKAGE_PROFILE_DELETE_FAILED' }
    }
    if ($testFile -and (Test-Path -LiteralPath $testFile -PathType Leaf)) {
        Assert ((Get-FileHash -LiteralPath $testFile -Algorithm SHA256).Hash -eq $originalHash) 'P3D2_EXISTING_DATA_MODIFIED'
    }
    # Remove only the disposable fixture; leave it on an unsafe restore for
    # forensic inspection rather than hiding a security failure.
    if ($restorationFailures -and $restorationFailures.Count -gt 0) {
        Write-Warning ('P3D2_CI_TEMP_ROOT_RETAINED_FOR_FORENSICS:'+$fixture)
    } elseif (Test-Path -LiteralPath $fixture -PathType Container) {
        Remove-Item -LiteralPath $fixture -Recurse -Force
    }
}
Write-Host 'TURBO_B_P3D2_CI_NATIVE_SWITCH_REHEARSAL_PASS'
