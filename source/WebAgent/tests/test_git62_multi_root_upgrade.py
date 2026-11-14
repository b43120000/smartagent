"""Git62 regression: Git upgrade checks every Native Root independently.

No host process, security profile mutation or live ProgramData access.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core import force_stop_all_agents as cleanup
from agent_core import security_preflight, windows_security, workspace_access
from agent_core import turbo_os_broker_client as broker


@pytest.fixture
def upgrade_fixture(tmp_path, monkeypatch):
    root = tmp_path / "install"
    sec = root / "localdata" / "secure" / "windows_security"
    sec.mkdir(parents=True)
    (sec / "security_profile.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cleanup, "_verify_upgrade_commit", lambda *args: None)
    monkeypatch.setattr(windows_security, "acl_mode", lambda *args, **kwargs: "on")
    monkeypatch.setattr(security_preflight, "preflight", lambda *args: {"status": "PASS"})
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    roots = [str(tmp_path / "workspace" / "root1"),
             str(tmp_path / "workspace" / "root2")]
    monkeypatch.setattr(
        workspace_access, "load_workspace_access_policy",
        lambda: {"turbo_requested": True,
                 "writable_workspaces": [str(tmp_path / "workspace")],
                 "turbo_roots": roots},
    )
    return root, roots


def test_upgrade_checks_all_roots_not_their_parent(
    upgrade_fixture, monkeypatch,
):
    root, roots = upgrade_fixture
    seen = []
    monkeypatch.setattr(cleanup, "_machine_manifest_status",
                        lambda _: "MATCH")

    def active(path):
        seen.append(str(path))
        return str(path) in roots

    monkeypatch.setattr(broker, "active_broker_available", active)

    def no_reprovision(*args, **kwargs):
        raise AssertionError("valid Native roots must not be re-provisioned")

    monkeypatch.setattr(cleanup.subprocess, "run", no_reprovision)
    cleanup._apply_git_upgrade(root, "a" * 40)
    assert seen == roots


def test_manifest_mismatch_requires_admin_restore_and_rechecks_every_root(
    upgrade_fixture, monkeypatch,
):
    root, roots = upgrade_fixture
    machine_status = iter(("machine_authorization_manifest_mismatch", "MATCH"))
    monkeypatch.setattr(cleanup, "_machine_manifest_status",
                        lambda _: next(machine_status))
    seen = []
    monkeypatch.setattr(broker, "active_broker_available",
                        lambda path: seen.append(str(path)) or str(path) in roots)
    invocations = []

    def fake_admin(argv, **kwargs):
        invocations.append((argv, kwargs))
        return None

    monkeypatch.setattr(cleanup.subprocess, "run", fake_admin)
    cleanup._apply_git_upgrade(root, "b" * 40)
    assert len(invocations) == 1
    args = invocations[0][0]
    assert "-RestoreTurboAfterUpgrade" in args
    assert "acl_mode_launcher.ps1" in " ".join(args)
    assert seen == roots


def test_missing_second_root_is_fail_closed_even_if_first_is_available(
    upgrade_fixture, monkeypatch,
):
    root, roots = upgrade_fixture
    monkeypatch.setattr(cleanup, "_machine_manifest_status",
                        lambda _: "MATCH")
    checked = []
    monkeypatch.setattr(
        broker, "active_broker_available",
        lambda path: checked.append(str(path)) or str(path) == roots[0],
    )
    invoked = []
    monkeypatch.setattr(cleanup.subprocess, "run",
                        lambda *args, **kwargs: invoked.append(args))
    with pytest.raises(RuntimeError, match="force_stop_upgrade_turbo_restore_unverified"):
        cleanup._apply_git_upgrade(root, "c" * 40)
    assert invoked, "requested privileged restore of a missing Native enrollment"
    assert roots[1] in checked
