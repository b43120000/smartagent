// Git47 experimental native Win32 AppContainer launcher.
// This is a kernel-token proof primitive, NOT a production Turbo broker.
// There is no software fallback, inherited privileged handles or host shell.
// Do not enable turbo_effective from successful process creation alone.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class SmartAgentGit47AppContainerProbe
{
    const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    const uint CREATE_SUSPENDED = 0x00000004;
    const uint CREATE_NO_WINDOW = 0x08000000;
    const int PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009;
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    const int JobObjectExtendedLimitInformation = 9;
    const uint TOKEN_QUERY = 0x0008;
    const int TokenIsAppContainer = 29;
    const int TokenAppContainerSid = 31;
    const uint WAIT_OBJECT_0 = 0x00000000;
    const uint WAIT_TIMEOUT = 0x00000102;

    [StructLayout(LayoutKind.Sequential)]
    struct STARTUPINFO
    {
        public int cb;
        public IntPtr lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars;
        public int dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct STARTUPINFOEX
    {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public uint dwProcessId, dwThreadId;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct SECURITY_CAPABILITIES
    {
        public IntPtr AppContainerSid;
        public IntPtr Capabilities;
        public uint CapabilityCount;
        public uint Reserved;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("userenv.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
    static extern uint DeriveAppContainerSidFromAppContainerName(
        string name, out IntPtr sid);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
    static extern uint CreateAppContainerProfile(
        string name, string displayName, string description,
        IntPtr capabilities, uint capabilityCount, out IntPtr sid);
    [DllImport("advapi32.dll")]
    static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool ConvertSidToStringSidW(IntPtr sid, out IntPtr stringSid);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr memory);

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool InitializeProcThreadAttributeList(
        IntPtr attributeList, int attributeCount, int flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool UpdateProcThreadAttribute(
        IntPtr attributeList, uint flags, IntPtr attribute,
        ref SECURITY_CAPABILITIES value, IntPtr size,
        IntPtr previous, IntPtr returnedSize);
    [DllImport("kernel32.dll")]
    static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcessW(
        string applicationName, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes,
        bool inheritHandles, uint creationFlags,
        IntPtr environment, string workingDirectory,
        ref STARTUPINFOEX startup, out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr jobAttributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(
        IntPtr job, int informationClass,
        ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool OpenProcessToken(
        IntPtr process, uint desiredAccess, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool GetTokenInformation(
        IntPtr token, int informationClass,
        IntPtr buffer, int length, out int returned);

    static Exception Last(string operation)
    {
        return new Win32Exception(Marshal.GetLastWin32Error(), operation);
    }
    static string SidToString(IntPtr sid)
    {
        IntPtr text = IntPtr.Zero;
        if (!ConvertSidToStringSidW(sid, out text))
            throw Last("ConvertSidToStringSidW");
        try { return Marshal.PtrToStringUni(text); }
        finally { LocalFree(text); }
    }
    public static string CreateProfile(string name)
    {
        if (string.IsNullOrWhiteSpace(name) || name.Length > 64)
            throw new ArgumentException("invalid_moniker");
        IntPtr sid = IntPtr.Zero;
        uint hr = CreateAppContainerProfile(
            name, "SmartAgent Turbo " + name,
            "Scoped disposable SmartAgent AppContainer", IntPtr.Zero, 0, out sid);
        // ERROR_ALREADY_EXISTS is expected when reinstalling idempotently.
        if (hr != 0 && hr != 0x800700B7u)
            throw new InvalidOperationException("appcontainer_create_profile_hresult:" + hr.ToString("X8"));
        if (sid != IntPtr.Zero) FreeSid(sid);
        return DeriveSid(name);
    }
    public static string DeriveSid(string name)
    {
        IntPtr sid = IntPtr.Zero;
        uint hr = DeriveAppContainerSidFromAppContainerName(name, out sid);
        if (hr != 0 || sid == IntPtr.Zero)
            throw new InvalidOperationException("appcontainer_sid_derivation_failed:" + hr.ToString("X8"));
        try { return SidToString(sid); }
        finally { FreeSid(sid); }
    }
    static void VerifyChildToken(IntPtr process, string expectedSid)
    {
        IntPtr token = IntPtr.Zero;
        if (!OpenProcessToken(process, TOKEN_QUERY, out token))
            throw Last("OpenProcessToken");
        try
        {
            IntPtr isContainer = Marshal.AllocHGlobal(4);
            try
            {
                int size;
                if (!GetTokenInformation(token, TokenIsAppContainer, isContainer, 4, out size)
                    || Marshal.ReadInt32(isContainer) != 1)
                    throw new InvalidOperationException("child_token_is_not_appcontainer");
            }
            finally { Marshal.FreeHGlobal(isContainer); }

            int sizeRequired = IntPtr.Size;
            IntPtr data = Marshal.AllocHGlobal(IntPtr.Size);
            try
            {
                int written;
                if (!GetTokenInformation(token, TokenAppContainerSid,
                    data, sizeRequired, out written))
                    throw Last("GetTokenInformation(TokenAppContainerSid)");
                IntPtr childSid = Marshal.ReadIntPtr(data);
                if (childSid == IntPtr.Zero
                    || !string.Equals(SidToString(childSid), expectedSid,
                                      StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("appcontainer_child_sid_mismatch");
            }
            finally { Marshal.FreeHGlobal(data); }
        }
        finally { CloseHandle(token); }
    }

    /// <summary>Runs ONE explicit executable under the derived package SID.
    /// Returns a structured process receipt; kernel containment still requires
    /// independent ACL and negative-file-access attestation.</summary>
    public static string Launch(string moniker, string executable,
                                string arguments, string cwd,
                                uint timeoutMilliseconds)
    {
        if (string.IsNullOrWhiteSpace(moniker)
            || string.IsNullOrWhiteSpace(executable)
            || !System.IO.Path.IsPathRooted(executable)
            || !System.IO.Directory.Exists(cwd)
            || timeoutMilliseconds == 0 || timeoutMilliseconds > 300000)
            throw new ArgumentException("launch_preflight_invalid");
        string sidString = DeriveSid(moniker);
        IntPtr sid = IntPtr.Zero, attributeList = IntPtr.Zero;
        IntPtr job = IntPtr.Zero;
        PROCESS_INFORMATION pi = new PROCESS_INFORMATION();
        bool started = false;
        bool listInitialized = false;
        try
        {
            uint hresult = DeriveAppContainerSidFromAppContainerName(moniker, out sid);
            if (hresult != 0 || sid == IntPtr.Zero)
                throw new InvalidOperationException("profile_sid_unavailable");

            IntPtr attrSize = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref attrSize);
            if (attrSize == IntPtr.Zero)
                throw new InvalidOperationException("attribute_size_unavailable");
            attributeList = Marshal.AllocHGlobal(attrSize);
            if (!InitializeProcThreadAttributeList(attributeList, 1, 0, ref attrSize))
                throw Last("InitializeProcThreadAttributeList");
            listInitialized = true;
            SECURITY_CAPABILITIES caps = new SECURITY_CAPABILITIES {
                AppContainerSid = sid, Capabilities = IntPtr.Zero,
                CapabilityCount = 0, Reserved = 0
            };
            if (!UpdateProcThreadAttribute(attributeList, 0,
                (IntPtr)PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ref caps, (IntPtr)Marshal.SizeOf(typeof(SECURITY_CAPABILITIES)),
                IntPtr.Zero, IntPtr.Zero))
                throw Last("UpdateProcThreadAttribute(SecurityCapabilities)");

            STARTUPINFOEX si = new STARTUPINFOEX();
            si.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            si.lpAttributeList = attributeList;
            StringBuilder command = new StringBuilder(
                "\"" + executable.Replace("\"", "") + "\" " + (arguments ?? ""));
            if (!CreateProcessW(executable, command,
                IntPtr.Zero, IntPtr.Zero, false,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW,
                IntPtr.Zero, cwd, ref si, out pi))
                throw Last("CreateProcessW(AppContainer)");
            started = true;

            job = CreateJobObjectW(IntPtr.Zero, null);
            if (job == IntPtr.Zero) throw Last("CreateJobObjectW");
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                ref limits, Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))))
                throw Last("SetInformationJobObject");
            if (!AssignProcessToJobObject(job, pi.hProcess))
                throw Last("AssignProcessToJobObject");
            VerifyChildToken(pi.hProcess, sidString);
            if (ResumeThread(pi.hThread) == 0xFFFFFFFFu)
                throw Last("ResumeThread");
            uint wait = WaitForSingleObject(pi.hProcess, timeoutMilliseconds);
            if (wait == WAIT_TIMEOUT)
            {
                TerminateJobObject(job, 124);
                throw new TimeoutException("appcontainer_execution_timeout");
            }
            if (wait != WAIT_OBJECT_0)
                throw Last("WaitForSingleObject");
            uint exitCode;
            if (!GetExitCodeProcess(pi.hProcess, out exitCode))
                throw Last("GetExitCodeProcess");
            return "{\"schema\":\"SMARTAGENT_GIT47_APP_CONTAINER_PROCESS_V1\","
                + "\"sandbox_backend\":\"WIN32_APPCONTAINER\","
                + "\"sandbox_identity_verified\":true,"
                + "\"child_process_policy\":\"JOB_KILL_ON_CLOSE\","
                + "\"os_process_started\":true,"
                + "\"exit_code\":" + exitCode + ","
                + "\"pid\":" + pi.dwProcessId + "}";
        }
        catch
        {
            if (started && job != IntPtr.Zero) TerminateJobObject(job, 125);
            else if (started) { /* suspended child must be terminated */
                if (pi.hProcess != IntPtr.Zero) {
                    // Never resume a child that failed attestation.
                    TerminateProcess(pi.hProcess, 125);
                }
            }
            throw;
        }
        finally
        {
            if (pi.hThread != IntPtr.Zero) CloseHandle(pi.hThread);
            if (pi.hProcess != IntPtr.Zero) CloseHandle(pi.hProcess);
            if (job != IntPtr.Zero) CloseHandle(job);
            if (listInitialized) DeleteProcThreadAttributeList(attributeList);
            if (attributeList != IntPtr.Zero) Marshal.FreeHGlobal(attributeList);
            if (sid != IntPtr.Zero) FreeSid(sid);
        }
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateProcess(IntPtr process, uint exitCode);
}
