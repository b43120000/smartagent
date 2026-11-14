"""Git62 shared Turbo registry: additive Telegram/WebGPT operator setting.

These tests change disposable policy JSON only. They never grant NTFS or
Windows AppContainer rights; that requires the elevated Workspace Manager.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from agent_core import workspace_access, workspace_turbo_binding


@pytest.fixture
def registry(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws2 = tmp_path / "other-workspace"
    a, b, foreign = ws / "alpha", ws / "beta", ws2 / "foreign"
    for path in (a, b, foreign):
        path.mkdir(parents=True)
    policy_file = tmp_path / "workspace_access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: policy_file)
    monkeypatch.setattr(workspace_access, "acl_mode", lambda: "on")
    monkeypatch.setattr("agent_core.windows_security.acl_mode", lambda: "on")
    workspace_access.save_workspace_access_policy([ws, ws2], [])
    return ws, a, b, ws2, foreign


def update(mode, ws):
    prepared = workspace_turbo_binding.prepare_turbo_mode_update(mode, ws)
    assert prepared is not None
    return workspace_turbo_binding.commit_turbo_mode_update(prepared)


def test_additive_two_roots_same_workspace_and_display(registry):
    ws, a, b, _, _ = registry
    update(str(a), ws)
    update(str(b), ws)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == [
        str(a), str(b),
    ]
    assert workspace_turbo_binding.turbo_mode_for_workspace(ws) == (
        str(a) + ";" + str(b)
    )


def test_duplicate_add_does_not_increment_policy_revision(registry):
    ws, a, _, _, _ = registry
    first = update(str(a), ws)
    second = update(str(a), ws)
    assert second["revision"] == first["revision"]
    assert second["turbo_roots"] == [str(a)]


def test_remove_one_and_off_preserves_other_workspace(registry):
    ws, a, b, ws2, foreign = registry
    update(str(a), ws)
    update(str(b), ws)
    update(str(foreign), ws2)
    update("-" + str(a), ws)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == [
        str(foreign), str(b),
    ]
    update("off", ws)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == [
        str(foreign),
    ]


def test_semicolon_form_replaces_only_current_workspace(registry):
    ws, a, b, ws2, foreign = registry
    update(str(foreign), ws2)
    update(str(a) + ";" + str(b), ws)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == [
        str(foreign), str(a), str(b),
    ]


def test_nested_registration_cannot_widen_scope(registry):
    ws, a, b, _, _ = registry
    sub = a / "nested"
    sub.mkdir()
    update(str(a), ws)
    with pytest.raises(ValueError, match="turbo_roots_overlap"):
        update(str(sub), ws)
    assert workspace_access.load_workspace_access_policy()["turbo_roots"] == [str(a)]


def test_invalid_outside_root_rejected_without_registry_mutation(registry, tmp_path):
    ws, a, b, _, _ = registry
    external = tmp_path / "outside"
    external.mkdir()
    with pytest.raises(ValueError, match="turbo_root_not_strict_writable_workspace_child"):
        update(str(external), ws)
    assert not workspace_access.load_workspace_access_policy()["turbo_roots"]
