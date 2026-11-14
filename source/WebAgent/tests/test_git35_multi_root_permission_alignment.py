"""Git35 regression: global multi-root visibility and per-action authority.

Non-Windows test fakes NEVER attest a real AppContainer or modify host ACLs.
Actual kernel isolation is covered by the existing Windows Git62 CI tests.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import (
    turbo_sandbox, turbo_broker_admission, turbo_os_broker_client,
    workspace_access, task_workspace_resolver, remote_binding_manager,
    workspace_turbo_binding, workspace_manager,
)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    east = tmp_path / "east"
    desktop = tmp_path / "desktop"
    a = east / "turbo_a"
    b = desktop / "test"
    c = desktop / "remoteagent"
    project = c / "HDRViewer" / "sources" / "Androidviewer"
    for directory in (a, b, project):
        directory.mkdir(parents=True)
    policy = {
        "revision": 32, "turbo_requested": True,
        "writable_workspaces": [str(east), str(desktop)],
        "read_only_roots": [], "denied_write_roots": [],
        "turbo_roots": [str(a), str(b), str(c)],
    }
    snapshot = {
        "policy": policy, "writable_workspaces": tuple(policy["writable_workspaces"]),
        "read_only_roots": (), "turbo_roots": tuple(policy["turbo_roots"]),
    }
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: snapshot)
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "on")
    monkeypatch.setattr(workspace_access, "access_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        turbo_os_broker_client, "active_broker_available",
        lambda candidate: Path(candidate) in {a, b, c},
    )
    return SimpleNamespace(east=east, desktop=desktop, a=a, b=b, c=c,
                           project=project, policy=policy, access=snapshot)


def test_global_status_keeps_all_roots_without_selecting_cwd(roots):
    status = turbo_sandbox.global_turbo_snapshot(access=roots.access)
    assert status["configured_roots"] == [str(roots.a), str(roots.b), str(roots.c)]
    assert status["enrolled_root_count"] == 3
    assert status["turbo_effective"] == "PER_ACTION"
    assert status["authorization_granted"] is False
    assert all(row["native_enrollment"] == "ENROLLED_ADVISORY"
               for row in status["roots"])
    # A query for a broad parent is still ambiguous as an ACTION; the manager
    # must not display that error as the GLOBAL status.
    assert (turbo_sandbox.resolve_turbo_state(roots.desktop)["reason_code"]
            == "TURBO_MULTI_ROOT_CWD_REQUIRED")
    assert status["reason_code"] != "TURBO_MULTI_ROOT_CWD_REQUIRED"


def test_each_action_selects_its_own_root_not_transport_workspace(roots):
    result = turbo_broker_admission.admit_action(
        roots.a, {"tool": "run_command", "working_directory": str(roots.project),
                  "command": "(Get-Location).Path", "action_id": "ACT-GIT35"},
    )
    assert result["decision"] == "BROKER_REQUIRED"
    assert result["state"]["turbo_root"] == str(roots.c)
    assert result["authorization_workspace"] == str(roots.desktop)
    assert result["task_working_directory"] == str(roots.project)
    assert result["evidence"]["authorization_granted"] is False


def test_outside_root_cannot_be_admitted_even_with_other_roots_ready(roots):
    result = turbo_broker_admission.admit_action(
        roots.a, {"tool": "run_command",
                  "working_directory": str(roots.desktop),
                  "command": "Write-Output UNSAFE",
                  "action_id": "ACT-GIT35-OUTSIDE"},
    )
    assert result["decision"] == "BLOCKED"
    assert result["state"]["scope_verdict"] != "TURBO_SCOPE_ALLOWED"


def test_request_explicit_project_path_precedes_stale_recent_workspace(
    roots, monkeypatch,
):
    monkeypatch.setattr(task_workspace_resolver, "access_snapshot",
                        lambda: roots.access)
    monkeypatch.setattr(task_workspace_resolver, "request_paths",
                        lambda _text: [str(roots.project)])
    monkeypatch.setattr(task_workspace_resolver, "_recent_candidates",
                        lambda *_a, **_kw: [{
                            "task_id": "TASK-OLD", "workspace": str(roots.a)
                        }])
    selected = task_workspace_resolver.resolve_task_workspace(
        "compile APK", conversation_key="chat", transport="TELEGRAM",
        sender_id="owner", store=object(),
    )
    assert selected["source"] == "REQUEST_AUTHORIZED_TURBO_PATH"
    assert selected["workspace"] == str(roots.project)


def test_telegram_and_edit_workspace_use_same_global_roots(
    roots, tmp_path, monkeypatch,
):
    binding = {"workspace": str(roots.east),
               "gpt_url": "https://chatgpt.com/c/abcdef00-1234-1234-1234-123456789abc",
               "skill_path": str(tmp_path / "skills")}
    monkeypatch.setattr(remote_binding_manager, "load", lambda *_: binding)
    monkeypatch.setattr(remote_binding_manager, "access_snapshot",
                        lambda: roots.access)
    monkeypatch.setattr(remote_binding_manager, "turbo_mode_for_workspace",
                        lambda _ws: str(roots.a))
    telegram = remote_binding_manager.RemoteBindingManager(tmp_path)
    page = telegram.begin(chat_id=123)
    for path in (roots.a, roots.b, roots.c):
        assert str(path) in page
    assert "global_turbo_roots=" + ";".join(
        [str(roots.a), str(roots.b), str(roots.c)]) in page
    assert "\\nturbomode=" not in page
    assert "TURBO_MULTI_ROOT_CWD_REQUIRED" not in page

    monkeypatch.setattr(workspace_manager, "_management_access_snapshot",
                        lambda: {**roots.access, "security_status": "CONFIGURED"})
    monkeypatch.setattr(workspace_manager, "load_remote", lambda *_: binding)
    monkeypatch.setattr(workspace_manager, "load_startup_preferences", lambda: {})
    monkeypatch.setattr(workspace_manager, "load_gar_webgpt2", lambda: "")
    manager = workspace_manager._binding_snapshot()
    assert manager["turbo_sandbox"]["configured_roots"] == [
        str(roots.a), str(roots.b), str(roots.c),
    ]
    assert manager["turbo_sandbox"]["enrolled_root_count"] == 3


def test_unchanged_telegram_scope_does_not_reorder_global_registry(
    tmp_path, monkeypatch,
):
    east = tmp_path / "east"
    desktop = tmp_path / "desktop"
    a = east / "a"
    b = desktop / "b"
    c = east / "c"
    for path in (a, b, c):
        path.mkdir(parents=True)
    policy_file = tmp_path / "workspace_access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: policy_file)
    monkeypatch.setattr(workspace_access, "acl_mode", lambda: "on")
    monkeypatch.setattr("agent_core.windows_security.acl_mode", lambda: "on")
    workspace_access.save_workspace_access_policy([east, desktop], [])
    first = workspace_access.save_turbo_roots([str(a), str(b), str(c)])
    prepared = workspace_turbo_binding.prepare_turbo_mode_update(
        str(a) + ";" + str(c), east,
    )
    assert prepared["roots"] == [str(a), str(b), str(c)]
    after = workspace_turbo_binding.commit_turbo_mode_update(prepared)
    assert after["revision"] == first["revision"]
    assert after["turbo_roots"] == first["turbo_roots"]
