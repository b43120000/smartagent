"""Git64 guarded Native PowerShell PSDrive CWD fallback contracts.

These tests validate source invariants and broker receipt parsing; they DO NOT
claim a real AppContainer CWD or kernel ACL result on CI runners.
"""
from pathlib import Path

from agent_core.turbo_os_broker_client import TurboOSBrokerUnavailable


ROOT = Path(__file__).resolve().parents[3]
TASK_BROKER = ROOT / "install_smart_agent" / "turbo_os_execute.ps1"
NATIVE_CLIENT = ROOT / "source" / "agent_core" / "turbo_os_broker_client.py"
COLLECTOR = ROOT / "collect.bat"
ACL_IMPORT = ROOT / "install_smart_agent" / "turbo_existing_root_import.ps1"


def _script() -> str:
    return TASK_BROKER.read_text(encoding="utf-8")


def test_original_direct_set_location_runs_first():
    script = _script()
    assert "try {\n        Set-Location -LiteralPath '__CWD_LITERAL__' -ErrorAction Stop" in script
    assert "catch [System.UnauthorizedAccessException]" in script
    direct = script.index("Set-Location -LiteralPath '__CWD_LITERAL__' -ErrorAction Stop")
    fallback = script.index("New-PSDrive -Name 'SARTCWD'")
    assert direct < fallback
    assert "CWD_SET_LOCATION.FAIL" in script


def test_fallback_requires_exact_os_cwd_and_provider_root():
    script = _script()
    start = script.index("$script:smartagentSubstage='CWD_PSDRIVE_FALLBACK'")
    end = script.index("New-PSDrive -Name 'SARTCWD'", start)
    guard = script[start:end]
    assert "CurrentDirectory" in guard
    assert "GIT64_NATIVE_CWD_FALLBACK_OS_CWD_MISMATCH" in guard
    assert "Resolve-Path -LiteralPath '__CWD_LITERAL__' -ErrorAction Stop" in guard
    assert "$targetLocation.Provider.Name -cne 'FileSystem'" in guard
    assert "GIT64_NATIVE_CWD_FALLBACK_PROVIDER_MISMATCH" in guard
    assert "New-PSDrive" not in guard
    assert "Set-Location" not in guard


def test_temporary_psdrive_never_changes_host_os_scope():
    script = _script()
    assert "New-PSDrive -Name 'SARTCWD' -PSProvider FileSystem -Root '__CWD_LITERAL__' -Scope Local -ErrorAction Stop" in script
    assert "Set-Location -LiteralPath 'SARTCWD:\\' -ErrorAction Stop" in script
    assert "$script:verifiedLocation.Provider.Name -cne 'FileSystem'" in script
    assert "$script:verifiedLocation.ProviderPath" in script
    assert "GIT63_NATIVE_TASK_CWD_MISMATCH" in script
    assert "$script:nativeProcessCwd=[Environment]::CurrentDirectory" in script
    assert "GIT64_NATIVE_CWD_FALLBACK" not in ACL_IMPORT.read_text(encoding="utf-8")


def test_native_tool_result_and_collector_trace_fallback():
    client = NATIVE_CLIENT.read_text(encoding="utf-8")
    collector = COLLECTOR.read_text(encoding="utf-8")
    assert '"CWD_PSDRIVE_FALLBACK"' in client
    assert "CWD_PSDRIVE_FALLBACK.START" in _script()
    assert "CWD_PSDRIVE_FALLBACK.PASS" in _script()
    assert "CWD_PSDRIVE_FALLBACK='New-PSDrive" in collector
    assert "native_execution_trace.json" in collector
    assert "native_execution_trace.txt" in collector
    assert "CWD_PSDRIVE_FALLBACK" in _script()
