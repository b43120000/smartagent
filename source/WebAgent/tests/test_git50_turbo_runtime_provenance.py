"""Git50: OS-sandbox evidence must survive canonical Tool Result assembly.

Mocked adapter result only. Real Windows AppContainer/token/kernel denial is
tested independently by validate_git50_turbo_activation.ps1.
"""
from __future__ import annotations

from agent_core import tools
from agent_core.tool_result_contract import decode_result


def test_native_broker_evidence_attached_to_blackboard_result(tmp_path, monkeypatch):
    os_evidence = {
        "schema": "SMARTAGENT_TURBO_OS_ACTION_ATTESTATION_V1",
        "sandbox_backend": "WIN32_APPCONTAINER",
        "sandbox_identity_verified": True,
        "kernel_outside_read_denied": True,
        "child_process_policy": "JOB_KILL_ON_CLOSE",
        "action_id": "ACT-EVIDENCE-50",
        "task_id": "TASK-EVIDENCE-50",
        "executed": True,
        "os_execution_result": "OS_EXECUTION_SUCCEEDED",
    }
    passed_telemetry = []

    def capture(_command, _timeout=30, _capture_root=None, *,
                cwd=None, telemetry=None, approval_manifest=None):
        passed_telemetry.append(telemetry)
        return {
            "exit_code": 0, "stdout": "OS_STDOUT", "stderr": "",
            "stdout_bytes": 9, "stderr_bytes": 0,
            "stdout_ref": "", "stderr_ref": "",
            "stdout_truncated": False, "stderr_truncated": False,
            "timed_out": False, "security_rejected": False,
            "process_started": True, "sandbox_evidence": os_evidence,
        }

    monkeypatch.setattr(tools, "_run_powershell_capture", capture)
    result = tools.tool_run_command(
        "Get-Date", "INSPECT", verify=[],
        working_directory=tmp_path,
        action_id="ACT-EVIDENCE-50",
    )
    parsed = decode_result(result)
    assert parsed is not None
    runtime = parsed.get("_runtime_result", parsed)
    data = runtime.get("data", {})
    assert data["sandbox_evidence"] == os_evidence
    assert runtime["executed"] is True
    assert "[TURBO_OS_SANDBOX_EVIDENCE]" in result
    assert "OS_STDOUT" in result
    assert data["exit_code"] == 0
    # No fake task identity is invented when no SecurityContext exists.
    assert passed_telemetry == [None]


def test_rejected_broker_never_counts_as_executed(tmp_path, monkeypatch):
    def rejected(*args, **kwargs):
        return {
            "exit_code": None, "stdout": "", "stderr": "",
            "timed_out": False, "security_rejected": True,
            "process_started": False,
            "error": "TURBO_OS_BROKER_BLOCKED:NON_AUTHORIZING",
            "sandbox_evidence": {
                "sandbox_backend": "UNAVAILABLE_NO_OS_BROKER",
                "executed": False, "os_execution_result": "NOT_EXECUTED",
            },
        }

    monkeypatch.setattr(tools, "_run_powershell_capture", rejected)
    result = tools.tool_run_command(
        "Get-Date", "INSPECT", verify=[], working_directory=tmp_path
    )
    parsed = decode_result(result)
    assert parsed is not None
    runtime = parsed.get("_runtime_result", parsed)
    assert runtime["executed"] is False
    assert runtime["status"] == "REJECTED"
    assert runtime["data"]["sandbox_evidence"]["executed"] is False
    assert runtime["data"]["terminal_eligible"] is False
