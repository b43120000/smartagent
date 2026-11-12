"""Git52 P0/P2: explicit executable trust is operator-pinned, never OS authority.

Uses disposable files and monkeypatched authorization snapshots. No native
AppContainer or host command is executed by these tests.
"""
from __future__ import annotations

import hashlib

from agent_core.command_security import inspect_command, inspect_native_command
from agent_core.turbo_execution_policy import native_trusted_tool_snapshot
from agent_core.windows_security import is_sha256_pinned_executable


def _profile(*files):
    return {
        "schema": "SMARTAGENT_WINDOWS_SECURITY_V2",
        "trusted_executables": [
            {"path": str(file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
            for file in files
        ],
    }


def test_native_external_script_requires_operator_sha256_pin(tmp_path, monkeypatch):
    root = tmp_path / "turbo"
    root.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    script = outside / "verify.py"
    script.write_text("print('check')\n", encoding="utf-8")
    import agent_core.windows_security as ws
    import agent_core.workspace_access as wa
    profile = _profile()
    monkeypatch.setattr(ws, "load_security_profile", lambda *a, **kw: profile)
    monkeypatch.setattr(wa, "access_snapshot", lambda *a, **kw: {
        "writable_workspaces": [tmp_path],
    })
    cmd = "python ../external/verify.py"
    # Normal workspace admission and Native Turbo have different trust rules.
    assert inspect_command(cmd, workspace=root).allowed
    rejected = inspect_native_command(cmd, workspace=root)
    assert rejected.allowed is False
    assert rejected.code == "execution_target_outside_workspace"
    assert rejected.target_kind == "SCRIPT"
    profile["trusted_executables"] = _profile(script)["trusted_executables"]
    assert inspect_native_command(cmd, workspace=root).allowed
    script.write_text("print('changed')\n", encoding="utf-8")
    assert inspect_native_command(cmd, workspace=root).code == (
        "execution_target_outside_workspace"
    )


def test_native_sha256_pin_independent_of_host_acl_off(tmp_path, monkeypatch):
    candidate = tmp_path / "git.exe"
    candidate.write_bytes(b"git-test-fixture")
    profile = _profile(candidate)
    import agent_core.windows_security as ws
    monkeypatch.setattr(ws, "acl_mode", lambda *args, **kwargs: "off")
    assert is_sha256_pinned_executable(candidate, profile)
    # A malicious edit after enrollment invalidates the pin.
    candidate.write_bytes(b"modified")
    assert not is_sha256_pinned_executable(candidate, profile)


def test_native_compound_command_checks_all_external_targets(tmp_path, monkeypatch):
    root = tmp_path / "turbo"
    root.mkdir()
    outer = tmp_path / "outer"
    outer.mkdir()
    good = outer / "good.py"
    bad = outer / "bad.py"
    good.write_bytes(b"print(1)")
    bad.write_bytes(b"print(2)")
    import agent_core.windows_security as ws
    import agent_core.workspace_access as wa
    profile = _profile(good)
    monkeypatch.setattr(ws, "load_security_profile", lambda *a, **kw: profile)
    monkeypatch.setattr(wa, "access_snapshot", lambda *a, **kw: {
        "writable_workspaces": [tmp_path],
    })
    command = "python ../outer/good.py; python ../outer/bad.py"
    result = inspect_native_command(command, workspace=root)
    assert not result.allowed
    assert result.code == "execution_target_outside_workspace"
    profile["trusted_executables"] = _profile(good, bad)["trusted_executables"]
    assert inspect_native_command(command, workspace=root).allowed


def test_native_hint_reports_hash_trust_not_os_execution(tmp_path, monkeypatch):
    git = tmp_path / "git.exe"
    git.write_bytes(b"pinned-git")
    profile = _profile(git)
    import agent_core.windows_security as ws
    monkeypatch.setattr(ws, "load_security_profile", lambda *a, **kw: profile)
    snapshot = native_trusted_tool_snapshot()
    assert snapshot["git"] == {
        "operator_sha256_pin_present": True,
        "trusted_executable_verified": True,
        "native_execution_verified": False,
    }
    assert snapshot["python"]["trusted_executable_verified"] is False
    git.write_bytes(b"changed-git")
    after = native_trusted_tool_snapshot()
    assert after["git"]["operator_sha256_pin_present"] is True
    assert after["git"]["trusted_executable_verified"] is False


def test_native_hard_security_still_denied_without_pinned_target(tmp_path):
    root = tmp_path / "turbo"
    root.mkdir()
    result = inspect_native_command("diskpart", workspace=root)
    assert not result.allowed
    assert result.code == "diskpart_forbidden"
