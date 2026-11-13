"""Git53: scoped native full access is separated from legacy command filters.

Offline tests: mocks NEVER establish Windows AppContainer isolation.
Windows live token/ACL/negative-access attestation remains mandatory.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import bounded_process, turbo_broker_admission, workspace_access
from agent_core.path_security import PathSecurityError
from agent_core.security_context import SecurityContext
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.turbo_execution_policy import evaluate_native_broker_confirmation
from WebAgent.minimal_admission import admit_minimal_action
from WebAgent.minimal_decision import MinimalDecision


def _gate(workspace, root):
    return {
        "decision": "BROKER_REQUIRED",
        "authorization_workspace": str(workspace),
        "state": {
            "acl_mode": "on", "turbo_requested": "ON",
            "scope_verdict": "TURBO_SCOPE_ALLOWED",
            "sandbox_status": "NOT_READY", "turbo_root": str(root),
            "policy_revision": 53,
        },
    }


def test_builtin_venv_read_only_even_if_install_is_editable(monkeypatch, tmp_path):
    install = tmp_path / "install"
    venv = install / ".venv"
    exe = venv / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"unit-test")
    profile = install / "localdata/secure/windows_security/security_profile.json"
    monkeypatch.setattr(workspace_access, "default_profile_path", lambda: profile)
    assert workspace_access.builtin_toolchain_read_roots() == (str(venv.resolve()),)
    context = SecurityContext.for_workspace(install, read_roots=(venv,))
    assert context.require_read(exe) == exe
    with pytest.raises(PathSecurityError, match="builtin_toolchain_read_only"):
        context.require_write(exe)
    with pytest.raises(PathSecurityError):
        context.require_read(tmp_path / "private/other.txt")


def test_builtin_venv_absent_does_not_enroll_arbitrary_path(monkeypatch, tmp_path):
    install = tmp_path / "install"
    profile = install / "localdata/secure/windows_security/security_profile.json"
    monkeypatch.setattr(workspace_access, "default_profile_path", lambda: profile)
    assert workspace_access.builtin_toolchain_read_roots() == ()


@pytest.mark.parametrize("command", [
    "$env:PATH",
    "Remove-Item -LiteralPath 'scratch' -Recurse -Force",
    "diskpart",
    "python -c \"print('a'); print('b')\"",
])
def test_native_envelope_allows_unfamiliar_and_destructive_grammar(command):
    action = {"tool": "run_command", "operation": "GENERAL", "command": command}
    admitted, diagnostic = validate_tool_envelope(action, native_broker=True)
    assert admitted, diagnostic


def test_legacy_envelope_keeps_recursive_delete_denial():
    action = {"tool": "run_command", "operation": "GENERAL",
              "command": "Remove-Item -LiteralPath 'scratch' -Recurse -Force"}
    accepted, _ = validate_tool_envelope(action)
    assert not accepted


def test_native_admission_does_not_require_mutation_verification(monkeypatch, tmp_path):
    monkeypatch.setattr(
        turbo_broker_admission, "assess",
        lambda workspace, *, tool, action_id="": _gate(tmp_path, tmp_path),
    )
    context = SimpleNamespace(
        workspace_root=tmp_path, interface_name="remote",
        preview_tool_scope=lambda action: {
            "allowed": True, "scope_diagnostic": {"reason_code": "ALLOWED"},
        },
    )
    for command in ("$env:PATH", "Remove-Item -LiteralPath 'scratch' -Recurse -Force"):
        decision = MinimalDecision(
            kind="tool_call", tool="run_command",
            arguments={"operation": "GENERAL", "command": command},
        )
        action, error = admit_minimal_action(decision, context)
        assert action is not None, error


def test_legacy_admission_still_needs_postcondition(monkeypatch, tmp_path):
    monkeypatch.setattr(
        turbo_broker_admission, "assess",
        lambda workspace, *, tool, action_id="": {"decision": "NORMAL"},
    )
    context = SimpleNamespace(workspace_root=tmp_path, interface_name="remote")
    decision = MinimalDecision(
        kind="tool_call", tool="run_command",
        arguments={"operation": "INSPECT", "command": "$env:PATH"},
    )
    action, error = admit_minimal_action(decision, context)
    assert action is None
    assert error == "mutation_success_criteria_required"


@pytest.mark.parametrize("command", [
    "Remove-Item -LiteralPath 'scratch' -Recurse -Force",
    "diskpart",
    "python -c \"print('a'); print('b')\"",
])
def test_native_confirm_is_os_route_not_command_content_classifier(tmp_path, command):
    root = tmp_path / "Turbo"
    root.mkdir()
    workspace = tmp_path
    loop = SimpleNamespace(workspace=workspace)
    result = evaluate_native_broker_confirmation(
        loop,
        {"tool": "run_command", "command": command,
         "working_directory": str(root)},
        _gate(workspace, root),
    )
    assert result["auto_confirm"] is True
    assert result["authorization_granted"] is False


def test_bounded_process_native_never_rechecks_legacy_blacklist(monkeypatch, tmp_path):
    command = "Remove-Item -LiteralPath 'scratch' -Recurse -Force"
    monkeypatch.setattr(turbo_broker_admission, "assess", lambda *a, **kw: {
        "decision": "BROKER_REQUIRED", "evidence": {}, "reason_code": "TEST",
    })
    monkeypatch.setattr(
        "agent_core.workspace_access.resolve_turbo_authorization_workspace",
        lambda cwd: tmp_path,
    )
    def no_legacy(*a, **kw):
        raise AssertionError("legacy command gate reached Native Broker")
    monkeypatch.setattr("agent_core.command_security.require_command_allowed", no_legacy)
    monkeypatch.setattr(bounded_process.subprocess, "Popen", no_legacy)
    seen = []
    def native(script, **kwargs):
        seen.append(script)
        return {"exit_code": 0, "stdout": "native", "stderr": "",
                "sandbox_evidence": {"sandbox_identity_verified": True}}
    monkeypatch.setattr("agent_core.turbo_os_broker_client.execute_turbo_command", native)
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=tmp_path, security_command=command,
    )
    assert result["exit_code"] == 0
    assert seen == [command]


def test_native_broker_unavailable_never_runs_host(monkeypatch, tmp_path):
    monkeypatch.setattr(turbo_broker_admission, "assess", lambda *a, **kw: {
        "decision": "BLOCKED", "evidence": {},
        "reason_code": "SANDBOX_UNAVAILABLE",
    })
    monkeypatch.setattr(bounded_process.subprocess, "Popen", lambda *a, **kw: (
        (_ for _ in ()).throw(AssertionError("host fallback"))
    ))
    result = bounded_process.run_bounded_process(
        ["powershell", "-NoProfile", "-Command", "Remove-Item -Recurse x"],
        cwd=tmp_path, security_command="Remove-Item -Recurse x",
    )
    assert result["security_rejected"] is True
    assert result["process_started"] is False
    assert "SANDBOX_UNAVAILABLE" in result["error"]


def test_recursive_typed_delete_uses_native_broker(monkeypatch, tmp_path):
    from agent_core import turbo_os_typed_mutations as typed
    root = tmp_path / "Turbo"
    target = root / "scratch"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "data.txt").write_text("data", encoding="utf-8")
    context = SecurityContext.for_workspace(root, task_id="TASK-TEST")
    monkeypatch.setattr(typed, "_checked_target", lambda ctx, value: (target, root))
    commands = []
    def native(script, **kwargs):
        commands.append(script)
        shutil.rmtree(target)  # test double for OS Broker, NOT an attestation
        return {"exit_code": 0, "process_started": True,
                "sandbox_evidence": {"sandbox_identity_verified": True}}
    monkeypatch.setattr(typed, "execute_turbo_command", native)
    result = typed.delete_path_in_broker(str(target), recursive=True,
                                         security_context=context, action_id="MIN-TEST")
    assert "SUCCEEDED" in result
    assert not target.exists()
    assert len(commands) == 1
    assert "$true" in commands[0]


def test_native_cwd_is_not_rejected_for_path_independent_diagnostics(
    monkeypatch, tmp_path,
):
    from agent_core import tools
    root = tmp_path / "Turbo"
    root.mkdir()
    context = SecurityContext.for_workspace(tmp_path)
    monkeypatch.setattr(tools, "_native_run_command_admitted", lambda cwd: True)
    assert tools.resolve_command_working_directory(
        root, context, command="Get-Location", operation="INSPECT",
    ) == root
    monkeypatch.setattr(tools, "_native_run_command_admitted", lambda cwd: False)
    with pytest.raises(PathSecurityError, match="path_independent_command"):
        tools.resolve_command_working_directory(
            root, context, command="Get-Location", operation="INSPECT",
        )


def test_native_literal_read_still_checks_configured_read_scope(
    monkeypatch, tmp_path,
):
    from agent_core import tools
    root = tmp_path / "Turbo"
    root.mkdir()
    context = SecurityContext.for_workspace(tmp_path)
    monkeypatch.setattr(tools, "_native_run_command_admitted", lambda cwd: True)
    command = "Test-Path -LiteralPath '" + str(tmp_path.parent / "private" / "secret.txt") + "'"
    with pytest.raises(PathSecurityError, match="outside_authorized"):
        tools.resolve_command_working_directory(
            root, context, command=command, operation="INSPECT",
        )
