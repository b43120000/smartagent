"""Preserve Runtime command rejection details for the PokeClaw Planner.

Mock-only test; never an OS AppContainer attestation.
"""
from __future__ import annotations

import pytest
from agent_core import tools
from agent_core.tool_result_contract import decode_result
from agent_core.security_context import SecurityContext
from WebAgent.pokeclaw_executor_loop import _tool_result


@pytest.mark.parametrize("detail,stage", [
    ("SECURITY_COMMAND_REJECTED:diskpart_forbidden", "COMMAND_SECURITY"),
    ("TURBO_OS_BROKER_BLOCKED:TURBO_ROOT_NOT_BOUND_TO_ACTIVE_WORKSPACE", "TURBO_BROKER"),
    ("TURBO_OS_BROKER_BLOCKED:TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED", "TURBO_BROKER"),
])
def test_rejection_cause_reaches_planner(tmp_path, monkeypatch, detail, stage):
    def blocked(_command, _timeout, _capture, *, cwd, telemetry, approval_manifest):
        return {
            "exit_code": None, "stdout": "", "stderr": "",
            "timed_out": False, "security_rejected": True,
            "process_started": False, "error": detail,
            "sandbox_evidence": {"executed": False},
        }
    monkeypatch.setattr(tools, "_run_powershell_capture", blocked)
    raw = tools.tool_run_command(
        "Write-Output 'mock-only'", "MUTATE",
        working_directory=tmp_path,
        security_context=SecurityContext.for_workspace(tmp_path),
    )
    receipt = decode_result(raw)
    assert receipt is not None
    assert receipt["status"] == "REJECTED"
    assert receipt["executed"] is False
    assert receipt["reason"] == "COMMAND_REJECTED"
    assert receipt["data"]["command_error"] == detail
    assert receipt["data"]["command_rejection_stage"] == stage
    tool_view = _tool_result(receipt["status"], receipt["reason"], receipt["data"])
    assert tool_view["isSuccess"] is False
    assert tool_view["data"] is None
    assert detail in tool_view["error"]
    assert stage in tool_view["error"]


def test_other_rejection_does_not_invent_command_error():
    view = _tool_result(
        "REJECTED", "DIRECTORY_SCOPE_REJECTED",
        {"process_state": "NOT_STARTED", "exit_code": None},
    )
    assert "command_error" not in view["error"]
    assert "TURBO_BROKER" not in view["error"]
