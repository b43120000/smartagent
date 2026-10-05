[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$NonInteractive,
    [switch]$ConfigureSecurity,
    [string]$SecurityWorkspaceContainer = "",
    [string[]]$SecurityAdditionalWriteRoots = @(),
    [string]$SecurityRuntimeRoot = "",
    [string[]]$SecurityDeniedWriteRoots = @("$env:PUBLIC", "$env:WINDIR\Temp"),
    [string[]]$SecurityReadOnlyRoots = @(),
    [string]$SecuritySkillRoot = "",
    [string]$SecuritySkillSourcePath = "$env:USERPROFILE\.codex\skills"
)

$ErrorActionPreference = "Stop"
$InstallerVersion = 1
$InstallerDir = if ($env:SMARTAGENT_INSTALLER_DIR) {
    $env:SMARTAGENT_INSTALLER_DIR.TrimEnd("\")
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = Split-Path -Parent $InstallerDir
$SourceDir = Join-Path $ProjectRoot "source"
if ([string]::IsNullOrWhiteSpace($SecurityRuntimeRoot)) {
    $SecurityRuntimeRoot = Join-Path $ProjectRoot "localdata\runtime\security"
}
if ([string]::IsNullOrWhiteSpace($SecuritySkillRoot)) {
    $SecuritySkillRoot = Join-Path $ProjectRoot "localdata\secure\windows_security\skills"
}
$env:PYTHONPATH = $SourceDir + ";" + $env:PYTHONPATH
$env:PYTHONSAFEPATH = "1"
if ([string]::IsNullOrWhiteSpace($SecurityWorkspaceContainer)) {
    $SecurityWorkspaceContainer = Join-Path ([Environment]::GetFolderPath('Desktop')) "SmartAgentWorkspace\default"
}
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $InstallerDir "requirements.txt"
$Verifier = Join-Path $InstallerDir "verify_environment.py"
$ReportPath = Join-Path $ProjectRoot "localdata\logs\install_report.txt"
$StatePath = Join-Path $ProjectRoot "localdata\metadata\install-state.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReportPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
$script:State = [ordered]@{
    installer_version = $InstallerVersion; updated_at = (Get-Date).ToString("o")
    python_ready = $false; venv_ready = $false; dependencies_ready = $false
    chromium_ready = $false; model_mode = "webgpt"
    validation_passed = $false
    security_configured = $false
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
Set-Content -LiteralPath $ReportPath -Value ("SmartAgent install report - {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding UTF8
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " SmartAgent Windows 一鍵環境安裝" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Cyan
try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw "僅支援 64-bit Windows。" }
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir "smart_agent.py"))) { throw "找不到 SmartAgent Source。" }
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
    $dependencyProbe = "import importlib.metadata as m; assert m.version('playwright')=='1.62.0'"
    $dependenciesReady = Test-PythonCommand @("-c", $dependencyProbe)
    if (-not $dependenciesReady -and $ValidateOnly) { throw "驗證模式：Python 依賴缺少或版本不符。" }
    if (-not $dependenciesReady) {
        Write-InstallLog "[依賴] 缺少套件或版本不符，安裝鎖定版本。" "Cyan"
        Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip") "pip 更新失敗"
        Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", $Requirements) "Python 依賴安裝失敗"
    } else {
        Write-InstallLog "[依賴] 鎖定版本已就緒，跳過安裝。" "Green"
    }
    Invoke-Checked $VenvPython @("-c", "import playwright; print('Python dependencies: OK')") "Python 套件驗證失敗"
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
    Write-InstallLog "[Runtime] Lightweight WebGPT mode enabled." "Green"
    $verifyArgs = @($Verifier, "--project-root", $ProjectRoot, "--check-browser")
    Invoke-Checked $VenvPython $verifyArgs "SmartAgent 環境驗證失敗"
    if ($ConfigureSecurity) {
        if ($ValidateOnly) { throw "ValidateOnly cannot change Windows security configuration." }
        $securityScript = Join-Path $InstallerDir "configure_security.ps1"
        Write-InstallLog "[Security] 啟動 NTFS ACL 與 restricted executor 設定；將再次要求輸入 APPLY。" "Cyan"
        & $securityScript -WorkspaceContainer $SecurityWorkspaceContainer -AdditionalWriteRoots $SecurityAdditionalWriteRoots -RuntimeRoot $SecurityRuntimeRoot -DeniedWriteRoots $SecurityDeniedWriteRoots -ReadOnlyRoots $SecurityReadOnlyRoots -SkillRoot $SecuritySkillRoot -SkillSourcePath $SecuritySkillSourcePath -ExecutorPython $VenvPython -Apply
        if ($LASTEXITCODE -ne 0) { throw "Restricted executor security configuration failed." }
        $script:State.security_configured = $true
        Save-InstallState
    }
    foreach ($test in @("tests\validate_stage8.py", "tests\validate_stage9.py", "tests\validate_stage10.py", "tests\validate_cross_session_routing.py", "source\WebAgent\tests\validate_bootstrap_provisioning.py")) {
        $testPath = Join-Path $ProjectRoot $test
        if (Test-Path -LiteralPath $testPath) { Invoke-Checked $VenvPython @($testPath) "SmartAgent 自測失敗：$test" }
    }
    $layoutScript = Join-Path $InstallerDir "deploy_public_layout.ps1"
    & $layoutScript -ProjectRoot $ProjectRoot -WorkspaceRoot $SecurityWorkspaceContainer
    if ($LASTEXITCODE -ne 0) { throw "Desktop/control layout deployment failed." }
    $script:State.validation_passed = $true; Save-InstallState
    Write-InstallLog "[完成] SmartAgent 執行環境與本機自測全部通過。" "Green"
    Write-Host "首次執行 launch_webcopilot_chatgpt.bat 時，Chromium 會開啟 ChatGPT。" -ForegroundColor Yellow
    Write-Host "請親自登入 ChatGPT；帳密、2FA、CAPTCHA 不會由安裝器處理。" -ForegroundColor Yellow
    Write-Host "安裝報告：$ReportPath" -ForegroundColor Gray
    exit 0
} catch {
    Write-InstallLog ("[失敗] " + $_.Exception.Message) "Red"
    Save-InstallState
    Write-Host "可重新執行安裝器；已完成項目會重新驗證並沿用。" -ForegroundColor Yellow
    exit 1
}
