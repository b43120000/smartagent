[CmdletBinding()]
param(
    [string]$WorkspaceRoot = $env:TEMP
)
# Git46 DISCOVERY ONLY: no token creation, process execution, DACL mutation,
# filesystem mutation, or claim that any executable works in AppContainer.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$native = @'
using System;
using System.Runtime.InteropServices;
public static class Git46SandboxApiProbe {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr LoadLibraryEx(string name, IntPtr file, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi, SetLastError=true)]
    public static extern IntPtr GetProcAddress(IntPtr library, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool FreeLibrary(IntPtr library);
}
'@
Add-Type -TypeDefinition $native -ErrorAction Stop
$processmodel = [Git46SandboxApiProbe]::LoadLibraryEx('processmodel.dll', [IntPtr]::Zero, 0x800)
$experimentalPresent = $false
try {
    if ($processmodel -ne [IntPtr]::Zero) {
        $symbol = [Git46SandboxApiProbe]::GetProcAddress(
            $processmodel, 'Experimental_CreateProcessInSandbox')
        $experimentalPresent = $symbol -ne [IntPtr]::Zero
    }
} finally {
    if ($processmodel -ne [IntPtr]::Zero) {
        [void][Git46SandboxApiProbe]::FreeLibrary($processmodel)
    }
}

$tools = [ordered]@{}
foreach ($tool in @('python.exe','powershell.exe','cmd.exe','git.exe','cl.exe')) {
    $resolved = Get-Command $tool -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $tools[$tool] = $null -ne $resolved
}

$filesystem = 'UNKNOWN'
if (-not [string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $drive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($WorkspaceRoot))
    if ($drive -match '^[a-zA-Z]:\\$') {
        try {
            $volume = Get-Volume -DriveLetter $drive.Substring(0,1) -ErrorAction Stop
            $filesystem = [string]$volume.FileSystem
        } catch {
            $filesystem = 'UNAVAILABLE'
        }
    }
}
[ordered]@{
    schema = 'SMARTAGENT_GIT46_WINDOWS_API_DISCOVERY_V1'
    discovery_only = $true
    sandbox_status = 'NOT_READY'
    sandbox_attestation_passed = $false
    windows_os_access_denied_proven = $false
    windows_version = [Environment]::OSVersion.Version.ToString()
    windows_nt = [Environment]::OSVersion.Platform -eq 'Win32NT'
    filesystem = $filesystem
    experimental_createprocessinsandbox_export_present = $experimentalPresent
    classic_appcontainer_launch_proven = $false
    child_process_isolation_proven = $false
    tool_executable_present = $tools
    reason_code = 'WINDOWS_SANDBOX_EXECUTION_NOT_TESTED'
} | ConvertTo-Json -Depth 6
