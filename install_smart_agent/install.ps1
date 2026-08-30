[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$SkipOllama,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$InstallerVersion = 1
$InstallerDir = if ($env:SMARTAGENT_INSTALLER_DIR) {
    $env:SMARTAGENT_INSTALLER_DIR.TrimEnd("\")
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = Split-Path -Parent $InstallerDir
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $InstallerDir "requirements.txt"
$Verifier = Join-Path $InstallerDir "verify_environment.py"
$ReportPath = Join-Path $InstallerDir "install_report.txt"
$StatePath = Join-Path $InstallerDir "install-state.json"
$script:State = [ordered]@{
    installer_version = $InstallerVersion; updated_at = (Get-Date).ToString("o")
    python_ready = $false; venv_ready = $false; dependencies_ready = $false
    chromium_ready = $false; ollama_ready = $false; model_mode = "minimal"
    validation_passed = $false
}

function Write-InstallLog {
    param([string]$Message, [string]$Color = "Gray")
    Write-Host $Message -ForegroundColor $Color
    $safe = $Message -replace '(?i)(token|password|cookie|authorization)\s*[:=]\s*\S+', '$1=[REDACTED]'
    Add-Content -LiteralPath $ReportPath -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $safe) -Encoding UTF8
}
function Save-InstallState {
    $script:State.updated_at = (Get-Date).ToString("o")
    $temporary = $StatePath + ".tmp"
    $script:State | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
}
function Refresh-InstallerPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-CompatiblePython {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates += ,@($py.Source, "-3.11") }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { $candidates += ,@($python.Source) }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]; $prefix = @()
        if ($candidate.Count -gt 1) { $prefix = $candidate[1..($candidate.Count - 1)] }
        try {
            $probe = & $exe @prefix -c "import json,struct,sys; print(json.dumps({'version':sys.version_info[:3],'bits':struct.calcsize('P')*8}))" 2>$null
            $info = $probe | ConvertFrom-Json
            if ($info.bits -eq 64 -and $info.version[0] -eq 3 -and $info.version[1] -ge 11) {
                return @{ Exe = $exe; Prefix = $prefix; Version = ($info.version -join ".") }
            }
        } catch { }
    }
    return $null
}
function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FailureMessage (exit=$LASTEXITCODE)" }
}
function Test-PythonCommand {
    param([string[]]$Arguments)
    try {
        & $VenvPython @Arguments *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}
function Test-OllamaApi {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}
function Ensure-Ollama {
    if ($SkipOllama) {
        Write-InstallLog "[Ollama] 已依參數跳過；WebGPT 模式仍可使用。" "Yellow"
        return
    }
    $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $ollama) {
        if ($ValidateOnly) { throw "驗證模式：找不到 Ollama。" }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw "缺少 winget。請先從 Microsoft Store 安裝 App Installer。" }
        Write-InstallLog "[Ollama] 正在透過 winget 安裝；Windows 可能要求確認。" "Cyan"
        Invoke-Checked $winget.Source @("install", "-e", "--id", "Ollama.Ollama", "--accept-package-agreements", "--accept-source-agreements") "Ollama 安裝失敗"
        Refresh-InstallerPath
        $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    }
    if (-not $ollama) { throw "Ollama 安裝後仍找不到 ollama.exe，請重新開機後再執行。" }
    if (-not (Test-OllamaApi)) {
        Write-InstallLog "[Ollama] 啟動本機服務並等待 API 就緒。" "Cyan"
        Start-Process -FilePath $ollama.Source -ArgumentList "serve" -WindowStyle Hidden
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline -and -not (Test-OllamaApi)) { Start-Sleep -Milliseconds 750 }
    }
    if (-not (Test-OllamaApi)) { throw "Ollama 已安裝，但本機 API 未能在 30 秒內啟動。" }
    $script:State.ollama_ready = $true; Save-InstallState
    Write-InstallLog "[Ollama] 本機服務已就緒。預設不下載大型模型。" "Green"
    if (-not $NonInteractive -and -not $ValidateOnly) {
        Write-Host "若要使用 GPT-OSS 120B 等 Ollama Cloud 模型，需要登入 Ollama。" -ForegroundColor Yellow
        $signin = Read-Host "現在執行 'ollama signin' 並開啟登入頁面？(y/N)"
        if ($signin -match '^[yY]$') {
            & $ollama.Source signin
            if ($LASTEXITCODE -ne 0) { Write-InstallLog "[Ollama] 登入未完成；可稍後執行 ollama signin。" "Yellow" }
        }
        $model = Read-Host "是否下載本機 fallback 模型 gemma4:latest（需要額外磁碟與時間）？(y/N)"
        if ($model -match '^[yY]$') {
            $script:State.model_mode = "standard"
            Invoke-Checked $ollama.Source @("pull", "gemma4:latest") "模型下載失敗"
            Save-InstallState
        }
    }
}

Set-Content -LiteralPath $ReportPath -Value ("SmartAgent install report - {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding UTF8
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " SmartAgent Windows 一鍵環境安裝" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Cyan
try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw "僅支援 64-bit Windows。" }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "smart_agent.py"))) { throw "找不到 SmartAgent Source。" }
    Write-InstallLog "[預檢] 專案：$ProjectRoot" "Green"
    $pythonInfo = Find-CompatiblePython
    if (-not $pythonInfo) {
        if ($ValidateOnly) { throw "驗證模式：找不到 Python 3.11+ 64-bit。" }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw "缺少 winget。請從 Microsoft Store 安裝 App Installer。" }
        Write-InstallLog "[Python] 正在透過 winget 安裝 Python 3.11 64-bit。" "Cyan"
        Invoke-Checked $winget.Source @("install", "-e", "--id", "Python.Python.3.11", "--scope", "user", "--accept-package-agreements", "--accept-source-agreements") "Python 安裝失敗"
        Refresh-InstallerPath; $pythonInfo = Find-CompatiblePython
    }
    if (-not $pythonInfo) { throw "Python 安裝後仍無法定位；請重新開機後再執行。" }
    $script:State.python_ready = $true
    Write-InstallLog ("[Python] 使用 {0}" -f $pythonInfo.Version) "Green"
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if ($ValidateOnly) { throw "驗證模式：.venv 尚未建立。" }
        Write-InstallLog "[Python] 建立專案專用 .venv。" "Cyan"
        & $pythonInfo.Exe @($pythonInfo.Prefix) -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw ".venv 建立失敗。" }
    }
    $script:State.venv_ready = $true; Save-InstallState
    $dependencyProbe = "import importlib.metadata as m; assert m.version('ollama')=='0.6.2'; assert m.version('playwright')=='1.62.0'; q=tuple(int(x) for x in m.version('qrcode').split('.')[:2]); assert (7,4)<=q<(9,0); assert m.version('Pillow')"
    $dependenciesReady = Test-PythonCommand @("-c", $dependencyProbe)
    if (-not $dependenciesReady -and $ValidateOnly) { throw "驗證模式：Python 依賴缺少或版本不符。" }
    if (-not $dependenciesReady) {
        Write-InstallLog "[依賴] 缺少套件或版本不符，安裝鎖定版本。" "Cyan"
        Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip") "pip 更新失敗"
        Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", $Requirements) "Python 依賴安裝失敗"
    } else {
        Write-InstallLog "[依賴] 鎖定版本已就緒，跳過安裝。" "Green"
    }
    Invoke-Checked $VenvPython @("-c", "import ollama,playwright,qrcode,PIL; print('Python dependencies: OK')") "Python 套件驗證失敗"
    $script:State.dependencies_ready = $true; Save-InstallState
    $browserReady = Test-PythonCommand @($Verifier, "--project-root", $ProjectRoot, "--check-browser")
    if (-not $browserReady -and $ValidateOnly) { throw "驗證模式：Playwright Chromium 無法啟動。" }
    if (-not $browserReady) {
        Write-InstallLog "[Chromium] 尚未就緒，安裝或修復 Playwright Chromium。" "Cyan"
        Invoke-Checked $VenvPython @("-m", "playwright", "install", "chromium") "Playwright Chromium 安裝失敗"
    } else {
        Write-InstallLog "[Chromium] 已可正常啟動，跳過安裝。" "Green"
    }
    Invoke-Checked $VenvPython @($Verifier, "--project-root", $ProjectRoot, "--check-browser") "Chromium 啟動驗證失敗"
    $script:State.chromium_ready = $true; Save-InstallState
    Ensure-Ollama
    $verifyArgs = @($Verifier, "--project-root", $ProjectRoot, "--check-browser")
    if (-not $SkipOllama) { $verifyArgs += "--check-ollama" }
    Invoke-Checked $VenvPython $verifyArgs "SmartAgent 環境驗證失敗"
    foreach ($test in @("tests\validate_stage7_final.py", "tests\validate_stage8.py", "tests\validate_stage9.py", "tests\validate_stage10.py", "tests\validate_cross_session_routing.py")) {
        $testPath = Join-Path $ProjectRoot $test
        if (Test-Path -LiteralPath $testPath) { Invoke-Checked $VenvPython @($testPath) "SmartAgent 自測失敗：$test" }
    }
    $script:State.validation_passed = $true; Save-InstallState
    Write-InstallLog "[完成] SmartAgent 執行環境與本機自測全部通過。" "Green"
    Write-Host "首次執行 launch_smart_agent.bat 時，Chromium 會開啟 ChatGPT。" -ForegroundColor Yellow
    Write-Host "請親自登入 ChatGPT；帳密、2FA、CAPTCHA 不會由安裝器處理。" -ForegroundColor Yellow
    if (-not $SkipOllama) { Write-Host "若使用 Ollama Cloud 但尚未登入，請執行：ollama signin" -ForegroundColor Yellow }
    Write-Host "安裝報告：$ReportPath" -ForegroundColor Gray
    exit 0
} catch {
    Write-InstallLog ("[失敗] " + $_.Exception.Message) "Red"
    Save-InstallState
    Write-Host "可重新執行安裝器；已完成項目會重新驗證並沿用。" -ForegroundColor Yellow
    exit 1
}
