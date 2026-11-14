"""Git61: an existing directory requires operator approval, never mkdir or host fallback."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import workspace_manager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    install = tmp_path / "smartagent_install"
    activate = install / "install_smart_agent" / "turbo_existing_root_activate.ps1"
    activate.parent.mkdir(parents=True)
    activate.write_text("# test-only dummy native path", encoding="utf-8")
    workspace = tmp_path / "workspace"
    root = workspace / "existing_project"
    root.mkdir(parents=True)
    original = root / "keep.txt"
    original.write_bytes(b"GIT61_EXISTING_FILE_MUST_SURVIVE")
    policy = {
        "revision": 1, "writable_workspaces": [str(workspace)],
        "read_only_roots": [], "turbo_roots": [],
        "turbo_requested": False,
    }
    native_calls = []
    saved_roots = []

    monkeypatch.setattr(workspace_manager, "AGENT_PROJECT_ROOT", install)
    monkeypatch.setattr(workspace_manager, "_acl_off_enabled", lambda: False)
    monkeypatch.setattr(workspace_manager, "_is_admin", lambda: True)
    monkeypatch.setattr(workspace_manager, "_management_access_snapshot",
                        lambda: {"security_status": "READY"})
    monkeypatch.setattr(workspace_manager, "access_snapshot",
                        lambda: {
                            "profile": {"denied_write_roots": []},
                            "writable_workspaces": list(policy["writable_workspaces"]),
                            "read_only_roots": [],
                            "turbo_roots": list(policy["turbo_roots"]),
                            "policy": dict(policy),
                        })
    monkeypatch.setattr(workspace_manager, "load_workspace_access_policy",
                        lambda: dict(policy))
    monkeypatch.setattr(workspace_manager, "acl_mode", lambda: "on")
    monkeypatch.setattr(workspace_manager, "is_valid_turbo_root",
                        lambda p, ws, writable, read_only, denied: (
                            Path(p).is_dir() and Path(ws) in Path(p).parents
                        ))
    monkeypatch.setattr(workspace_manager, "_write_apply_log",
                        lambda *_args, **_kwargs: None)

    def no_acl_reset(*_args, **_kwargs):
        raise AssertionError("unchanged writable roots must not reapply NTFS ACL")
    monkeypatch.setattr(workspace_manager, "_apply_access", no_acl_reset)

    def save_roots(roots):
        saved_roots.append(list(roots))
        policy["turbo_roots"] = list(roots)
        policy["turbo_requested"] = bool(roots)
        policy["revision"] += 1
        return dict(policy)
    monkeypatch.setattr(workspace_manager, "save_turbo_roots", save_roots)

    def native(args, **kwargs):
        native_calls.append((list(args), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout="enrolled\n", stderr="")
    monkeypatch.setattr(workspace_manager.subprocess, "run", native)

    def provide(*values):
        incoming = iter(values)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(incoming))

    return SimpleNamespace(
        workspace=workspace, root=root, original=original,
        policy=policy, calls=native_calls, saves=saved_roots, provide=provide,
        activate=activate,
    )


def test_existing_directory_can_enroll_without_recreation(manager):
    original = manager.original.read_bytes()
    manager.provide("5", str(manager.root), "7", str(manager.root), "")
    assert workspace_manager._security_menu() == 0
    assert manager.original.read_bytes() == original
    assert manager.root.is_dir()
    assert manager.saves == [[str(manager.root)]]
    assert len(manager.calls) == 1
    argv = manager.calls[0][0]
    assert str(manager.activate) in argv
    assert argv[argv.index("-TurboRoot") + 1] == str(manager.root)
    assert argv[argv.index("-ConfirmTarget") + 1] == str(manager.root)
    assert "turbo_auto_activate.ps1" not in " ".join(argv)


def test_existing_directory_wrong_confirmation_never_grants(manager):
    manager.provide("5", str(manager.root), "7", str(manager.workspace), "")
    assert workspace_manager._security_menu() == 1
    assert not manager.calls and not manager.saves
    assert manager.original.read_bytes() == b"GIT61_EXISTING_FILE_MUST_SURVIVE"


def test_existing_active_broker_cannot_be_replaced_by_apply(manager):
    active = manager.activate.parents[1] / "localdata" / "secure"
    active = active / "windows_security" / "turbo_os_activation.json"
    active.parent.mkdir(parents=True)
    active.write_text("{}", encoding="utf-8")
    manager.provide("5", str(manager.root), "7", "")
    assert workspace_manager._security_menu() == 1
    assert not manager.calls and not manager.saves
    assert active.exists()


def test_already_registered_but_not_requested_root_can_enroll(manager):
    manager.policy["turbo_roots"] = [str(manager.root)]
    manager.provide("7", str(manager.root), "")
    assert workspace_manager._security_menu() == 0
    assert manager.policy["turbo_requested"] is True
    assert len(manager.calls) == 1
    assert len(manager.saves) == 1


def test_nested_writable_workspace_ownership_is_rejected(manager):
    # An OS Broker must bind to precisely one registered Workspace.
    manager.policy["writable_workspaces"].append(str(manager.root.parent / "nested"))
    (manager.root.parent / "nested").mkdir()
    second = manager.root.parent / "nested" / "target"
    second.mkdir()
    # Root is strict child of exactly one Workspace in this fixture; append
    # the outer directory as a second registered root to create ambiguity.
    manager.policy["writable_workspaces"].append(str(manager.workspace.parent))
    manager.provide("5", str(second), "7", "")
    assert workspace_manager._security_menu() == 1
    assert not manager.calls and not manager.saves
