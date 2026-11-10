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
def test_acl_off_overrides_stale_turbo_request_without_bypassing_acl_on(gate, mode, requested):
    workspace, root, _, policy, acl = gate
    policy["turbo_requested"] = requested
    acl["value"] = mode
    result = turbo_sandbox.resolve_turbo_state(workspace)
    assert result["acl_mode"] == mode
    assert result["turbo_requested"] == ("ON" if requested else "OFF")
    assert result["turbo_effective"] == "OFF"
    assert result["sandbox_attestation_passed"] is False
    assert result["authorization_granted"] is False
    if mode == "off":
        assert result["status"] == "TURBO_DISABLED"
        assert result["reason_code"] == (
            "TURBO_SUPPRESSED_ACL_OFF" if requested else "TURBO_DISABLED"
        )
        turbo_sandbox.require_turbo_backend(result)
    elif requested:
        assert result["reason_code"] == "SANDBOX_UNAVAILABLE"
        with pytest.raises(RuntimeError, match="SANDBOX_UNAVAILABLE"):
            turbo_sandbox.require_turbo_backend(result)
    else:
        assert result["reason_code"] == "TURBO_DISABLED"
        turbo_sandbox.require_turbo_backend(result)


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


@pytest.mark.parametrize("requested", [False, True])
def test_acl_mode_unknown_is_never_treated_as_off(gate, requested):
    workspace, _, _, policy, acl = gate
    policy["turbo_requested"] = requested
    acl["value"] = "unknown"
    state = turbo_sandbox.resolve_turbo_state(workspace)
    assert state["sandbox_status"] == "FAILED"
    assert state["reason_code"] == "ACL_MODE_UNVERIFIED"
    with pytest.raises(RuntimeError, match="ACL_MODE_UNVERIFIED"):
        turbo_sandbox.require_turbo_backend(state)


def test_acl_off_corrupt_policy_is_not_ignored(gate, monkeypatch):
    workspace, _, _, policy, acl = gate
    acl["value"] = "off"
    policy["turbo_requested"] = True
    monkeypatch.setattr(turbo_sandbox, "access_snapshot",
                        lambda: (_ for _ in ()).throw(ValueError("bad-policy")))
    state = turbo_sandbox.resolve_turbo_state(workspace)
    assert state["sandbox_status"] == "FAILED"
    with pytest.raises(RuntimeError, match="SANDBOX_ATTESTATION_FAILED"):
        turbo_sandbox.require_turbo_backend(state)


def test_acl_off_stale_on_cli_and_planner_show_inactive(gate, monkeypatch, capsys):
    workspace, _, _, policy, acl = gate
    acl["value"] = "off"
    policy["turbo_requested"] = True
    assert turbo_sandbox.main(["--workspace", str(workspace)]) == 0
    import json
    reported = json.loads(capsys.readouterr().out)
    assert reported["turbo_requested"] == "ON"  # persisted preference
    assert reported["turbo_effective"] == "OFF"  # actual execution route
    assert reported["reason_code"] == "TURBO_SUPPRESSED_ACL_OFF"
    monkeypatch.setattr(turbo_execution_policy, "access_snapshot",
                        turbo_sandbox.access_snapshot)
    hint = turbo_execution_policy.turbo_planning_snapshot(workspace)
    assert hint["status"] == "OFF"
    assert hint["turbo_effective"] == "OFF"
    assert hint["safe_action_examples"] == []
    decision = turbo_execution_policy.evaluate_turbo_confirmation(
        SimpleNamespace(workspace=str(workspace)), {
            "tool": "run_command", "operation": "GIT_INSPECT",
            "working_directory": str(workspace), "command": "git status",
        })
    assert decision["auto_confirm"] is False
    assert decision["reason_code"] == "TURBO_SUPPRESSED_ACL_OFF"


def test_acl_off_stale_on_regular_command_really_executes(gate, tmp_path, monkeypatch):
    """Use the real ACL mode reader and ordinary software executor, not a fake PASS."""
    import json
    import sys
    from agent_core.bounded_process import run_bounded_process
    from agent_core.windows_security import restricted_executor_required

    workspace, _, _, policy, acl = gate
    acl["value"] = "off"
    policy["turbo_requested"] = True
    install = tmp_path / "fake_install"
    profile = install / "localdata" / "secure" / "windows_security" / "security_profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")
    profile.with_name("acl_mode.json").write_text(json.dumps({
        "schema": "SMARTAGENT_ACL_MODE_V1",
        "mode": "off",
        "install_root": str(install.resolve()),
    }), encoding="utf-8")
    monkeypatch.setenv("SMARTAGENT_SECURITY_PROFILE", str(profile))
    monkeypatch.setenv("SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED", "1")
    assert restricted_executor_required() is False
    state = turbo_sandbox.resolve_turbo_state(workspace)
    turbo_sandbox.require_turbo_backend(state)
    result = run_bounded_process(
        [sys.executable, "-c", "print('ACL_OFF_TURBO_OFF_PASS')"],
        cwd=workspace, capture_root=workspace, timeout=10,
    )
    assert result["exit_code"] == 0, result
    assert result["process_started"] is True
    assert "ACL_OFF_TURBO_OFF_PASS" in result["stdout"], result


def test_acl_off_stale_on_client_queue_not_turbo_blocked(gate, tmp_path, monkeypatch):
    """A stale ON cannot incorrectly veto a legacy client; no real service starts."""
    import json
    from agent_core import restricted_executor_client as client
    workspace, _, _, policy, acl = gate
    acl["value"] = "off"
    policy["turbo_requested"] = True
    endpoint = tmp_path / "restricted_endpoint"
    endpoint.mkdir()
    monkeypatch.setattr(client, "load_security_profile",
                        lambda: {"executor_endpoint": str(endpoint)})
    real_write = client.write_json_atomic

    def queue_with_fake_result(path, payload):
        real_write(path, payload)
        destination = endpoint / "results"
        destination.mkdir(exist_ok=True)
        (destination / path.name).write_text(json.dumps({
            "result": {"exit_code": 0, "stdout": "QUEUE_PROCESSED"}
        }), encoding="utf-8")

    monkeypatch.setattr(client, "write_json_atomic", queue_with_fake_result)
    answer = client.execute_restricted("git status", cwd=workspace, timeout=1)
    assert answer["exit_code"] == 0
    assert answer["stdout"] == "QUEUE_PROCESSED"
    assert len(list((endpoint / "requests").glob("*.json"))) == 1


def test_acl_off_stale_on_service_reaches_standard_digest_gate(gate):
    """Service defense-in-depth must not mistake an inactive preference for Turbo."""
    from agent_core.restricted_executor_service import _run
    workspace, _, _, policy, acl = gate
    acl["value"] = "off"
    policy["turbo_requested"] = True
    with pytest.raises(RuntimeError, match="restricted_executor_request_digest_mismatch"):
        _run({"cwd": str(workspace), "request_digest": "invalid"}, {})


def test_acl_off_inconsistent_state_still_rejected():
    state = {
        "sandbox_status": "NOT_READY",
        "acl_mode": "off",
        "turbo_requested": "ON",
        "turbo_effective": "ON",
        "authorization_granted": True,
    }
    with pytest.raises(RuntimeError, match="ACL_OFF_TURBO_STATE_INCONSISTENT"):
        turbo_sandbox.require_turbo_backend(state)
