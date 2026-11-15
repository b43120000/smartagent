"""Git62 edit-workspace P0 integration contract (no privileged OS writes)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core import workspace_manager as manager
from agent_core.turbo_root_registry import journal_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    install = tmp_path / "smartagent"
    scripts = install / "install_smart_agent"
    scripts.mkdir(parents=True)
    for name in ("turbo_existing_root_activate.ps1", "turbo_auto_activate.ps1",
                 "turbo_existing_root_preflight.ps1",
                 "turbo_os_activation.ps1", "turbo_multi_root_rebind.ps1"):
        (scripts / name).write_text("# mocked process", encoding="utf-8")
    ws = tmp_path / "workspace"
    a, b = ws / "alpha", ws / "beta"
    for x in (a, b):
        x.mkdir(parents=True)
        (x / "original.txt").write_text("ORIGINAL", encoding="utf-8")
    policy = {
        "revision": 2, "turbo_requested": False,
        "writable_workspaces": [str(ws)], "read_only_roots": [],
        "denied_write_roots": [], "turbo_roots": [],
    }
    secure = install / "localdata" / "secure" / "windows_security"
    calls = []
    events = []
    monkeypatch.setattr(manager, "AGENT_PROJECT_ROOT", install)
    monkeypatch.setattr(manager, "_acl_off_enabled", lambda: False)
    monkeypatch.setattr(manager, "_is_admin", lambda: True)
    monkeypatch.setattr(manager, "_management_access_snapshot",
                        lambda: {"security_status": "READY"})
    monkeypatch.setattr(manager, "acl_mode", lambda: "on")
    monkeypatch.setattr(manager, "_write_apply_log", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "load_workspace_access_policy", lambda: dict(policy))
    monkeypatch.setattr(manager, "access_snapshot", lambda: {
        "profile": {"denied_write_roots": []}, "policy": dict(policy),
        "writable_workspaces": list(policy["writable_workspaces"]),
        "read_only_roots": [], "turbo_roots": list(policy["turbo_roots"]),
    })
    monkeypatch.setattr(manager, "is_valid_turbo_root",
                        lambda path, owner, *_args: (
                            Path(owner) in Path(path).parents and
                            not Path(path).is_symlink()
                        ))
    monkeypatch.setattr(manager, "_apply_access", lambda *_args: 0)
    monkeypatch.setattr("agent_core.turbo_os_broker_client.active_broker_available",
                        lambda *_a: True)
    def save(roots):
        events.append(("policy_commit", policy["revision"]))
        policy["turbo_roots"] = list(roots)
        policy["turbo_requested"] = bool(roots)
        policy["revision"] += 1
        return dict(policy)
    monkeypatch.setattr(manager, "save_turbo_roots", save)

    def run(argv, **_kwargs):
        calls.append(list(argv))
        script = Path(argv[argv.index("-File") + 1]).name
        events.append(("native", script, policy["revision"], list(argv)))
        if script == "turbo_existing_root_activate.ps1":
            root = argv[argv.index("-TurboRoot") + 1]
            j = journal_path(secure, root)
            j.parent.mkdir(parents=True, exist_ok=True)
            j.write_text(json.dumps({
                "schema": "SMARTAGENT_TURBO_OS_ACTIVATION_V1",
                "turbo_root": root, "workspace_root": str(ws),
                "status": "ACTIVE_PROVISIONED",
                "policy_revision": policy["revision"],
            }), encoding="utf-8")
        if script == "turbo_os_activation.ps1":
            root = argv[argv.index("-TurboRoot") + 1]
            journal_path(secure, root).unlink()
        if script == "turbo_multi_root_rebind.ps1" and "-RepairMissingSentinels" not in argv:
            for root in policy["turbo_roots"]:
                j = journal_path(secure, root)
                if j.is_file():
                    entry = json.loads(j.read_text(encoding="utf-8"))
                    entry["policy_revision"] = policy["revision"]
                    j.write_text(json.dumps(entry), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="native accepted\n", stderr="")
    monkeypatch.setattr(manager.subprocess, "run", run)
    def select(*values):
        replies = iter(values)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    return SimpleNamespace(ws=ws, a=a, b=b, policy=policy, calls=calls,
                           events=events, select=select, secure=secure, install=install)


def test_add_two_existing_roots_one_apply_and_preserve_user_files(env):
    env.select("5", str(env.a), "5", str(env.b), "7", "APPLY", "")
    assert manager._security_menu() == 0
    assert env.policy["turbo_roots"] == [str(env.a), str(env.b)]
    assert all((x / "original.txt").read_text() == "ORIGINAL"
               for x in (env.a, env.b))
    assert journal_path(env.secure, env.a).is_file()
    assert journal_path(env.secure, env.b).is_file()
    commands = [str(c) for c in env.calls]
    assert sum("turbo_existing_root_activate.ps1" in c for c in commands) == 2


def test_reapply_after_two_enrollments_is_a_noop(env):
    env.select("5", str(env.a), "5", str(env.b), "7", "APPLY", "")
    assert manager._security_menu() == 0
    count = len(env.calls)
    env.select("7", "")
    assert manager._security_menu() == 0
    assert len(env.calls) == count


def test_deny_overlapping_roots_before_native_execution(env):
    sub = env.a / "nested"
    sub.mkdir()
    env.select("5", str(env.a), "5", str(sub), "7", "")
    assert manager._security_menu() == 1
    assert env.policy["turbo_roots"] == []
    assert not env.calls


def test_revoke_only_removed_root_before_registry_commit(env):
    env.select("5", str(env.a), "5", str(env.b), "7", "APPLY", "")
    assert manager._security_menu() == 0
    env.calls.clear()
    env.select("6", "2", "7", "APPLY", "")
    assert manager._security_menu() == 0
    assert env.policy["turbo_roots"] == [str(env.a)]
    assert journal_path(env.secure, env.a).exists()
    assert not journal_path(env.secure, env.b).exists()
    assert any("turbo_os_activation.ps1" in str(call) for call in env.calls)
    preflight = next(i for i, row in enumerate(env.events)
                     if row[0] == "native" and row[1] == "turbo_multi_root_rebind.ps1"
                     and "-RepairMissingSentinels" in row[3])
    revoke = next(i for i, row in enumerate(env.events)
                  if i > preflight and row[0] == "native"
                  and row[1] == "turbo_os_activation.ps1")
    commit = next(i for i, row in enumerate(env.events)
                  if i > revoke and row[0] == "policy_commit")
    assert preflight < revoke < commit
    assert env.events[preflight][2] == env.events[commit][1]
    assert env.policy["revision"] == env.events[commit][1] + 1


def test_failed_retained_precheck_keeps_policy_and_existing_roots(env, monkeypatch):
    env.select("5", str(env.a), "5", str(env.b), "7", "APPLY", "")
    assert manager._security_menu() == 0
    revision = env.policy["revision"]
    previous_events = len(env.events)
    original = manager.subprocess.run

    def fail(argv, **kwargs):
        if "-RepairMissingSentinels" in argv:
            return SimpleNamespace(returncode=3, stdout="",
                                   stderr="SIMULATED_KERNEL_DENIAL_TEST_FAILURE")
        return original(argv, **kwargs)

    monkeypatch.setattr(manager.subprocess, "run", fail)
    env.select("6", "2", "7", "APPLY", "")
    assert manager._security_menu() == 1
    assert env.policy["revision"] == revision
    assert env.policy["turbo_roots"] == [str(env.a), str(env.b)]
    assert journal_path(env.secure, env.a).exists()
    assert journal_path(env.secure, env.b).exists()
    assert not any(row[1] == "turbo_os_activation.ps1"
                   for row in env.events[previous_events:] if row[0] == "native")
    assert (env.a / "original.txt").read_text() == "ORIGINAL"
    assert (env.b / "original.txt").read_text() == "ORIGINAL"


def test_operator_does_not_confirm_os_mutations(env):
    env.select("5", str(env.a), "5", str(env.b), "7", "DECLINE", "")
    assert manager._security_menu() == 1
    assert not env.calls
    assert env.policy["turbo_roots"] == []


def test_edit_workspace_shows_both_turbo_paths_after_apply(env, capsys):
    """End-user acceptance: Edit_workspace manager lists T1 and the new tool T2."""
    env.select("5", str(env.a), "5", str(env.b), "7", "APPLY", "")
    assert manager._security_menu() == 0
    env.select("8")
    assert manager._security_menu() == 0
    displayed = capsys.readouterr().out
    assert "T1. " + str(env.a) in displayed
    assert "T2. " + str(env.b) in displayed
    assert all(x in env.policy["turbo_roots"] for x in (str(env.a), str(env.b)))
