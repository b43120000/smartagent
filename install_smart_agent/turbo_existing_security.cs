using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

// Trusted elevated setup/recovery only. No untrusted child code may call this.
// LABEL_SECURITY_INFORMATION changes the mandatory-integrity ACE without
// replacing the audit SACL or introducing the spurious S:PAI flag observed
// when SetFileSecurityW(SACL_SECURITY_INFORMATION) is used.
public static class SmartAgentTurboExistingSecurity {
    [StructLayout(LayoutKind.Sequential)] struct LUID {
        public UInt32 LowPart; public Int32 HighPart;
    }
    [StructLayout(LayoutKind.Sequential)] struct TOKEN_PRIVILEGES {
        public UInt32 PrivilegeCount; public LUID Luid; public UInt32 Attributes;
    }
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr h);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool OpenProcessToken(IntPtr process, UInt32 access, out IntPtr token);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool LookupPrivilegeValueW(string system, string name, out LUID luid);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool AdjustTokenPrivileges(IntPtr token, bool disableAll,
        ref TOKEN_PRIVILEGES privileges, UInt32 bufferLength,
        IntPtr previous, IntPtr returned);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(
        string sddl, uint revision, out IntPtr sd, out uint bytes);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool SetFileSecurityW(string path, uint info, IntPtr sd);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr LocalFree(IntPtr sd);

    public static void EnableSecurityPrivilege() {
        IntPtr token;
        if (!OpenProcessToken(GetCurrentProcess(), 0x20u | 0x8u, out token))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "TURBO_EXISTING_OPEN_TOKEN");
        try {
            LUID luid;
            if (!LookupPrivilegeValueW(null, "SeSecurityPrivilege", out luid))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "TURBO_EXISTING_PRIVILEGE_LOOKUP");
            TOKEN_PRIVILEGES tp = new TOKEN_PRIVILEGES();
            tp.PrivilegeCount=1u; tp.Luid=luid; tp.Attributes=2u;
            if (!AdjustTokenPrivileges(token, false, ref tp, 0u, IntPtr.Zero, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "TURBO_EXISTING_ENABLE_PRIVILEGE");
            int status=Marshal.GetLastWin32Error();
            if (status == 1300)
                throw new InvalidOperationException("TURBO_EXISTING_SE_SECURITY_PRIVILEGE_NOT_ASSIGNED");
        } finally {CloseHandle(token);}
    }
    public static void RestoreMandatoryLabel(string path, string originalSddl) {
        if (String.IsNullOrEmpty(path) || String.IsNullOrEmpty(originalSddl))
            throw new ArgumentException("TURBO_EXISTING_SDDL_ARGS");
        EnableSecurityPrivilege();
        IntPtr sd; uint count;
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
            originalSddl,1,out sd,out count))
            throw new Win32Exception(Marshal.GetLastWin32Error(),"TURBO_EXISTING_SDDL_PARSE");
        try {
            if (!SetFileSecurityW(path,16u,sd))
                throw new Win32Exception(Marshal.GetLastWin32Error(),"TURBO_EXISTING_RESTORE_LABEL");
        } finally {LocalFree(sd);}
    }
}
