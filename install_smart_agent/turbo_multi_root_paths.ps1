# Git62 root-index path helper for elevated Native Broker enrollment scripts.
# An index identifies a root; it is NOT a security credential or grant.
function Get-TurboRootKey([string]$Root) {
    if (-not [IO.Path]::IsPathRooted($Root)) {
        throw 'GIT62_NATIVE_ABSOLUTE_ROOT_REQUIRED'
    }
    $normalized=[IO.Path]::GetFullPath($Root).TrimEnd('\').ToUpperInvariant()
    $hash=[Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hash.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($normalized))).Replace('-','').ToLowerInvariant()
    } finally {$hash.Dispose()}
}
function Get-TurboRootJournalPaths([string]$SecurityRoot,[string]$Root) {
    $key=Get-TurboRootKey $Root
    $folder=Join-Path $SecurityRoot 'turbo_root_activations'
    return [pscustomobject]@{
        directory=$folder
        activation=(Join-Path $folder ('activation-'+$key+'.json'))
        import=(Join-Path $folder ('import-'+$key+'.json'))
        key=$key
    }
}
function Assert-TurboRootJournalDirectory([string]$SecurityRoot) {
    $folder=Join-Path $SecurityRoot 'turbo_root_activations'
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
        New-Item -ItemType Directory -Path $folder -ErrorAction Stop | Out-Null
    }
    if ((Get-Item -LiteralPath $folder -Force -ErrorAction Stop).Attributes -band
        [IO.FileAttributes]::ReparsePoint) {
        throw 'GIT62_NATIVE_JOURNAL_DIR_REPARSE'
    }
    & icacls.exe $folder '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) {throw 'GIT62_NATIVE_JOURNAL_DIR_ACL_FAILED'}
    $acl=Get-Acl -LiteralPath $folder -ErrorAction Stop
    foreach($rule in @($acl.Access)) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin @('S-1-5-18','S-1-5-32-544')) {
                throw 'GIT62_NATIVE_JOURNAL_DIR_UNTRUSTED_ACE'
            }
        }
    }
}

function Set-TurboNegativeSentinel([string]$Path, [object]$Mode, [bool]$Repair) {
    $controller=[string]$Mode.controller_sid
    if($controller -notmatch '^S-1-(?:5-21|12-1)(?:-[0-9]+){4}$'){
        throw 'GIT62_SENTINEL_CONTROLLER_SID_INVALID'
    }
    if(-not ('SmartAgentTurboNegativeSentinel' -as [type])){
        Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;

public static class SmartAgentTurboNegativeSentinel {
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInformation {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFile(string name, uint access,
        uint share, IntPtr security, uint mode, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file,
        out FileInformation info);

    private static FileInformation Inspect(SafeFileHandle handle) {
        FileInformation info;
        if (!GetFileInformationByHandle(handle, out info))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return info;
    }

    private static bool HasSentinelDacl(FileSecurity current, FileSecurity desired) {
        if (!current.AreAccessRulesProtected)
            return false;
        var actualAcl = new RawSecurityDescriptor(
            current.GetSecurityDescriptorBinaryForm(), 0).DiscretionaryAcl;
        var requiredAcl = new RawSecurityDescriptor(
            desired.GetSecurityDescriptorBinaryForm(), 0).DiscretionaryAcl;
        if (actualAcl == null || actualAcl.Count != requiredAcl.Count)
            return false;
        // NTFS may reorder allow entries or add an auto-inheritance control
        // flag when replacing a protected DACL. Compare the actual ACEs.
        var required = new HashSet<string>();
        foreach (CommonAce ace in requiredAcl)
            required.Add(ace.SecurityIdentifier.Value + ":" + ace.AccessMask);
        foreach (GenericAce rule in actualAcl) {
            var ace = rule as CommonAce;
            if (ace == null || ace.AceFlags != AceFlags.None || ace.IsCallback ||
                ace.AceQualifier != AceQualifier.AccessAllowed ||
                !required.Remove(ace.SecurityIdentifier.Value + ":" + ace.AccessMask))
                return false;
        }
        return required.Count == 0;
    }

    private static void PinParents(string directory, List<SafeFileHandle> handles) {
        string parent = Path.GetDirectoryName(directory);
        if (parent == null)
            return;
        PinParents(parent, handles);
        // Exclude delete sharing so each verified directory remains in place
        // until the file handle is opened and the DACL is committed.
        const uint readAttributes = 0x80;
        const uint openNoFollowDirectory = 0x02200000;
        SafeFileHandle handle = CreateFile(directory, readAttributes, 3,
            IntPtr.Zero, 3, openNoFollowDirectory, IntPtr.Zero);
        if (handle.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        handles.Add(handle);
        if ((Inspect(handle).Attributes & 0x410) != 0x10)
            throw new IOException("GIT62_REBIND_SENTINEL_PARENT_REPARSE");
    }

    public static string Protect(string path, string controller, bool repair) {
        var parents = new List<SafeFileHandle>();
        try {
            PinParents(Path.GetDirectoryName(path), parents);
            return ProtectUnderPinnedParents(path, controller, repair);
        } finally {
            for (int i = parents.Count - 1; i >= 0; i--)
                parents[i].Dispose();
        }
    }

    private static string ProtectUnderPinnedParents(string path, string controller, bool repair) {
        var acl = new FileSecurity();
        acl.SetAccessRuleProtection(true, false);
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-18"),
            FileSystemRights.FullControl, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-544"),
            FileSystemRights.FullControl, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(controller),
            FileSystemRights.Read, AccessControlType.Allow));
        byte[] expected = System.Text.Encoding.ASCII.GetBytes("OUTSIDE_SENTINEL");
        if (repair) {
            // Pin the directory entry without following links or allowing replacement
            // while reading the marker or applying its DACL.
            const uint readAndChangeAcl = 0x80040000;
            const uint openReparsePoint = 0x00200000;
            using (SafeFileHandle handle = CreateFile(path, readAndChangeAcl,
                    1, IntPtr.Zero, 3, openReparsePoint, IntPtr.Zero)) {
                if (!handle.IsInvalid) {
                    FileInformation info = Inspect(handle);
                    if ((info.Attributes & 0x410) != 0 || info.Links != 1 ||
                        info.SizeHigh != 0 || info.SizeLow != expected.Length)
                        throw new IOException("GIT62_REBIND_SENTINEL_UNTRUSTED");
                    using (var stream = new FileStream(handle, FileAccess.Read)) {
                        foreach (byte value in expected) {
                            if (stream.ReadByte() != value)
                                throw new IOException("GIT62_REBIND_SENTINEL_UNTRUSTED");
                        }
                        if (HasSentinelDacl(stream.GetAccessControl(), acl))
                            return "UNCHANGED";
                        stream.SetAccessControl(acl);
                        if (!HasSentinelDacl(stream.GetAccessControl(), acl))
                            throw new IOException("GIT62_REBIND_SENTINEL_PROTECTION_FAILED");
                    }
                    return "ACL_REPAIRED";
                }
                int error = Marshal.GetLastWin32Error();
                if (error != 2)
                    throw new Win32Exception(error);
            }
        }
        using (var stream = new FileStream(path, FileMode.CreateNew,
                FileSystemRights.WriteData, FileShare.None, 4096, FileOptions.None, acl)) {
            stream.Write(expected, 0, expected.Length);
            stream.Flush(true);
        }
        return "CREATED";
    }
}
'@
    }
    [SmartAgentTurboNegativeSentinel]::Protect($Path,$controller,$Repair)
}
