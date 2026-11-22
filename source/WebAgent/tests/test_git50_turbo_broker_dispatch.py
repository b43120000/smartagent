"""Git50: Python process dispatch may enter only the native Broker.

These are adapter unit tests; Windows OS token/negative-file proof is
independently exercised in the disposable Windows integration job.
"""
from __future__ import annotations

import pytest
from agent_core import bounded_process, turbo_broker_admission, turbo_os_broker_client


@pytest.fixture
def requested(monkeypatch):
    monkeypatch.setattr(
        turbo_broker_admission, "assess",
        lambda workspace, *, tool, action_id="": {
            "decision": "BROKER_REQUIRED", "reason_code": "SANDBOX_UNAVAILABLE",
            "evidence": {"sandbox_backend": "UNAVAILABLE_NO_OS_BROKER",
                         "executed": False, "os_execution_result": "NOT_EXECUTED"},
        },
    )
    monkeypatch.setattr("agent_core.command_security.require_command_allowed",
                        lambda *args, **kwargs: None)


def test_supported_powershell_adapter_calls_only_native_broker(requested, monkeypatch, tmp_path):
    calls = []

    def broker(command, *, cwd, timeout, max_stream_bytes, task_id, action_id):
        calls.append((command, cwd, timeout, task_id, action_id))
        return {
            "exit_code": 0, "stdout": "OS_APP_CONTAINER", "stderr": "",
            "security_rejected": False, "process_started": True,
            "sandbox_evidence": {"sandbox_identity_verified": True,
                                 "os_execution_result": "OS_EXECUTION_SUCCEEDED"},
        }

    monkeypatch.setattr(turbo_os_broker_client, "execute_turbo_command", broker)
    def fail_host(*_args, **_kwargs):
        raise AssertionError("HOST_SUBPROCESS_MUST_NOT_RUN")
    monkeypatch.setattr(bounded_process.subprocess, "Popen", fail_host)
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Write-Output NOT_THE_REAL_SCRIPT"],
        cwd=tmp_path, security_command="Write-Output SAFE_OS_SCRIPT",
        telemetry={"task_id": "TASK-REAL", "action_id": "ACT-REAL"},
        timeout=33,
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == "OS_APP_CONTAINER"
    assert calls == [("Write-Output SAFE_OS_SCRIPT", tmp_path, 33,
                      "TASK-REAL", "ACT-REAL")]


@pytest.mark.parametrize("args,security_command,shell", [
    (["python", "-c", "print('bad')"], "print('bad')", False),
    (["powershell", "-NoProfile", "-Command", "Write-Output x"],
     None, False),
    (["powershell", "-NoProfile", "-Command", "Write-Output x"],
     "Write-Output x", True),
    (["powershell", "-ExecutionPolicy", "Bypass", "-Command", "x"],
     "Write-Output x", False),
])
def test_unadapted_host_launches_blocked(requested, monkeypatch, tmp_path,
                                        args, security_command, shell):
    def fail_os(*_args, **_kwargs):
        raise AssertionError("UNSUPPORTED_LAUNCH_REACHED_OS_BROKER")
    monkeypatch.setattr(turbo_os_broker_client, "execute_turbo_command", fail_os)
    result = bounded_process.run_bounded_process(
        args, cwd=tmp_path, shell=shell, security_command=security_command,
    )
    assert result["security_rejected"] is True
    assert result["process_started"] is False
    assert result["exit_code"] is None
    assert "TURBO_BROKER_UNSUPPORTED_LAUNCH_FORM" in result["error"]


def test_broker_failure_is_never_normal_executor_fallback(requested, monkeypatch, tmp_path):
    def unavailable(*_args, **_kwargs):
        raise turbo_os_broker_client.TurboOSBrokerUnavailable(
            "TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED"
        )
    monkeypatch.setattr(turbo_os_broker_client, "execute_turbo_command", unavailable)
    def fail_host(*_args, **_kwargs):
        raise AssertionError("HOST_POPEN_USED_AFTER_NATIVE_BROKER_FAILURE")
    monkeypatch.setattr(bounded_process.subprocess, "Popen", fail_host)
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Get-Date"], cwd=tmp_path,
        security_command="Get-Date",
    )
    assert result["security_rejected"] is True
    assert result["process_started"] is False
    assert result["exit_code"] is None
    assert result["sandbox_evidence"]["executed"] is False
    assert "TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED" in result["error"]
