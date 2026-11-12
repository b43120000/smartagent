"""Regression: task working directory must not replace OS authorization parent.

These tests do NOT claim Windows AppContainer token or kernel isolation PASS.
"""
from __future__ import annotations

from pathlib import Path

from agent_core import (
    bounded_process, turbo_broker_admission, turbo_execution_policy,
    turbo_os_broker_client, turbo_sandbox, workspace_access,
)


def _setup(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    turbo = ws / "turbo"
    project = turbo / "project"
    other = ws / "other"
    project.mkdir(parents=True)
    other.mkdir()
    access = {
        "policy": {
            "revision": 19, "turbo_requested": True,
            "turbo_roots": [str(turbo)], "denied_write_roots": [],
        },
        "writable_workspaces": (str(ws),),
        "read_only_roots": (),
        "turbo_roots": (str(turbo),),
    }
    for module in (workspace_access, turbo_sandbox, turbo_execution_policy):
        monkeypatch.setattr(module, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "on")
    monkeypatch.setattr(
        turbo_os_broker_client, "active_broker_available",
        lambda workspace: workspace is not None and
        Path(workspace).resolve() == ws.resolve(),
    )
    return ws, turbo, project, other, access


def test_unique_protected_authorization_parent_for_task_and_cwd(tmp_path, monkeypatch):
    ws, turbo, project, other, access = _setup(tmp_path, monkeypatch)
    for directory in (ws, turbo, project, other):
        assert workspace_access.resolve_turbo_authorization_workspace(
            directory, snapshot=access,
        ) == ws
    outside = tmp_path / "outside"
    outside.mkdir()
    assert workspace_access.resolve_turbo_authorization_workspace(
        outside, snapshot=access,
    ) is None
    access["writable_workspaces"] = (str(ws), str(turbo))
    assert workspace_access.resolve_turbo_authorization_workspace(
        project, snapshot=access,
    ) is None  # Nested writable parents are ambiguous: fail closed.


def test_planner_enrollment_for_child_workspace_is_advisory_only(tmp_path, monkeypatch):
    ws, turbo, project, _, _ = _setup(tmp_path, monkeypatch)
    for working in (ws, turbo, project):
        hint = turbo_execution_policy.turbo_planning_snapshot(working)
        assert hint["status"] == "BROKER_ENROLLED"
        assert hint["authorization_workspace"] == str(ws)
        assert hint["task_working_directory"] == str(working)
        assert hint["configured_roots"] == [str(turbo)]
        assert "run_command" in hint["native_broker_tools"]
        assert hint["turbo_effective"] == "OFF"
        assert hint["authorization_granted"] is False


def test_admission_uses_parent_even_when_task_is_root(tmp_path, monkeypatch):
    ws, turbo, project, _, _ = _setup(tmp_path, monkeypatch)
    for task_directory in (ws, turbo, project):
        admission = turbo_broker_admission.assess(
            task_directory, tool="run_command",
        )
        assert admission["decision"] == "BROKER_REQUIRED"
        assert admission["authorization_workspace"] == str(ws)
        assert admission["state"]["scope_verdict"] == "TURBO_SCOPE_ALLOWED"
        assert admission["evidence"]["executed"] is False


def test_outside_policy_never_uses_host_fallback(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    admission = turbo_broker_admission.assess(outside, tool="run_command")
    assert admission["decision"] == "BLOCKED"
    monkeypatch.setattr(
        bounded_process.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HOST_FALLBACK_FORBIDDEN")),
    )
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Get-Date"],
        cwd=outside, security_command="Get-Date",
    )
    assert result["security_rejected"] is True
    assert result["process_started"] is False
    assert result["exit_code"] is None


def test_root_cwd_enters_only_native_broker_and_keeps_cwd(tmp_path, monkeypatch):
    ws, turbo, _, other, _ = _setup(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(
        bounded_process.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HOST_EXECUTOR_MUST_NOT_RUN")),
    )

    def native(command, *, cwd, timeout, max_stream_bytes, task_id, action_id):
        seen.append((command, str(cwd), task_id, action_id))
        return {
            "exit_code": 0, "stdout": "NATIVE_TEST_STUB", "stderr": "",
            "process_started": True, "security_rejected": False,
            "sandbox_evidence": {"os_execution_result": "MOCK_ONLY"},
        }
    monkeypatch.setattr(turbo_os_broker_client, "execute_turbo_command", native)
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Get-Date"],
        cwd=turbo, security_command="Get-Date",
        telemetry={"task_id": "TASK-TEST", "action_id": "ACT-TEST"},
    )
    assert result["exit_code"] == 0
    assert seen == [("Get-Date", str(turbo), "TASK-TEST", "ACT-TEST")]

    seen.clear()
    rejection = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Get-Date"],
        cwd=other, security_command="Get-Date",
    )
    assert rejection["security_rejected"] is True
    assert rejection["process_started"] is False
    assert "TURBO_OS_BROKER_CWD_OUTSIDE_ROOT" in rejection["error"]
    assert seen == []


def test_broker_attestation_failure_stays_blocked(tmp_path, monkeypatch):
    _, turbo, _, _, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        turbo_os_broker_client, "execute_turbo_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            turbo_os_broker_client.TurboOSBrokerUnavailable(
                "TURBO_OS_BROKER_ATTESTATION_INVALID")),
    )
    monkeypatch.setattr(
        bounded_process.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HOST_FALLBACK_FORBIDDEN")),
    )
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Get-Date"],
        cwd=turbo, security_command="Get-Date",
    )
    assert result["security_rejected"] is True
    assert result["process_started"] is False
    assert "ATTESTATION_INVALID" in result["error"]


def test_acl_off_still_suppresses_turbo(tmp_path, monkeypatch):
    _, turbo, _, _, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "off")
    result = turbo_broker_admission.assess(turbo, tool="run_command")
    assert result["decision"] == "NORMAL"
    assert result["state"]["turbo_effective"] == "OFF"
