param(
    [switch]$SelfTest,
    [int]$DelaySeconds = 2
)

$ErrorActionPreference = 'Stop'
# The visible desktop app is ChatGPT.exe / "ChatGPT".  Its current Store
# package keeps the historical OpenAI.Codex AppUserModelID.
$ChatGPTAppId = 'OpenAI.Codex_2p2nqsd0c76g0!App'
$ChatGPTPackagePrefix = 'OpenAI.Codex_'
$LogPath = Join-Path $env:TEMP 'smartagent_restart_chatgpt.log'

function Write-RestartLog([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Get-ChatGPTDesktopProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'" | Where-Object {
        $path = [string]$_.ExecutablePath
        $command = [string]$_.CommandLine
        $path -like "*\WindowsApps\$ChatGPTPackagePrefix*\app\ChatGPT.exe" -or
        $command -like "*\WindowsApps\$ChatGPTPackagePrefix*\app\ChatGPT.exe*"
    })
}

if ($SelfTest) {
    $app = Get-StartApps | Where-Object { $_.AppID -eq $ChatGPTAppId } | Select-Object -First 1
    if ($null -eq $app) {
        throw "chatgpt_app_id_not_found:$ChatGPTAppId"
    }
    $processes = @(Get-ChatGPTDesktopProcesses)
    [pscustomobject]@{
        status = 'RESTART_CHATGPT_SELF_TEST_OK'
        display_name = [string]$app.Name
        app_id = $ChatGPTAppId
        current_process_count = $processes.Count
        log_path = $LogPath
    } | ConvertTo-Json -Compress
    exit 0
}

Write-RestartLog "Restart requested; delay_seconds=$DelaySeconds"
Start-Sleep -Seconds ([Math]::Max(1, $DelaySeconds))

$processes = @(Get-ChatGPTDesktopProcesses)
$main = @($processes | Where-Object { [string]$_.CommandLine -notmatch '\s--type=' })
foreach ($item in $main) {
    try {
        $process = Get-Process -Id $item.ProcessId -ErrorAction Stop
        [void]$process.CloseMainWindow()
    }
    catch {
        Write-RestartLog "Graceful close failed pid=$($item.ProcessId): $($_.Exception.Message)"
    }
}

$deadline = (Get-Date).AddSeconds(5)
do {
    Start-Sleep -Milliseconds 250
    $remaining = @(Get-ChatGPTDesktopProcesses)
} while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)

foreach ($item in $remaining) {
    try {
        Stop-Process -Id $item.ProcessId -Force -ErrorAction Stop
    }
    catch {
        Write-RestartLog "Forced close failed pid=$($item.ProcessId): $($_.Exception.Message)"
    }
}

Start-Sleep -Seconds 1
Start-Process -FilePath 'explorer.exe' -ArgumentList "shell:AppsFolder\$ChatGPTAppId"
Write-RestartLog "ChatGPT launch requested through AppUserModelID=$ChatGPTAppId"

