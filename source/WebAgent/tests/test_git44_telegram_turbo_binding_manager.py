"""Git44: Telegram WebGPT/Workspace manager Turbo form and shared policy.

No real Telegram, OS ACL or local SmartAgent state is modified by these tests.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from agent_core import remote_binding_manager, workspace_access
from agent_core.remote_binding_manager import (
    RemoteBindingManager, UPDATE_CLOSE, UPDATE_OPEN,
)
from agent_core.workspace_turbo_binding import (
    turbo_mode_for_workspace, commit_turbo_mode_update,
)


@pytest.fixture
def form_env(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    project = ws / "project"
    ws2 = tmp_path / "workspace-two"
    project2 = ws2 / "project-two"
    outside = tmp_path / "read-only"
    skills = tmp_path / "skills"
    for path in (ws, project, ws2, project2, outside, skills):
        path.mkdir()
    policy_path = tmp_path / "workspace_access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path",
                        lambda: policy_path)
    workspace_access.save_workspace_access_policy([ws, ws2], [outside])
    binding = {
        "workspace": str(ws),
        "gpt_url": "https://chatgpt.com/c/abcdef00-1234-1234-1234-123456789abc",
        "skill_path": str(skills),
    }
    monkeypatch.setattr(remote_binding_manager, "load", lambda root: dict(binding))
    def access_view():
        policy = workspace_access.load_workspace_access_policy()
        return {
            "policy": policy,
            "writable_workspaces": tuple(policy["writable_workspaces"]),
            "read_only_roots": tuple(policy["read_only_roots"]),
            "turbo_roots": tuple(policy["turbo_roots"]),
        }
    monkeypatch.setattr(remote_binding_manager, "access_snapshot", access_view)
    def require_writable(value):
        candidate = str(Path(value).resolve())
        if candidate not in access_view()["writable_workspaces"]:
            raise ValueError("workspace_not_authorized")
        return candidate
    monkeypatch.setattr(remote_binding_manager, "require_writable_workspace",
                        require_writable)
    manager = RemoteBindingManager(tmp_path)
    return manager, binding, project, project2, outside


def new_form(manager, turbo=None, chat_id=123):
    rendered = manager.begin(chat_id=chat_id)
    assert rendered.count("turbomode=") == 1
    block = rendered[rendered.index(UPDATE_OPEN):rendered.index(UPDATE_CLOSE)
                     + len(UPDATE_CLOSE)]
    if turbo is not None:
        line = next(v for v in block.splitlines() if v.startswith("turbomode="))
        block = block.replace(line, "turbomode=" + str(turbo))
    return block


def fake_host(seen):
    def apply(binding):
        seen.append(dict(binding))
        prepared = binding.get("_turbo_prepared")
        if prepared is not None:
            commit_turbo_mode_update(prepared)
    return apply


def test_default_off_and_shared_policy(form_env):
    manager, binding, project, project2, outside = form_env
    block = new_form(manager)
    assert "turbomode=off" in block
    assert manager._parse_update(block)["turbomode"] == "off"
    seen = []
    result = manager.apply(block, chat_id=123, runtime_apply=fake_host(seen))
    assert "Turbo Mode：off" in result
    assert "_turbo_prepared" in seen[0]
    assert turbo_mode_for_workspace(binding["workspace"]) == "off"


def test_enable_and_manager_reads_back_value(form_env):
    manager, binding, project, project2, outside = form_env
    seen = []
    result = manager.apply(
        new_form(manager, turbo=project),
        chat_id=123, runtime_apply=fake_host(seen),
    )
    assert f"Turbo Mode：{project}" in result
    assert turbo_mode_for_workspace(binding["workspace"]) == str(project)
    assert f"turbomode={project}" in manager.begin(chat_id=123)


def test_fenced_single_line_is_accepted(form_env):
    manager, binding, project, project2, outside = form_env
    fence = chr(96) * 3
    block = new_form(manager).replace(
        "turbomode=off", fence + "\nturbomode=off\n" + fence,
    )
    assert manager._parse_update(block)["turbomode"] == "off"


def test_old_form_without_turbo_does_not_change_setting(form_env):
    manager, binding, project, project2, outside = form_env
    workspace_access.set_turbo_root(project, workspace=binding["workspace"])
    block = new_form(manager)
    assert f"turbomode={project}" in block
    legacy = block.replace(f"turbomode={project}\n", "")
    assert "turbomode" not in manager._parse_update(legacy)
    seen = []
    manager.apply(legacy, chat_id=123, runtime_apply=fake_host(seen))
    assert "_turbo_prepared" not in seen[0]
    assert turbo_mode_for_workspace(binding["workspace"]) == str(project)


@pytest.mark.parametrize("bad", ["root", "outside", "other", "relative"])
def test_invalid_turbo_cannot_mutate_binding(form_env, bad):
    manager, binding, project, project2, outside = form_env
    value = {"root": binding["workspace"], "outside": str(outside),
             "other": str(project2), "relative": "relative-path"}[bad]
    seen = []
    with pytest.raises(ValueError, match="turbo"):
        manager.apply(
            new_form(manager, turbo=value),
            chat_id=123, runtime_apply=fake_host(seen),
        )
    assert not seen
    assert turbo_mode_for_workspace(binding["workspace"]) == "off"


def test_stale_transaction_fails_after_workspace_manager_edit(form_env):
    manager, binding, project, project2, outside = form_env
    block = new_form(manager, turbo=project)
    workspace_access.set_turbo_root(project2, workspace=str(project2.parent))
    seen = []
    with pytest.raises(ValueError, match="policy_stale"):
        manager.apply(block, chat_id=123, runtime_apply=fake_host(seen))
    assert not seen
    assert turbo_mode_for_workspace(binding["workspace"]) == "off"
    assert turbo_mode_for_workspace(str(project2.parent)) == str(project2)


def test_one_workspace_off_keeps_other_workspace_turbo(form_env):
    manager, binding, project, project2, outside = form_env
    workspace_access.set_turbo_root(project2, workspace=str(project2.parent))
    manager.apply(new_form(manager, turbo=project), chat_id=123,
                  runtime_apply=fake_host([]))
    assert turbo_mode_for_workspace(binding["workspace"]) == str(project)
    assert turbo_mode_for_workspace(str(project2.parent)) == str(project2)
    manager.apply(new_form(manager, turbo="off"), chat_id=123,
                  runtime_apply=fake_host([]))
    assert turbo_mode_for_workspace(binding["workspace"]) == "off"
    assert turbo_mode_for_workspace(str(project2.parent)) == str(project2)


def test_chat_identity_and_single_use_transaction(form_env):
    manager, binding, project, project2, outside = form_env
    block = new_form(manager, turbo=project)
    seen = []
    with pytest.raises(ValueError, match="chat_mismatch"):
        manager.apply(block, chat_id=444, runtime_apply=fake_host(seen))
    assert seen == []
    manager.apply(block, chat_id=123, runtime_apply=fake_host(seen))
    with pytest.raises(ValueError, match="transaction_unknown"):
        manager.apply(block, chat_id=123, runtime_apply=fake_host(seen))


def test_wrong_fence_rejected(form_env):
    manager, binding, project, project2, outside = form_env
    fence = chr(96) * 3
    block = new_form(manager).replace(
        "turbomode=off", fence + "\nworkspace=bad\n" + fence,
    )
    with pytest.raises(ValueError, match="turbo_fence_invalid|duplicate_field"):
        manager._parse_update(block)


def test_host_without_policy_commit_is_detected(form_env):
    manager, binding, project, project2, outside = form_env
    block = new_form(manager, turbo=project)
    seen = []
    with pytest.raises(RuntimeError, match="turbo_postcondition_failed"):
        manager.apply(block, chat_id=123, runtime_apply=lambda v: seen.append(v))
    assert len(seen) == 1


def test_symlink_escape_rejected(form_env):
    manager, binding, project, project2, outside = form_env
    link = project / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    seen = []
    with pytest.raises(ValueError, match="turbo"):
        manager.apply(new_form(manager, turbo=link), chat_id=123,
                      runtime_apply=fake_host(seen))
    assert not seen
