# SmartAgent release installer helpers

此目錄由 `release\install_smart_agent.bat` 使用，release 封裝時必須一併保留。

- `check_environment.ps1`：只讀取環境狀態，逐項顯示 `PASS`、`MISSING`、`BROKEN` 或 `SKIPPED`，並產生 `environment_report.txt`。
- `install.ps1`：只在環境檢查未通過且使用者確認後，安裝或修復缺少項目。
- `requirements.txt`：SmartAgent Python 套件版本。
- `verify_environment.py`：安裝後驗證 Python 套件、Chromium 與選用的 Ollama。

一般操作：直接雙擊上一層的 `install_smart_agent.bat`。

只檢查、不安裝：

```bat
install_smart_agent.bat -ValidateOnly
```

WebGPT-only、不要求 Ollama：

```bat
install_smart_agent.bat -SkipOllama
```
