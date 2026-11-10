"""Git46 unit/contract tests. These are NOT Windows OS-isolation PASS."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core import workspace_access
from agent_core import turbo_sandbox
from agent_core import turbo_execution_policy


@pytest.fixture
def gate(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    root = workspace / "turbo"
    sibling = workspace / "other_project"
    workspace.mkdir()
    root.mkdir()
    sibling.mkdir()
    policy = {
        "revision": 21,
        "turbo_requested": False,
        "turbo_roots": [str(root)],
        "denied_write_roots": [],
        # Spoofed fields must NEVER become authorization.
        "sandbox_status": "READY",
        "sandbox_attestation_passed": True,
    }
    access = {
        "policy": policy,
        "turbo_roots": tuple(policy["turbo_roots"]),
        "writable_workspaces": (str(workspace),),
        "read_only_roots": (),
    }
    mode = {"value": "off"}
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: mode["value"])
    return workspace, root, sibling, policy, mode


@pytest.mark.parametrize("mode,requested", [
    ("off", False), ("off", True), ("on", False), ("on", True),
])
def test_all_acl_turbo_combinations_fail_closed(gate, mode, requested):
    workspace, root, _, policy, acl = gate
    policy["turbo_requested"] = requested
    acl["value"] = mode
    result = turbo_sandbox.resolve_turbo_state(workspace)
    assert result["acl_mode"] == mode
    assert result["turbo_requested"] == ("ON" if requested else "OFF")
    assert result["turbo_effective"] == "OFF"
    assert result["sandbox_attestation_passed"] is False
    assert result["authorization_granted"] is False
    if requested and mode == "off":
        assert result["reason_code"] == "TURBO_REQUIRES_ACL_ON"
    elif requested and mode == "on":
        assert result["reason_code"] == "SANDBOX_UNAVAILABLE"
    else:
        assert result["reason_code"] == "TURBO_DISABLED"


def test_spoofed_ready_in_policy_never_becomes_effective(gate):
    workspace, _, _, policy, acl = gate
    policy["turbo_requested"] = True
    acl["value"] = "on"
    state = turbo_sandbox.resolve_turbo_state(workspace)
    assert state["sandbox_status"] == "NOT_READY"
    with pytest.raises(RuntimeError, match="SANDBOX_UNAVAILABLE"):
        turbo_sandbox.require_turbo_backend(state)


def test_attestation_failure_cannot_dispatch(gate, monkeypatch):
    workspace, _, _, policy, acl = gate
    policy["turbo_requested"] = True
    acl["value"] = "on"
    monkeypatch.setattr(turbo_sandbox, "access_snapshot",
                        lambda: (_ for _ in ()).throw(RuntimeError("missing")))
    state = turbo_sandbox.resolve_turbo_state(workspace)
    assert state["sandbox_status"] == "FAILED"
    assert state["status"] == "SANDBOX_ATTESTATION_FAILED"
    with pytest.raises(RuntimeError):
        turbo_sandbox.require_turbo_backend(state)


def test_receipt_cannot_be_misread_as_os_access_denied(gate):
    workspace, _, _, policy, acl = gate
    acl["value"] = "on"
    policy["turbo_requested"] = True
    state = turbo_sandbox.resolve_turbo_state(workspace)
    receipt = turbo_sandbox.sandbox_evidence(
        state, action_id="ACT-1", requested_operation="GIT_MUTATE")
    assert receipt["action_id"] == "ACT-1"
    assert receipt["executed"] is False
    assert receipt["os_execution_result"] == "NOT_EXECUTED"
    assert receipt["os_access_denied"] is False
    assert receipt["failure_reason_code"] == "SANDBOX_UNAVAILABLE"
    assert "command" not in receipt


def test_scope_conflict_does_not_enable_broker(gate, monkeypatch):
    workspace, _, _, policy, acl = gate
    acl["value"] = "on"
    policy["turbo_requested"] = True
    monkeypatch.setattr(turbo_sandbox, "is_valid_turbo_root",
                        lambda *args, **kwargs: False)
    state = turbo_sandbox.resolve_turbo_state(workspace)
    assert state["scope_verdict"] == "TURBO_SCOPE_DENIED"
    assert state["reason_code"] == "TURBO_ROOT_NOT_BOUND_TO_ACTIVE_WORKSPACE"


def test_git45_auto_confirmation_is_disabled_without_os_backend(gate):
    workspace, root, _, policy, acl = gate
    acl["value"] = "on"
    policy["turbo_requested"] = True
    loop = SimpleNamespace(workspace=str(workspace), task_request="git status")
    decision = turbo_execution_policy.evaluate_turbo_confirmation(loop, {
        "tool": "run_command", "operation": "GIT_INSPECT",
        "working_directory": str(root), "command": "git status",
    })
    assert decision["auto_confirm"] is False
    assert decision["reason_code"] == "SANDBOX_UNAVAILABLE"


def test_acl_off_rejects_turbo_enable_before_persist(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    root = workspace / "turbo"
    workspace.mkdir()
    root.mkdir()
    policy_file = tmp_path / "workspace_access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: policy_file)
    workspace_access.save_workspace_access_policy([workspace], [])
    monkeypatch.setattr(workspace_access, "acl_mode", lambda: "off")
    with pytest.raises(ValueError, match="TURBO_REQUIRES_ACL_ON"):
        workspace_access.set_turbo_root(root, workspace=workspace)
    assert workspace_access.load_workspace_access_policy()["turbo_requested"] is False


def test_acl_on_request_persists_but_does_not_grant(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    root = workspace / "turbo"
    workspace.mkdir()
    root.mkdir()
    policy_file = tmp_path / "workspace_access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: policy_file)
    monkeypatch.setattr(workspace_access, "acl_mode", lambda: "on")
    workspace_access.save_workspace_access_policy([workspace], [])
    saved = workspace_access.set_turbo_root(root, workspace=workspace)
    assert saved["turbo_requested"] is True
    new = workspace_access.save_workspace_access_policy([workspace], [])
    assert new["turbo_requested"] is False
    assert new["revision"] > saved["revision"]


def test_git45_root_only_policy_migrates_requested_off(tmp_path, monkeypatch):
    import json
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    turbo = workspace / "turbo"
    turbo.mkdir()
    policy_file = tmp_path / "workspace_access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: policy_file)
    policy_file.write_text(json.dumps({
        "schema": workspace_access.ACCESS_POLICY_SCHEMA, "revision": 3,
        "writable_workspaces": [str(workspace)],
        "read_only_roots": [], "denied_write_roots": [],
        "turbo_roots": [str(turbo)],
    }), encoding="utf-8")
    state = workspace_access.load_workspace_access_policy()
    assert state["turbo_roots"] == [str(turbo)]
    assert state["turbo_requested"] is False


def test_restricted_client_fails_before_queue_write(gate, monkeypatch):
    from agent_core import restricted_executor_client as client
    workspace, _, _, policy, acl = gate
    acl["value"] = "on"
    policy["turbo_requested"] = True
    monkeypatch.setattr(client, "load_security_profile",
                        lambda: (_ for _ in ()).throw(AssertionError("must not load")))
    with pytest.raises(client.RestrictedExecutorUnavailable, match="TURBO_SANDBOX_BLOCKED"):
        client.execute_restricted(["git", "status"], cwd=str(workspace))
