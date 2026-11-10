// Git47 disposable fixture helper. It must execute *inside* the AppContainer.
// Reports Win32 GetLastError from direct CreateFileW, not software preflight.
using System;
using System.IO;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;

public static class SmartAgentGit47Win32FileProbe
{
    const uint GENERIC_READ = 0x80000000;
    const uint GENERIC_WRITE = 0x40000000;
    const uint FILE_SHARE_READ = 1, FILE_SHARE_WRITE = 2, FILE_SHARE_DELETE = 4;
    const uint OPEN_EXISTING = 3, OPEN_ALWAYS = 4;
    static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateFileW(string path, uint desiredAccess,
        uint sharing, IntPtr securityAttributes, uint creation,
        uint flags, IntPtr templateFile);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool DeleteFileW(string path);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool RemoveDirectoryW(string path);

    public static int Main(string[] args)
    {
        if (args.Length != 3 || (args[0] != "read" && args[0] != "write"
                && args[0] != "delete_file" && args[0] != "delete_dir"
                && args[0] != "spawn_read"))
            return 78;
        string operation = args[0], target = args[1], receipt = args[2];
        if (operation == "spawn_read")
        {
            // This runs under the *already attested* AppContainer token.
            // A child is expected to inherit that token, not the host one.
            var info = new ProcessStartInfo(
                Assembly.GetExecutingAssembly().Location,
                "read \"" + target.Replace("\"", "") + "\" \"" +
                receipt.Replace("\"", "") + "\"");
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            using (Process child = Process.Start(info))
            {
                if (child == null) return 76;
                if (!child.WaitForExit(15000))
                {
                    child.Kill();
                    return 75;
                }
                return child.ExitCode;
            }
        }
        int osError = 0;
        if (operation == "delete_file")
        {
            if (!DeleteFileW(target)) osError = Marshal.GetLastWin32Error();
        }
        else if (operation == "delete_dir")
        {
            if (!RemoveDirectoryW(target)) osError = Marshal.GetLastWin32Error();
        }
        else
        {
            uint desired = operation == "write" ? GENERIC_WRITE : GENERIC_READ;
            uint disposition = operation == "write" ? OPEN_ALWAYS : OPEN_EXISTING;
            IntPtr handle = CreateFileW(
                target, desired,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                IntPtr.Zero, disposition, 0, IntPtr.Zero);
            if (handle == INVALID_HANDLE_VALUE)
                osError = Marshal.GetLastWin32Error();
            else
                CloseHandle(handle);
        }
        // Receipt path is a prearranged disposable in-root file. The measured
        // status is the direct Windows File API error code above.
        try { File.WriteAllText(receipt, "WIN32_ERROR=" + osError); }
        catch { return 77; }
        return osError == 0 ? 0 : 10;
    }
}
