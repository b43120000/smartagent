"""Git51: Telegram task Workspace/Turbo Broker enrollment binding regression.

A Runtime task bound to a true child of an enrolled Turbo Root may use that
Root's *existing* registered parent authority. A typo, broad ancestor,
sibling workspace or reparse path may NEVER be silently rebound.
"""
from __future__ import annotations

import pytest

from agent_core import (
    workspace_access, turbo_sandbox, turbo_execution_policy,
    turbo_broker_admission,
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    top = tmp_path / "volume"
    registered = top / "AIspace"
    root = registered / "SmartAgent_Turbo_Test"
    child = root / "case_a"
    sibling = registered / "other_project"
    for p in (child, sibling):
        p.mkdir(parents=True)
    policy = {
        "revision": 19, "turbo_requested": True, "turbo_roots": [str(root)],
        "writable_workspaces": [str(registered)],
        "denied_write_roots": [],
    }
    access = {
        "policy": policy,
        "writable_workspaces": (str(registered),),
        "turbo_roots": (str(root),),
        "read_only_roots": (),
    }
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "on")
    monkeypatch.setattr(turbo_execution_policy, "access_snapshot", lambda: access)
    return top, registered, root, child, sibling, access


@pytest.mark.parametrize("task", ["parent", "root", "child"])
def test_only_registered_parent_or_turbo_descendant_may_use_broker(tree, task):
    _, registered, root, child, _, access = tree
    path = {"parent": registered, "root": root, "child": child}[task]
    canonical = workspace_access.resolve_turbo_registered_workspace(path, access)
    assert canonical == str(registered)
    state = turbo_sandbox.resolve_turbo_state(path)
    assert state["scope_verdict"] == "TURBO_SCOPE_ALLOWED"
    assert state["workspace_root"] == str(registered)
    assert state["task_workspace_root"] == str(path)
    assert state["turbo_root"] == str(root)
    assert state["policy_revision"] == 19
    assert state["turbo_effective"] == "OFF"  # Enrollment != kernel attestation.


@pytest.mark.parametrize("task", ["sibling", "ancestor", "typo"])
def test_nonmatching_telegram_binding_stays_fail_closed(tree, task):
    top, registered, root, _, sibling, access = tree
    invalid = {
        "sibling": sibling,
        "ancestor": top,
        "typo": top / "Alspace",  # l vs capital I; MUST NOT guess.
    }[task]
    assert workspace_access.resolve_turbo_registered_workspace(invalid, access) is None
    state = turbo_sandbox.resolve_turbo_state(invalid)
    assert state["reason_code"] == "TURBO_ROOT_NOT_BOUND_TO_ACTIVE_WORKSPACE"
    assert state["scope_verdict"] == "TURBO_SCOPE_DENIED"
    assert state["turbo_root"] == ""
    # Important diagnostic distinction: configured Policy is not empty.
    assert state["policy_turbo_roots"] == [str(root)]
    assert state["registered_workspace_roots"] == [str(registered)]
    assert state["task_workspace_root"] == str(invalid)
    result = turbo_broker_admission.assess(
        invalid, tool="run_command", action_id="ACT-LIVE",
    )
    assert result["decision"] == "BLOCKED"
    assert result["evidence"]["executed"] is False
    assert result["evidence"]["authorization_granted"] is False


def test_nested_turbo_workspace_in_planner_hint_with_enrolled_broker(tree, monkeypatch):
    _, registered, root, child, _, _ = tree
    monkeypatch.setattr(
        turbo_broker_admission, "assess",
        lambda *_args, **kwargs: {"decision": "BROKER_REQUIRED"},
    )
    hint = turbo_execution_policy.turbo_planning_snapshot(child)
    assert hint["status"] == "BROKER_ENROLLED", hint
    assert hint["native_broker_tools"] == [
        "run_command", "create_directory", "write_file", "delete_path"
    ]
    assert hint["configured_roots"] == [str(root)]
    assert hint["broker_registered_workspace"] == str(registered)
    assert hint["task_workspace_root"] == str(child)
    assert hint["authorization_granted"] is False


def test_wrong_workspace_has_diagnostic_not_false_empty_policy(tree, monkeypatch):
    top, registered, root, _, _, _ = tree
    monkeypatch.setattr(
        turbo_broker_admission, "assess",
        lambda *_args, **kwargs: {"decision": "BLOCKED"},
    )
    hint = turbo_execution_policy.turbo_planning_snapshot(top)
    assert hint["configured_roots"] == []
    assert hint["policy_turbo_roots"] == [str(root)]
    assert hint["registered_workspace_roots"] == [str(registered)]
    assert hint["task_workspace_root"] == str(top)
    assert hint["binding_diagnostic"] == "TASK_WORKSPACE_OUTSIDE_CONFIGURED_TURBO_SCOPE"
    assert hint["native_broker_tools"] == []


def test_broker_workspace_binding_rejects_symlinked_task_root(tree):
    _, registered, root, _, sibling, access = tree
    link = root / "alias_to_other"
    try:
        link.symlink_to(sibling, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable without Windows developer/admin rights")
    assert workspace_access.resolve_turbo_registered_workspace(link, access) is None

def test_cli_binding_diagnostics_does_not_unseal_secrets_or_grant_authority(
    tree, monkeypatch,
):
    from agent_core import turbo_binding_diagnostics
    _, registered, root, child, _, access = tree
    monkeypatch.setattr(turbo_binding_diagnostics, "access_snapshot", lambda: access)
    monkeypatch.setattr(
        turbo_binding_diagnostics,
        "active_broker_available",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        turbo_binding_diagnostics, "_configured_workspace",
        lambda *_args: str(registered),
    )
    info = turbo_binding_diagnostics.snapshot(str(child))
    assert info["schema"] == "SMARTAGENT_TURBO_TASK_BINDING_DIAGNOSTIC_V1"
    assert info["binding_status"] == "SCOPE_MATCHED"
    assert info["registered_broker_workspace"] == str(registered)
    assert info["policy_turbo_roots"] == [str(root)]
    assert info["actual_running_task_workspace"] == "NOT_OBSERVED_BY_THIS_CLI"
    assert info["untrusted_command_authorized"] is False
    assert info["policy_or_acl_changed"] is False
    assert "bot_token" not in str(info).lower()
