"""Git63 Native step-trace RCA: fixed commands, no secret-bearing values.

These are source contract and local Python unit tests. Live PowerShell
AppContainer execution remains a separate Windows-only acceptance test.
"""
from pathlib import Path

from agent_core.executor_command_rca import capture_command_result_rca


ROOT = Path(__file__).resolve().parents[3]
BROKER = ROOT / "install_smart_agent" / "turbo_os_execute.ps1"
NATIVE = ROOT / "install_smart_agent" / "turbo_appcontainer_probe.cs"
COLLECT = ROOT / "collect.bat"


def test_native_cwd_operations_are_individually_traced_before_execution():
    source = BROKER.read_text(encoding="utf-8")
    steps = [
        "CWD_GET_LOCATION_INITIAL",
        "CWD_SET_LOCATION",
        "CWD_GET_LOCATION_VERIFY",
        "CWD_GET_NATIVE_CURRENT_DIRECTORY",
        "CWD_COMPARE",
    ]
    for step in steps:
        assert f"'{step}.START'" in source or step == "CWD_SET_LOCATION"
        assert f"'{step}.PASS'" in source
    assert "'CWD_SET_LOCATION.SKIP'" in source
    assert "$script:smartagentSubstage='CWD_GET_LOCATION_INITIAL'" in source
    assert "$script:smartagentSubstage='CWD_SET_LOCATION'" in source
    assert "SMARTAGENT_NATIVE_TRACE_V1" in source
    assert "native_failure_substage=$nativeFailureSubstage" in source
    assert "native_exception_hresult=$nativeExceptionHresult" in source
    assert "native_command_trace=@($nativeCommandTrace)" in source


def test_native_wrapper_emits_trace_even_after_command_exception():
    source = NATIVE.read_text(encoding="utf-8")
    assert "$script:smartagentTrace=New-Object" in source
    assert "SMARTAGENT_NATIVE_FAILURE_SUBSTAGE=" in source
    assert "SMARTAGENT_NATIVE_TRACE_V1=" in source
    assert "$_.Exception.HResult.ToString('X8')" in source
    assert "WaitForSingleObject(pi.hProcess, interval)" in source


def test_command_rca_retains_only_named_safe_native_fields():
    receipt = {
        "status": "FAILED", "reason": "COMMAND_EXITED", "executed": True,
        "data": {
            "exit_code": 1,
            "sandbox_evidence": {
                "sandbox_backend": "WIN32_APPCONTAINER",
                "native_failure_stage": "CWD_SETUP",
                "native_failure_substage": "CWD_GET_LOCATION_INITIAL",
                "native_exception_hresult": "80070005",
                "native_exception_type": "UnauthorizedAccessException",
                "native_command_trace": ["CWD_GET_LOCATION_INITIAL.START"],
                "native_process_pid": 1456,
                "native_running_sample_count": 0,
                "unsafe_extra": "NEVER_EXPORT",
            },
        },
    }
    result = capture_command_result_rca(
        "[COMMAND_RESULT]\nstderr:\nSMARTAGENT_NATIVE_FAILURE_STAGE=CWD_SETUP;UnauthorizedAccessException",
        receipt,
    )
    sandbox = result["sandbox_evidence"]
    assert sandbox["native_failure_substage"] == "CWD_GET_LOCATION_INITIAL"
    assert sandbox["native_exception_hresult"] == "80070005"
    assert sandbox["native_command_trace"] == ["CWD_GET_LOCATION_INITIAL.START"]
    assert "unsafe_extra" not in sandbox


def test_collector_exports_readable_native_trace_into_rca_zip():
    source = COLLECT.read_text(encoding="utf-8")
    assert "native_execution_trace.json" in source
    assert "native_execution_trace.txt" in source
    for token in [
        "CWD_GET_LOCATION_INITIAL", "CWD_SET_LOCATION",
        "CWD_GET_LOCATION_VERIFY", "CWD_GET_NATIVE_CURRENT_DIRECTORY",
        "native_exception_hresult", "native_failure_substage",
    ]:
        assert token in source
    assert "Set-Location -LiteralPath '<AUTHORIZED_CWD>' -ErrorAction Stop" in source
    assert "stage_diagnostic_not_authorization" not in source or "diagnostic" in source
    assert "$failureStage=Safe-Code $ev.native_failure_stage" in source
    assert "$stage=Safe-Code $ev.native_failure_stage" not in source
