"""Git49: first OS-broker admission tests, before production sandbox enablement."""
from __future__ import annotations

from types import SimpleNamespace
import pytest
from agent_core import turbo_sandbox, turbo_broker_admission


@pytest.fixture
def gate(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    turbo = workspace / "turbo"
    turbo.mkdir(parents=True)
    policy = {
        "revision": 11, "turbo_requested": False,
        "turbo_roots": [str(turbo)], "denied_write_roots": [],
        "sandbox_status": "READY", "sandbox_attestation_passed": True,
    }
    access = {"policy": policy, "turbo_roots": (str(turbo),),
              "writable_workspaces": (str(workspace),), "read_only_roots": ()}
    acl = {"mode": "off"}
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: acl["mode"])
    return workspace, turbo, policy, acl


@pytest.mark.parametrize("mode,requested,decision", [
    ("off", False, "NORMAL"), ("off", True, "NORMAL"),
    ("on", False, "NORMAL"), ("on", True, "BLOCKED"),
])
def test_matrix(gate, mode, requested, decision):
    workspace, _, policy, acl = gate
    acl["mode"] = mode
    policy["turbo_requested"] = requested
    result = turbo_broker_admission.assess(workspace, tool="run_command", action_id="A49")
    assert result["decision"] == decision
    assert result["state"]["turbo_effective"] == "OFF"
    assert result["evidence"]["action_id"] == "A49"
    assert result["evidence"]["executed"] is False
    assert result["evidence"]["authorization_granted"] is False


def test_unknown_acl_blocks_even_if_requested_off(gate):
    workspace, _, policy, acl = gate
    acl["mode"] = "unknown"
    for requested in [False, True]:
        policy["turbo_requested"] = requested
        result = turbo_broker_admission.assess(workspace, tool="write_file")
        assert result["decision"] == "BLOCKED"


def test_policy_failure_cannot_fallback(gate, monkeypatch):
    workspace, _, policy, acl = gate
    acl["mode"] = "off"
    policy["turbo_requested"] = False
    monkeypatch.setattr(turbo_sandbox, "access_snapshot",
                        lambda: (_ for _ in ()).throw(ValueError("corrupt")))
    assert turbo_broker_admission.assess(workspace, tool="run_command")["decision"] == "BLOCKED"


def test_unverified_os_broker_prevents_host_process(gate, monkeypatch):
    from agent_core.bounded_process import run_bounded_process
    workspace, _, policy, acl = gate
    acl["mode"] = "on"
    policy["turbo_requested"] = True
    monkeypatch.setattr("agent_core.windows_security.restricted_executor_required",
                        lambda: (_ for _ in ()).throw(AssertionError("executor selected")))
    result = run_bounded_process(["python", "-c", "print('UNSAFE')"], cwd=workspace)
    assert result["exit_code"] is None
    assert result["process_started"] is False
    assert result["security_rejected"] is True
    assert result["error"].startswith("TURBO_OS_BROKER_BLOCKED:")
    assert result["sandbox_evidence"]["executed"] is False


def test_typed_write_not_dispatched_as_controller(gate):
    from agent_core.tools import execute_tool
    workspace, turbo, policy, acl = gate
    acl["mode"] = "on"
    policy["turbo_requested"] = True
    target = turbo / "uncreated.txt"
    result = execute_tool(
        {"tool": "write_file", "path": str(target),
         "content": "UNSAFE", "action_id": "ACT-49"},
        SimpleNamespace(workspace=str(workspace)),
    )
    assert not target.exists()
    assert "TURBO_OS_BROKER_BLOCKED" in result
    assert "NOT_EXECUTED" in result


def test_read_only_diagnostics_are_not_turbo_mutators():
    for tool in ("read_file", "observe_fact", "inspect_directory",
                 "list_directory", "find_file", "query_project"):
        assert not turbo_broker_admission.needs_broker(tool)
    for tool in ("run_command", "write_file", "create_directory", "delete_path",
                 "write_file_chunk", "apply_edit_plan", "project_sync"):
        assert turbo_broker_admission.needs_broker(tool)


def test_stale_on_acl_off_does_not_block(gate):
    workspace, _, policy, acl = gate
    acl["mode"] = "off"
    policy["turbo_requested"] = True
    result = turbo_broker_admission.assess(workspace, tool="write_file")
    assert result["decision"] == "NORMAL"
    assert result["reason_code"] == "TURBO_SUPPRESSED_ACL_OFF"


def test_fake_policy_ready_is_not_an_os_grant(gate):
    workspace, _, policy, acl = gate
    acl["mode"] = "on"
    policy["turbo_requested"] = True
    policy["turbo_effective"] = "ON"
    result = turbo_broker_admission.assess(workspace, tool="run_command")
    assert result["decision"] == "BLOCKED"
    assert result["state"]["sandbox_status"] != "READY"
