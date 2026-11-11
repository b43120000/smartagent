# Safe preflight for importing a PRE-EXISTING Turbo root.
# Deliberately READ-ONLY: does not change DACL/SACL/integrity, data, or the
# native AppContainer enrollment journal. Audit results are not authorization.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TurboRoot,
    [int]$MaxEntries = 5000,
    [int]$MaxDepth = 32
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Full([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'TURBO_B_PREFLIGHT_EMPTY_PATH' }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}
function Same([string]$A,[string]$B) {
    return (Full $A).Equals((Full $B),[StringComparison]::OrdinalIgnoreCase)
}
function Under([string]$Child,[string]$Parent) {
    return (Full $Child).StartsWith((Full $Parent)+'\',
        [StringComparison]::OrdinalIgnoreCase)
}
$project=Full $ProjectRoot
$workspace=Full $Workspace
$root=Full $TurboRoot
if ($MaxEntries -lt 1 -or $MaxEntries -gt 100000 -or $MaxDepth -lt 1 -or $MaxDepth -gt 100) {
    throw 'TURBO_B_PREFLIGHT_LIMITS_INVALID'
}
if (-not (Under $root $workspace) -or (Under $root $project) -or
    (Under $workspace $project) -or (Under $project $workspace)) {
    throw 'TURBO_B_PREFLIGHT_SCOPE_VIOLATION'
}
foreach ($path in @($project,$workspace,$root)) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw ('TURBO_B_PREFLIGHT_DIRECTORY_MISSING:'+$path)
    }
}
if (([IO.DriveInfo]::new([IO.Path]::GetPathRoot($root))).DriveFormat -ne 'NTFS') {
    throw 'TURBO_B_PREFLIGHT_NTFS_REQUIRED'
}
$rootDrive=[IO.Path]::GetPathRoot($root)
if ($rootDrive -ne [IO.Path]::GetPathRoot($workspace)) {
    throw 'TURBO_B_PREFLIGHT_CROSS_VOLUME'
}
# Never traverse reparse ancestors, even inside the registered writable root.
$ancestor=$root
while ($true) {
    $entry=Get-Item -LiteralPath $ancestor -Force -ErrorAction Stop
    if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ('TURBO_B_PREFLIGHT_REPARSE_ANCESTOR:'+$ancestor)
    }
    if (Same $ancestor $workspace) { break }
    $ancestor=[IO.Path]::GetDirectoryName($ancestor)
    if (-not $ancestor -or -not (Under $ancestor $workspace) -and
        -not (Same $ancestor $workspace)) { throw 'TURBO_B_PREFLIGHT_PARENT_ESCAPE' }
}
# The Windows API exposes nNumberOfLinks and stable per-volume file IDs.
# A hardlinked regular file must NEVER be silently granted an AppContainer ACE.
if (-not ('SmartAgentTurboBFileId' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
[StructLayout(LayoutKind.Sequential)]
public struct TurboBFileTime { public uint lo; public uint hi; }
[StructLayout(LayoutKind.Sequential)]
public struct TurboBInfo {
    public uint attributes;
    public TurboBFileTime creation;
    public TurboBFileTime access;
    public TurboBFileTime write;
    public uint volume;
    public uint sizeHigh;
    public uint sizeLow;
    public uint links;
    public uint fileIdHigh;
    public uint fileIdLow;
}
public static class SmartAgentTurboBFileId {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle h, out TurboBInfo info);
    public static string Check(string path, bool directory) {
        using(var h = CreateFileW(path, 0x80, 0x1|0x2|0x4, IntPtr.Zero, 3,
                                 directory ? 0x02000000u : 0u, IntPtr.Zero)) {
            if(h.IsInvalid) throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), "TURBO_B_FILE_ID_OPEN_FAILED:"+path);
            TurboBInfo i;
            if(!GetFileInformationByHandle(h, out i)) throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), "TURBO_B_FILE_ID_QUERY_FAILED:"+path);
            if(i.links != 1) throw new InvalidOperationException(
                "TURBO_B_HARDLINK_FORBIDDEN:"+path+":links="+i.links.ToString());
            return i.volume.ToString("X8")+":"+i.fileIdHigh.ToString("X8")+i.fileIdLow.ToString("X8");
        }
    }
}
'@ -ErrorAction Stop
}
$nodes=New-Object System.Collections.Generic.List[object]
$pending=New-Object System.Collections.Generic.Queue[object]
$pending.Enqueue([pscustomobject]@{path=$root;depth=0})
while ($pending.Count -gt 0) {
    $entry=$pending.Dequeue()
    $path=[string]$entry.path
    $depth=[int]$entry.depth
    if ($depth -gt $MaxDepth) { throw 'TURBO_B_PREFLIGHT_MAX_DEPTH_EXCEEDED' }
    if (-not ((Same $path $root) -or (Under $path $root))) {
        throw ('TURBO_B_PREFLIGHT_ENTRY_OUTSIDE_ROOT:'+$path)
    }
    $item=Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ('TURBO_B_PREFLIGHT_REPARSE_ENTRY:'+$path)
    }
    $isDir=$item.PSIsContainer
    $id=[SmartAgentTurboBFileId]::Check($path,[bool]$isDir)
    # A full security snapshot must be readable by the CURRENT administrator.
    # '-Audit' is required to obtain labels/SACL rather than DACL-only backup.
    try {
        $acl=Get-Acl -LiteralPath $path -Audit -ErrorAction Stop
        $sddl=[string]$acl.Sddl
    } catch { throw ('TURBO_B_PREFLIGHT_FULL_SECURITY_UNREADABLE:'+$path+':'+$_.Exception.Message) }
    if ($sddl -notmatch '^O:.+G:.+D:') {
        throw ('TURBO_B_PREFLIGHT_SECURITY_DESCRIPTOR_INCOMPLETE:'+$path)
    }
    if ([string]::IsNullOrEmpty($sddl)) {
        throw ('TURBO_B_PREFLIGHT_EMPTY_SDDL:'+$path)
    }
    $relativePath='.'
    if (-not (Same $path $root)) { $relativePath=$path.Substring($root.Length+1) }
    [void]$nodes.Add([pscustomobject]@{
        path=$path
        relative=$relativePath
        directory=[bool]$isDir
        identity=$id
        sddl=$sddl
    })
    if ($nodes.Count -gt $MaxEntries) { throw 'TURBO_B_PREFLIGHT_ENTRY_LIMIT_EXCEEDED' }
    if ($isDir) {
        foreach ($child in [IO.Directory]::EnumerateFileSystemEntries($path)) {
            $pending.Enqueue([pscustomobject]@{path=$child;depth=($depth+1)})
        }
    }
}
# Detect concurrent addition/deletion during enumeration before any mutation.
foreach ($item in $nodes.ToArray()) {
    if (-not (Test-Path -LiteralPath $item.path)) {
        throw ('TURBO_B_PREFLIGHT_TREE_CHANGED:'+$item.path)
    }
    $now=[SmartAgentTurboBFileId]::Check([string]$item.path,[bool]$item.directory)
    if ($now -cne [string]$item.identity) {
        throw ('TURBO_B_PREFLIGHT_FILE_REPLACED:'+$item.path)
    }
}
$rootEntries=@($nodes | Sort-Object path)
$inventory=[ordered]@{
    schema='SMARTAGENT_TURBO_EXISTING_ROOT_PREFLIGHT_V1'
    import_allowed_for_mutation=$false
    advisory_only=$true
    project_root=$project
    workspace=$workspace
    turbo_root=$root
    count=$rootEntries.Count
    entries=$rootEntries
}
# Full SDDL contains sensitive ACL/identity information. Do NOT print it.
Write-Host ('TURBO_B_EXISTING_ROOT_PREFLIGHT_PASS entries='+$rootEntries.Count)
Write-Host 'TURBO_B_IMPORT_NOT_YET_AUTHORIZED: read-only inventory complete.'
# PS object for a future privileged, protected transactional importer.
Write-Output $inventory
