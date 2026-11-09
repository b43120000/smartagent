"""Git41: command-family grants remain task-scoped and path-only."""
from pathlib import Path
from types import SimpleNamespace

from agent_core.command_family_approval import (
    TaskCommandFamilyGrants,
    command_family_signature,
)
from RemoteAgent.remote_feature_catalog import security_approval_inline_keyboard


def action(command, root, *, operation="GENERAL", cwd=None):
    return {"tool": "run_command", "operation": operation, "command": command,
            "working_directory": str(cwd or root)}


def loop(root, *, task="TASK-ONE", owner="telegram:123:456", cwd=None):
    return SimpleNamespace(
        task_id=task, run_id="RR-ONE", task_epoch="EPOCH-ONE",
        continuation_owner=owner, workspace=cwd or root,
    )


def test_workspace_path_change_reuses_after_recorded_execution(tmp_path):
    a = tmp_path / "case_a"
    b = tmp_path / "case_b"
    src = action(f'Get-ChildItem -LiteralPath "{a}"', tmp_path)
    dst = action(f'Get-ChildItem -LiteralPath "{b}"', tmp_path)
    grants = TaskCommandFamilyGrants(tmp_path)
    current = loop(tmp_path)
    assert command_family_signature(src, str(tmp_path))
    assert command_family_signature(src, str(tmp_path)) == command_family_signature(dst, str(tmp_path))
    assert grants.match(loop=current, action=dst) is None
    assert grants.remember(loop=current, action=src, approval_id="APR-ONE", evidence_ref="MIN-EV-OK")
    reloaded = TaskCommandFamilyGrants(tmp_path)
    grant = reloaded.match(loop=current, action=dst)
    assert grant["state"] == "APPROVED"
    assert grant["reason"] == "same_task_command_family_reused"
    assert grant["approval_id"] == "APR-ONE"


def test_cannot_reuse_across_task_owner_or_epoch(tmp_path):
    path = tmp_path / "case_a"
    a = action(f'Get-ChildItem -Path "{path}"', tmp_path)
    store = TaskCommandFamilyGrants(tmp_path)
    assert store.remember(loop=loop(tmp_path), action=a, approval_id="APR-ONE", evidence_ref="MIN-EV-OK")
    assert store.match(loop=loop(tmp_path, task="TASK-TWO"), action=a) is None
    assert store.match(loop=loop(tmp_path, owner="telegram:999:456"), action=a) is None
    changed = loop(tmp_path)
    changed.task_epoch = "EPOCH-TWO"
    assert store.match(loop=changed, action=a) is None


def test_changed_command_structure_requires_new_approval(tmp_path):
    a = action(f'Get-Content -LiteralPath "{tmp_path / "a.json"}"', tmp_path)
    b = action(f'Get-Content -LiteralPath "{tmp_path / "b.json"}"', tmp_path)
    flags = action(f'Get-Content -Raw -LiteralPath "{tmp_path / "b.json"}"', tmp_path)
    other_operation = action(b["command"], tmp_path, operation="COMPILE")
    assert command_family_signature(a, str(tmp_path)) == command_family_signature(b, str(tmp_path))
    assert command_family_signature(a, str(tmp_path)) != command_family_signature(flags, str(tmp_path))
    assert command_family_signature(a, str(tmp_path)) != command_family_signature(other_operation, str(tmp_path))


def test_outside_workspace_and_syntax_fail_closed(tmp_path):
    a = action(f'Get-Content -Path "{tmp_path / "a.json"}"', tmp_path)
    assert command_family_signature(a, str(tmp_path))
    outside = action('Get-Content -Path "C:\\outside\\secrets.json"', tmp_path)
    assert command_family_signature(outside, str(tmp_path)) != command_family_signature(a, str(tmp_path))
    chained = action(a["command"] + "; del everything", tmp_path)
    assert command_family_signature(chained, str(tmp_path)) is None
    escalated = action(f'Remove-Item -Path "{tmp_path / "a.json"}"', tmp_path)
    assert command_family_signature(escalated, str(tmp_path)) is None
    assert command_family_signature(action("cmd /c dir", tmp_path), str(tmp_path)) is None


def test_script_executable_and_cwd_changes_are_not_a_path_family(tmp_path):
    first = action(f'python check.py "{tmp_path / "case_a"}"', tmp_path)
    second = action(f'python other.py "{tmp_path / "case_b"}"', tmp_path)
    same_script_new_input = action(f'python check.py "{tmp_path / "case_b"}"', tmp_path)
    other_cwd = action(same_script_new_input["command"], tmp_path, cwd=tmp_path / "other")
    assert command_family_signature(first, str(tmp_path)) != command_family_signature(second, str(tmp_path))
    assert command_family_signature(first, str(tmp_path)) == command_family_signature(same_script_new_input, str(tmp_path))
    assert command_family_signature(first, str(tmp_path)) != command_family_signature(other_cwd, str(tmp_path))
    assert command_family_signature(action("python -c print(1)", tmp_path), str(tmp_path)) is None


def test_git_clone_repo_url_is_not_variable_path(tmp_path):
    a = action(f'git clone https://github.com/example/first "{tmp_path / "first"}"', tmp_path)
    changed_url = action(f'git clone https://github.com/evil/second "{tmp_path / "second"}"', tmp_path)
    changed_target = action(f'git clone https://github.com/example/first "{tmp_path / "second"}"', tmp_path)
    assert command_family_signature(a, str(tmp_path))
    assert command_family_signature(a, str(tmp_path)) != command_family_signature(changed_url, str(tmp_path))
    assert command_family_signature(a, str(tmp_path)) == command_family_signature(changed_target, str(tmp_path))
    assert command_family_signature(action("git reset --hard", tmp_path), str(tmp_path)) is None


def test_telegram_approval_label_discloses_family_scope():
    label = security_approval_inline_keyboard("APR-ONE", approval_kind="RESUME_UNKNOWN_COMMAND")
    assert label["inline_keyboard"][0][0]["text"] == "允許本任務同類指令"
    assert security_approval_inline_keyboard("APR-ONE", approval_kind="UNKNOWN_COMMAND")["inline_keyboard"][0][0]["text"] == "允許本任務同類指令"
    assert security_approval_inline_keyboard("APR-ONE", approval_kind="EXECUTION")["inline_keyboard"][0][0]["text"] == "允許執行一次"
