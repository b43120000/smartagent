[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$SkipOllama,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$HelperDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $HelperDir
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BundledPython = Join-Path $ProjectRoot "python_runtime\python.exe"
$ReportPath = Join-Path $HelperDir "environment_report.txt"
$script:Results = [System.Collections.Generic.List[object]]::new()

function Add-CheckResult {
    param(
        [string]$Name,
        [ValidateSet("PASS", "MISSING", "BROKEN", "SKIPPED")]
        [string]$Status,
        [string]$Detail
    )
    $script:Results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail })
}

function Invoke-PythonProbe {
    param([string]$Executable, [string[]]$Prefix = @())
    if (-not (Test-Path -LiteralPath $Executable) -and -not (Get-Command $Executable -ErrorAction SilentlyContinue)) {
        return $null
    }
    try {
        $json = & $Executable @Prefix -c "import json,platform,struct,sys; print(json.dumps({'version':platform.python_version(),'major':sys.version_info.major,'minor':sys.version_info.minor,'bits':struct.calcsize('P')*8,'executable':sys.executable}))" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($json | ConvertFrom-Json)
    } catch { return $null }
}

function Find-CompatiblePython {
    $candidates = [System.Collections.Generic.List[object]]::new()
    if (Test-Path -LiteralPath $VenvPython) {
        $candidates.Add([pscustomobject]@{ Exe = $VenvPython; Prefix = @(); Label = "SmartAgent .venv" })
    }
    if (Test-Path -LiteralPath $BundledPython) {
        $candidates.Add([pscustomobject]@{ Exe = $BundledPython; Prefix = @(); Label = "bundled runtime" })
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates.Add([pscustomobject]@{ Exe = $py.Source; Prefix = @("-3.11"); Label = "Python launcher" }) }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { $candidates.Add([pscustomobject]@{ Exe = $python.Source; Prefix = @(); Label = "PATH" }) }
    foreach ($candidate in $candidates) {
        $info = Invoke-PythonProbe -Executable $candidate.Exe -Prefix $candidate.Prefix
        if ($info -and $info.major -eq 3 -and $info.minor -ge 11 -and $info.bits -eq 64) {
            return [pscustomobject]@{ Candidate = $candidate; Info = $info }
        }
    }
    return $null
}

function Test-OllamaApi {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Write-Report {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("SmartAgent environment report")
    $lines.Add("Generated: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"))
    $lines.Add("Project: " + $ProjectRoot)
    $lines.Add("")
    foreach ($item in $script:Results) {
        $lines.Add(("[{0,-7}] {1}: {2}" -f $item.Status, $item.Name, $item.Detail))
    }
    $failed = @($script:Results | Where-Object { $_.Status -in @("MISSING", "BROKEN") })
    $lines.Add("")
    if ($failed.Count -eq 0) {
        $lines.Add("RESULT: PASS - all required environments are ready; installation is not needed.")
    } else {
        $lines.Add(("RESULT: FAIL - {0} required item(s) are missing or broken." -f $failed.Count))
    }
    $lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

    Write-Host ""
    foreach ($item in $script:Results) {
        $color = switch ($item.Status) {
            "PASS" { "Green" }
            "SKIPPED" { "Yellow" }
            default { "Red" }
        }
        Write-Host ("[{0,-7}] {1}" -f $item.Status, $item.Name) -ForegroundColor $color
        Write-Host ("          {0}" -f $item.Detail) -ForegroundColor Gray
    }
    Write-Host ""
    if ($failed.Count -eq 0) {
        Write-Host "RESULT: PASS - installation is not needed." -ForegroundColor Green
    } else {
        Write-Host ("RESULT: FAIL - {0} required item(s) need installation or repair." -f $failed.Count) -ForegroundColor Red
    }
    Write-Host ("Report: {0}" -f $ReportPath) -ForegroundColor Gray
    return $failed.Count
}

try {
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and [Environment]::Is64BitOperatingSystem) {
        Add-CheckResult "Windows" "PASS" "64-bit Windows is supported."
    } else {
        Add-CheckResult "Windows" "BROKEN" "SmartAgent requires 64-bit Windows."
    }

    $requiredFiles = @(
        (Join-Path $ProjectRoot "smart_agent.py"),
        (Join-Path $ProjectRoot "agent_core"),
        (Join-Path $ProjectRoot "menu_ui.py"),
        (Join-Path $ProjectRoot "RemoteAgent"),
        (Join-Path $ProjectRoot "WebAgent\controller.py"),
        (Join-Path $ProjectRoot "launch_smart_agent.bat"),
        (Join-Path $ProjectRoot "launch_remote_agent.bat"),
        (Join-Path $ProjectRoot "launch_webcopilot_chatgpt.bat")
    )
    $missingFiles = @($requiredFiles | Where-Object { -not (Test-Path -LiteralPath $_) })
    if ($missingFiles.Count -eq 0) {
        Add-CheckResult "Release files" "PASS" "SmartAgent, RemoteAgent, WebAgent, and all launchers are present."
    } else {
        Add-CheckResult "Release files" "MISSING" ("Release package is incomplete: " + ($missingFiles -join ", "))
    }

    $python = Find-CompatiblePython
    if ($python) {
        Add-CheckResult "Python 3.11+ 64-bit" "PASS" ("{0} via {1} ({2})" -f $python.Info.version, $python.Candidate.Label, $python.Info.executable)
    } else {
        Add-CheckResult "Python 3.11+ 64-bit" "MISSING" "No compatible 64-bit Python was found. The installer can install Python 3.11 with winget."
    }

    $venvInfo = $null
    if (Test-Path -LiteralPath $VenvPython) { $venvInfo = Invoke-PythonProbe -Executable $VenvPython }
    if ($venvInfo -and $venvInfo.major -eq 3 -and $venvInfo.minor -ge 11 -and $venvInfo.bits -eq 64) {
        Add-CheckResult "SmartAgent .venv" "PASS" ("Usable virtual environment: Python {0}." -f $venvInfo.version)
    } elseif (Test-Path -LiteralPath $VenvPython) {
        Add-CheckResult "SmartAgent .venv" "BROKEN" ".venv exists, but its python.exe is unusable or incompatible."
    } else {
        Add-CheckResult "SmartAgent .venv" "MISSING" ".venv\Scripts\python.exe was not found."
    }

    $playwrightReady = $false
    if ($venvInfo) {
        $dependencyCode = @'
import importlib.metadata as m, json
names = ('ollama', 'playwright', 'qrcode', 'Pillow')
result = {}
for name in names:
    try:
        result[name] = m.version(name)
    except m.PackageNotFoundError:
        result[name] = None
print(json.dumps(result))
'@
        try {
            $dependencyJson = & $VenvPython -c $dependencyCode 2>$null
            $versions = $dependencyJson | ConvertFrom-Json
            $qrcodeVersion = if ($versions.qrcode) { [version]$versions.qrcode } else { $null }
            $dependenciesReady = (
                $LASTEXITCODE -eq 0 -and
                $versions.ollama -eq "0.6.2" -and
                $versions.playwright -eq "1.62.0" -and
                $qrcodeVersion -and
                $qrcodeVersion -ge [version]"7.4" -and
                $qrcodeVersion -lt [version]"9.0" -and
                $versions.Pillow
            )
            $playwrightReady = $versions.playwright -eq "1.62.0"
            if ($dependenciesReady) {
                Add-CheckResult "Python packages" "PASS" ("ollama {0}; playwright {1}; qrcode {2}; Pillow {3}." -f $versions.ollama, $versions.playwright, $versions.qrcode, $versions.Pillow)
            } else {
                Add-CheckResult "Python packages" "BROKEN" ("Required: ollama 0.6.2, playwright 1.62.0, qrcode >=7.4,<9, Pillow. Found: ollama={0}, playwright={1}, qrcode={2}, Pillow={3}." -f $versions.ollama, $versions.playwright, $versions.qrcode, $versions.Pillow)
            }
        } catch {
            Add-CheckResult "Python packages" "BROKEN" ("Package inspection failed: " + $_.Exception.Message)
        }
    } else {
        Add-CheckResult "Python packages" "MISSING" "Cannot inspect packages until SmartAgent .venv is created."
    }

    if ($venvInfo -and $playwrightReady) {
        $browserCode = "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"
        try {
            & $VenvPython -c $browserCode *> $null
            if ($LASTEXITCODE -eq 0) {
                Add-CheckResult "Playwright Chromium" "PASS" "Chromium launched successfully in headless mode."
            } else {
                Add-CheckResult "Playwright Chromium" "BROKEN" "Chromium is missing or could not launch; Playwright browser repair is required."
            }
        } catch {
            Add-CheckResult "Playwright Chromium" "BROKEN" ("Chromium launch failed: " + $_.Exception.Message)
        }
    } else {
        Add-CheckResult "Playwright Chromium" "MISSING" "Cannot launch Chromium until .venv and Playwright are ready."
    }

    if ($SkipOllama) {
        Add-CheckResult "Ollama" "SKIPPED" "Skipped by -SkipOllama; WebGPT mode can still be used."
    } else {
        $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
        if (-not $ollama) {
            Add-CheckResult "Ollama" "MISSING" "ollama.exe was not found."
        } elseif (Test-OllamaApi) {
            Add-CheckResult "Ollama" "PASS" ("Executable and local API are ready: " + $ollama.Source)
        } else {
            Add-CheckResult "Ollama" "BROKEN" ("ollama.exe exists, but http://127.0.0.1:11434 is not responding: " + $ollama.Source)
        }
    }

    $needsWinget = (-not $python -or (-not $SkipOllama -and -not (Get-Command ollama.exe -ErrorAction SilentlyContinue)))
    if ($needsWinget) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($winget) {
            Add-CheckResult "Installer prerequisite: winget" "PASS" ("Available for missing system components: " + $winget.Source)
        } else {
            Add-CheckResult "Installer prerequisite: winget" "MISSING" "App Installer/winget is required to install missing Python or Ollama automatically."
        }
    }

    $failedCount = Write-Report
    if ($failedCount -eq 0) { exit 0 }
    exit 1
} catch {
    Add-CheckResult "Environment checker" "BROKEN" $_.Exception.Message
    try { [void](Write-Report) } catch { Write-Error $_ }
    exit 2
}
