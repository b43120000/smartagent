"""Git43: cross-interface Turbo policy, project index and software registry."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import workspace_access, turbo_execution_policy
from agent_core.turbo_settings_command import (
    parse_turbo_control, apply_turbo_control, turbo_control_reply,
)
from agent_core.software_registry import (
    register_verified_software_probe, read_software_registry,
    software_registry_snapshot,
)
from agent_core.action_dependencies import command_is_proven_observation
from WebAgent.minimal_core_loop import tool_catalog


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    parent = tmp_path / "workspace"
    turbo = parent / "project"
    other = tmp_path / "other"
    parent.mkdir()
    turbo.mkdir()
    other.mkdir()
    store = tmp_path / "access-policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: store)
    workspace_access.save_workspace_access_policy([parent], [other])
    return parent, turbo, other


def test_turbo_is_off_by_default_and_can_be_enabled_by_explicit_user_control(
    workspace, monkeypatch,
):
    parent, turbo, other = workspace
    before = workspace_access.load_workspace_access_policy()
    assert before["turbo_roots"] == []
    assert parse_turbo_control(f"turbomode={turbo}") == str(turbo)
    assert parse_turbo_control("Set turbomode=/tmp/foo") is None
    assert parse_turbo_control(f"turbomode={turbo}\nrun command") is None
    result = apply_turbo_control(f"turbomode={turbo}", workspace=parent)
    assert result["status"] == "ENABLED"
    assert "Turbo Mode ENABLED" in turbo_control_reply(result)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == [str(turbo)]
    again = workspace_access.load_workspace_access_policy()
    assert again["turbo_roots"] == [str(turbo)]
    disabled = apply_turbo_control("turbomode=off", workspace=parent)
    assert disabled["status"] == "DISABLED"
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == []


@pytest.mark.parametrize("target", ["workspace", "other", "missing"])
def test_turbo_cannot_escape_workspace_or_use_root(workspace, target, tmp_path):
    parent, turbo, other = workspace
    path = {"workspace": parent, "other": other,
            "missing": parent / "missing"}[target]
    with pytest.raises(ValueError):
        apply_turbo_control("turbomode=" + str(path), workspace=parent)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == []


def test_turbo_secondary_approval_is_not_unrestricted_authority(workspace, monkeypatch):
    parent, turbo, other = workspace
    workspace_access.set_turbo_root(turbo, workspace=parent)
    policy = workspace_access.load_workspace_access_policy()
    monkeypatch.setattr(turbo_execution_policy, "access_snapshot", lambda: {
        "policy": policy,
        "turbo_roots": tuple(policy["turbo_roots"]),
        "writable_workspaces": tuple(policy["writable_workspaces"]),
        "read_only_roots": tuple(policy["read_only_roots"]),
    })
    loop = SimpleNamespace(workspace=parent)
    action = {"tool": "run_command", "working_directory": str(turbo),
              "command": "git ls-tree HEAD"}
    good = turbo_execution_policy.evaluate_turbo_confirmation(loop, action)
    assert good["auto_confirm"] is True
    assert good["authorization_granted"] is not True if "authorization_granted" in good else True
    assert good["turbo_root"] == str(turbo)
    for command in [
        "git ls-tree HEAD; Remove-Item something",
        "git ls-tree HEAD | Remove-Item something",
        "Get-Content -LiteralPath @(Write-Output 'bad')",
        "python script.py",
        "Remove-Item -Path foo",
        "git reset --hard",
        "git ls-tree HEAD $(whoami)",
        "git ls-tree HEAD " + chr(96) + "evil",
    ]:
        bad = turbo_execution_policy.evaluate_turbo_confirmation(
            loop, {**action, "command": command}
        )
        assert bad["auto_confirm"] is False, command
    outside = turbo_execution_policy.evaluate_turbo_confirmation(
        loop, {**action, "working_directory": str(parent)}
    )
    assert outside["auto_confirm"] is False


def test_hash_comparison_is_observation_but_not_compound():
    path = r"E:\SmartAgent_GoalDriven_IT_GH_v1\case_a\data.csv"
    sha = "a" * 64
    command = f"(Get-FileHash -LiteralPath '{path}' -Algorithm SHA256).Hash -eq '{sha}'"
    assert command_is_proven_observation({"command": command, "operation": "VERIFY"})
    for invalid in [
        command + "; Remove-Item -LiteralPath 'E:\\other.csv'",
        command + " | Write-Output 'x'",
        command.replace("-eq", "-ge"),
        command.replace(sha, "bad-hash"),
    ]:
        assert not command_is_proven_observation({
            "command": invalid, "operation": "VERIFY",
        })


def test_project_sync_local_index_visible_without_browser_upload():
    context = SimpleNamespace(
        interface_name="remote",
        web_file_capabilities=lambda: {"browser_upload": False,
                                       "browser_download": False},
    )
    tools = {item["name"]: item for item in tool_catalog(context)}
    assert "project_sync" in tools
    assert tools["project_sync"]["allowed_strategies"] == ["INDEX_ONLY"]
    assert "upload_file" not in tools
    context.web_file_capabilities = lambda: {
        "browser_upload": True, "browser_download": True,
    }
    enabled = {item["name"]: item for item in tool_catalog(context)}
    assert enabled["project_sync"]["allowed_strategies"] == [
        "INDEX_ONLY", "FULL_BUNDLE", "DELTA",
    ]


def test_software_registry_only_records_actual_executed_version_evidence(
    tmp_path, monkeypatch,
):
    from agent_core import software_registry
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(software_registry, "software_registry_path",
                        lambda root=None: registry)
    binary = tmp_path / "tool.exe"
    binary.write_bytes(b"test version probe executable placeholder")
    action = {"tool": "run_command",
              "command": f'"{binary}" --version', "operation": "INSPECT"}
    receipt = {"status": "SUCCEEDED", "executed": True,
               "data": {"process_state": "EXITED", "exit_code": 0,
                        "timed_out": False}}
    assert read_software_registry()["records"] == {}
    assert register_verified_software_probe(action, receipt, "MIN-EV-123") is not None
    items = software_registry_snapshot()["items"]
    assert len(items) == 1
    assert items[0]["path_hint"] == str(binary)
    assert items[0]["availability"] == "RECORDED_REPROBE_REQUIRED"
    assert items[0]["execution_authorized"] is False
    assert register_verified_software_probe(
        action, {**receipt, "status": "FAILED"}, "MIN-EV-456") is None
    assert register_verified_software_probe(
        {**action, "command": f'"{binary}" --version; Remove-Item foo'},
        receipt, "MIN-EV-456") is None
    binary.unlink()
    assert software_registry_snapshot()["items"][0]["availability"] == "MISSING"
