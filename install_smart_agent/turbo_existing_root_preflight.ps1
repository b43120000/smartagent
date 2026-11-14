# Safe preflight for importing a PRE-EXISTING Turbo root.
# Deliberately READ-ONLY: does not change DACL/SACL/integrity, data, or the
# native AppContainer enrollment journal. Audit results are not authorization.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$TurboRoot,
    [int]$MaxEntries = 200000,
    [int]$MaxDepth = 32,
    [switch]$IncludeSensitiveInventory
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
if ($MaxEntries -lt 1 -or $MaxEntries -gt 200000 -or $MaxDepth -lt 1 -or $MaxDepth -gt 100) {
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
# Compare NTFS link counts with the complete inventory inside this root.
if (-not ('SmartAgentTurboBFileId' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
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
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool DeviceIoControl(SafeFileHandle h, uint code,
        IntPtr input, uint inputLength, [Out] byte[] output, uint outputLength,
        out uint returned, IntPtr overlapped);
    // Open the LINK OBJECT (not its target). The reparse-buffer digest also
    // detects a junction being retargeted while preserving its NTFS File ID.
    public static string InspectReparse(string path, bool directory) {
        uint flags = 0x00200000u | (directory ? 0x02000000u : 0u);
        using (var handle = CreateFileW(path, 0x80u, 0x1|0x2|0x4,
                 IntPtr.Zero, 3, flags, IntPtr.Zero)) {
            if (handle.IsInvalid) throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), "TURBO_B_REPARSE_OPEN_FAILED:"+path);
            TurboBInfo info;
            if (!GetFileInformationByHandle(handle, out info))
                throw new System.ComponentModel.Win32Exception(
                    Marshal.GetLastWin32Error(), "TURBO_B_REPARSE_ID_FAILED:"+path);
            byte[] buffer = new byte[16384];
            uint returned;
            if (!DeviceIoControl(handle, 0x000900A8u, IntPtr.Zero, 0u,
                                 buffer, (uint)buffer.Length, out returned, IntPtr.Zero))
                throw new System.ComponentModel.Win32Exception(
                    Marshal.GetLastWin32Error(), "TURBO_B_REPARSE_DATA_FAILED:"+path);
            using (var sha = SHA256.Create()) {
                byte[] digest = sha.ComputeHash(buffer, 0, (int)returned);
                return info.volume.ToString("X8")+":"+
                       info.fileIdHigh.ToString("X8")+info.fileIdLow.ToString("X8")+
                       ":"+BitConverter.ToString(digest).Replace("-","");
            }
        }
    }
    public static string Check(string path, bool directory) {
        uint links;
        return Inspect(path, directory, out links);
    }
    public static string Inspect(string path, bool directory, out uint links) {
        using(var h = CreateFileW(path, 0x80, 0x1|0x2|0x4, IntPtr.Zero, 3,
                                 directory ? 0x02000000u : 0u, IntPtr.Zero)) {
            if(h.IsInvalid) throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), "TURBO_B_FILE_ID_OPEN_FAILED:"+path);
            TurboBInfo i;
            if(!GetFileInformationByHandle(h, out i)) throw new System.ComponentModel.Win32Exception(
                Marshal.GetLastWin32Error(), "TURBO_B_FILE_ID_QUERY_FAILED:"+path);
            if(directory && i.links != 1) throw new InvalidOperationException(
                "TURBO_B_DIRECTORY_LINK_COUNT_INVALID:"+path);
            links = i.links;
            return i.volume.ToString("X8")+":"+i.fileIdHigh.ToString("X8")+i.fileIdLow.ToString("X8");
        }
    }
}
'@ -ErrorAction Stop
}
$nodes=New-Object System.Collections.Generic.List[object]
$linkGroups=@{}
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
    # A link inside an existing project is an OPAQUE inventory entry:
    # never traverse its target, inspect its target ACL or grant the Turbo SID.
    # Reparse ancestors and the Turbo Root itself remain strictly forbidden.
    $isLink=(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    if($isLink -and (Same $path $root)){
        throw ('TURBO_B_PREFLIGHT_REPARSE_ROOT:'+$path)
    }
    $isDir=$item.PSIsContainer
    if($isLink){
        $relativePath=$path.Substring($root.Length+1)
        $id=[SmartAgentTurboBFileId]::InspectReparse($path,[bool]$isDir)
        [void]$nodes.Add([pscustomobject]@{
            path=$path
            relative=$relativePath
            directory=[bool]$isDir
            reparse=$true
            identity=$id
            link_count=[uint32]1
            sddl=''
        })
        if($nodes.Count -gt $MaxEntries){throw 'TURBO_B_PREFLIGHT_ENTRY_LIMIT_EXCEEDED'}
        continue
    }
    $links=[uint32]0
    $id=[SmartAgentTurboBFileId]::Inspect($path,[bool]$isDir,[ref]$links)
    if (-not $isDir -and $links -gt 1) {
        $linkGroups[$id] = 1 + [int]$linkGroups[$id]
    }
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
        link_count=$links
        reparse=$false
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
    # Test-Path can return false for a broken junction; Get-Item examines
    # the directory entry without requiring its external target to exist.
    $node=Get-Item -LiteralPath $item.path -Force -ErrorAction Stop
    $nowIsLink=(($node.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    if($nowIsLink -ne [bool]$item.reparse){
        throw ('TURBO_B_PREFLIGHT_LINK_TYPE_CHANGED:'+$item.path)
    }
    if($nowIsLink){
        $actual=[SmartAgentTurboBFileId]::InspectReparse(
            [string]$item.path,[bool]$item.directory)
        if($actual -cne [string]$item.identity){
            throw ('TURBO_B_PREFLIGHT_LINK_RETARGETED:'+$item.path)
        }
        continue
    }
    $links=[uint32]0
    $now=[SmartAgentTurboBFileId]::Inspect([string]$item.path,[bool]$item.directory,[ref]$links)
    if ($links -ne $item.link_count -or
        ($links -gt 1 -and $linkGroups[$now] -ne $links)) {
        throw ('TURBO_B_HARDLINK_SCOPE_UNVERIFIED:'+$item.path)
    }
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
$linkCount=@($rootEntries | Where-Object {$_.reparse}).Count
Write-Host ('TURBO_B_EXISTING_ROOT_PREFLIGHT_PASS entries='+$rootEntries.Count+' opaque_links='+$linkCount)
Write-Host 'TURBO_B_IMPORT_NOT_YET_AUTHORIZED: read-only inventory complete.'
# Do not disclose owner/SACL/SDDL fields through normal interactive output.
# A future protected transaction importer may explicitly opt into this
# sensitive in-memory object and must write only to an admin-only journal.
if ($IncludeSensitiveInventory) {
    Write-Output $inventory
} else {
    Write-Output ([pscustomobject]@{
        schema='SMARTAGENT_TURBO_EXISTING_ROOT_PREFLIGHT_V1'
        import_allowed_for_mutation=$false
        advisory_only=$true
        count=$rootEntries.Count
        security_descriptors_emitted=$false
    })
}
