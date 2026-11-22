"""Fail-closed diagnostics for a nonzero protected PowerShell broker exit.

All child process launches in these tests are stubbed; no OS attestation.
"""
from __future__ import annotations

from types import SimpleNamespace
import pytest

from agent_core import turbo_os_broker_client as broker


@pytest.mark.parametrize("stderr,expected", [
    (
        "At protected code: throw 'GIT50_BROKER_KERNEL_NEGATIVE_PROBE_FAILED'",
        "GIT50_BROKER_KERNEL_NEGATIVE_PROBE_FAILED",
    ),
    (
        "Exception calling LaunchPowerShell: CreateProcessW(AppContainer)",
        "WIN32_CREATEPROCESS_APP_CONTAINER",
    ),
    (
        "Exception calling LaunchPowerShell: AssignProcessToJobObject",
        "WIN32_ASSIGN_JOB",
    ),
    (
        "Add-Type : Cannot add type. Compilation errors occurred.",
        "POWERSHELL_NATIVE_TYPE_COMPILATION",
    ),
    (
        "ParameterBindingException: A parameter cannot be found",
        "POWERSHELL_PARAMETER_BINDING",
    ),
])
def test_native_broker_safe_rejection_categories(stderr, expected):
    reason = broker._native_broker_rejection_reason(stderr)
    assert reason == "TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED:" + expected
    assert "At protected code" not in reason


def test_unknown_reason_fails_closed_without_leaking_data():
    secret = "SECRET_ACCESS_KEY_PROHIBITED"
    reason = broker._native_broker_rejection_reason(
        "Unknown native error " + secret
    )
    assert reason == "TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED"
    assert secret not in reason


def test_native_broker_nonzero_exit_keeps_category(monkeypatch, tmp_path):
    monkeypatch.setattr(
        broker, "_resolve_workspace_and_cwd",
        lambda cwd: (tmp_path, tmp_path),
    )
    monkeypatch.setattr(
        broker, "_load_activation",
        lambda workspace: (
            {"turbo_root": str(tmp_path)},
            {"policy_revision": 17},
            tmp_path / "broker.ps1",
        ),
    )
    monkeypatch.setattr(broker, "_install_root", lambda: tmp_path)
    calls = []
    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="GIT50_BROKER_KERNEL_NEGATIVE_PROBE_FAILED",
        )
    monkeypatch.setattr(broker.subprocess, "run", fake_run)

    with pytest.raises(
        broker.TurboOSBrokerUnavailable,
        match="TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED:"
              "GIT50_BROKER_KERNEL_NEGATIVE_PROBE_FAILED",
    ):
        broker.execute_turbo_command(
            "Write-Output 'test only'",
            cwd=tmp_path, timeout=10,
            task_id="TASK-DIAGNOSTICS",
            action_id="MIN-DIAGNOSTICS",
        )
    assert len(calls) == 1
    assert calls[0][1]["shell"] is False


def test_native_broker_nonzero_without_stderr_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        broker, "_resolve_workspace_and_cwd",
        lambda cwd: (tmp_path, tmp_path),
    )
    monkeypatch.setattr(
        broker, "_load_activation",
        lambda workspace: ({}, {"policy_revision": 1}, tmp_path / "broker.ps1"),
    )
    monkeypatch.setattr(broker, "_install_root", lambda: tmp_path)
    monkeypatch.setattr(
        broker.subprocess, "run",
        lambda *args, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(
        broker.TurboOSBrokerUnavailable,
        match=r"^TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED$",
    ):
        broker.execute_turbo_command("Write-Output hello", cwd=tmp_path)
