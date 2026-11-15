"""Git63 Native AppContainer PWD and protected Broker update regressions.

Do not bind PowerShell's provider PWD in LaunchPowerShell: this same launcher
executes kernel negative-sentinel probes before any Task. Synchronize PWD only
in the explicitly admitted Task path (turbo_os_execute.ps1).
"""
from pathlib import Path
import hashlib
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "install_smart_agent" / "turbo_appcontainer_probe.cs"
BROKER = ROOT / "install_smart_agent" / "turbo_os_execute.ps1"
REBIND = ROOT / "install_smart_agent" / "turbo_multi_root_rebind.ps1"
UPDATER = ROOT / "install_smart_agent" / "git62_transaction_update.ps1"


def test_shared_native_launcher_does_not_change_sentinel_probe_pwd():
    source = NATIVE.read_text(encoding="utf-8")
    start = source.index("public static string LaunchPowerShell(string moniker, string script,")
    stop = source.index("public static string LaunchPowerShellCaptured(", start)
    launcher = source[start:stop]
    assert "Set-Location -LiteralPath" not in launcher
    assert "[Environment]::CurrentDirectory" not in launcher
    assert "Environment.CurrentDirectory =" not in launcher
    assert "Encoding.Unicode.GetBytes(body)" in launcher
    assert "LaunchWithTrustedPath(" in launcher
    assert "VerifyChildToken(pi.hProcess, sidString)" in source
    assert "environment, cwd, ref si, out pi" in source


def test_task_only_pwd_binding_precedes_untrusted_script():
    source = BROKER.read_text(encoding="utf-8")
    assert "$cwdLiteral=([string]$state.cwd).Replace(\"'\",\"''\")" in source
    assert "Set-Location -LiteralPath '__CWD_LITERAL__' -ErrorAction Stop" in source
    assert "(Get-Location).Path" in source
    assert "[Environment]::CurrentDirectory" in source
    assert "GIT63_NATIVE_TASK_CWD_MISMATCH" in source
    assert "$nativeScript=$taskCwdSetup+[Environment]::NewLine+$envSetup+" in source
    assert source.index("$nativeScript=$taskCwdSetup") < source.index(
        "$nativeRaw=[SmartAgentGit47AppContainerProbe]::LaunchPowerShellCaptured("
    )
    assert "$moniker,$nativeScript,$state.cwd," in source


def test_rebind_sentinel_os_proof_remains_isolated_from_task_pwd():
    rebind = REBIND.read_text(encoding="utf-8")
    assert "[IO.File]::ReadAllText('$literal'); exit 89" in rebind
    assert "catch [System.UnauthorizedAccessException] { exit 0 }" in rebind
    assert "[SmartAgentGit47AppContainerProbe]::LaunchPowerShell(" in rebind
    assert "GIT62_REBIND_KERNEL_ATTESTATION_FAILED" in rebind
    assert "GIT63_NATIVE_TASK_CWD_MISMATCH" not in rebind


def test_elevated_update_refreshes_both_protected_broker_files():
    rebind = REBIND.read_text(encoding="utf-8")
    updater = UPDATER.read_text(encoding="utf-8")
    assert "& $rebind -ProjectRoot $root -RefreshBrokerSource" in updater
    assert "$fileNames=@('turbo_os_execute.ps1','turbo_appcontainer_probe.cs')" in rebind
    assert "$manifest.core_files.PSObject.Properties[$pinName]" in rebind
    assert "GIT63_BROKER_REFRESH_MANIFEST_PIN_MISMATCH" in rebind
    assert "Copy-Item -LiteralPath $record.source -Destination $record.target" in rebind
    assert "GIT63_BROKER_REFRESH_HASH_FAILED" in rebind


def test_rebind_refresh_has_single_complete_phase_two():
    rebind = REBIND.read_text(encoding="utf-8")
    assert rebind.count("# Phase 2: independent, protected, replace-atomic journal writes.") == 1
    assert rebind.count("foreach($item in $verified){") == 1
    assert "if($expectedHash -notmatch '^[0-9a-fA-F]{64}$'){" in rebind
    assert rebind.count("GIT62_PROTECTED_BROKER_REFRESH_PASS") == 1


def test_updated_integrity_manifest_has_both_native_scripts():
    import json
    manifest = json.loads((ROOT / "config" / "protocol_manifest.json").read_text(encoding="utf-8"))
    for path in ("install_smart_agent/turbo_os_execute.ps1",
                 "install_smart_agent/turbo_appcontainer_probe.cs",
                 "install_smart_agent/turbo_multi_root_rebind.ps1",
                 "install_smart_agent/git62_transaction_update.ps1"):
        assert manifest["core_files"][path] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 Parser required")
@pytest.mark.parametrize("script", [
    "install_smart_agent/turbo_multi_root_rebind.ps1",
    "install_smart_agent/turbo_os_execute.ps1",
    "install_smart_agent/git62_transaction_update.ps1",
])
def test_windows_powershell_parser_reports_no_syntax_errors(script):
    path = (ROOT / script).resolve()
    literal = str(path).replace("'", "''")
    command = (
        "$tokens=$null; $parseErrors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile('" + literal + "',"
        "[ref]$tokens,[ref]$parseErrors) | Out-Null; "
        "if($null -ne $parseErrors -and $parseErrors.Count -gt 0){"
        "$parseErrors | ForEach-Object { Write-Error $_.Message }; exit 1 }; "
        "Write-Output 'GIT63_NATIVE_PARSER_PASS'"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "GIT63_NATIVE_PARSER_PASS" in result.stdout
