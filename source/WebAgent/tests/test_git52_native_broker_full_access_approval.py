"""Git52: Native-Broker scoped full access removes only secondary consent.

All admission and execution are mocked; these do not claim OS attestation.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_core.command_risk import classify_command
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.turbo_execution_policy import evaluate_native_broker_confirmation
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop


_WRITE = (
    "[System.IO.File]::WriteAllBytes('turbo_exact_verify.txt',"
    "[System.Text.Encoding]::UTF8.GetBytes('TURBO_LIVE_PASS'))"
)


def _gate(workspace, turbo, *, decision="BROKER_REQUIRED", acl="on",
          requested="ON", scope="TURBO_SCOPE_ALLOWED"):
    return {
        "decision": decision,
        "authorization_workspace": str(workspace),
        "state": {
            "acl_mode": acl,
            "turbo_requested": requested,
            "scope_verdict": scope,
            "sandbox_status": "NOT_READY",
            "turbo_root": str(turbo),
            "policy_revision": 52,
        },
    }


@pytest.fixture
def configured(tmp_path):
    workspace = tmp_path / "workspace"
    turbo = workspace / "Turbo"
    turbo.mkdir(parents=True)
    return workspace, turbo, SimpleNamespace(workspace=workspace)


@pytest.mark.parametrize("command", [
    _WRITE,
    "python arbitrary.py",
    "Set-Content -LiteralPath 'new.txt' -Value 'ok'",
    "Remove-Item -LiteralPath 'created-by-task.txt'",
])
def test_native_full_access_second_gate_only(configured, command):
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "operation": "GENERAL",
              "command": command, "working_directory": str(turbo)}
    assert evaluate_native_broker_confirmation(
        loop, action, _gate(workspace, turbo)
    )["auto_confirm"] is True


def test_writeallbytes_stays_unknown_outside_turbo(configured):
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "operation": "GENERAL",
              "command": _WRITE, "working_directory": str(turbo)}
    assert classify_command(action) == "UNKNOWN"
    for gate in (
        _gate(workspace, turbo, decision="NORMAL"),
        _gate(workspace, turbo, acl="off"),
        _gate(workspace, turbo, requested="OFF"),
        _gate(workspace, turbo, scope="TURBO_SCOPE_DENIED"),
        _gate(workspace, turbo, decision="BLOCKED"),
    ):
        assert evaluate_native_broker_confirmation(loop, action, gate)["auto_confirm"] is False


def test_parent_cwd_remapped_to_scoped_root(configured):
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "command": _WRITE,
              "working_directory": str(workspace)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is True
    assert decision["turbo_root"] == str(turbo)
    assert decision["authorization_granted"] is False


def test_outside_cwd_cannot_skip_approval(configured):
    workspace, turbo, loop = configured
    other = workspace / "outside"
    other.mkdir()
    action = {"tool": "run_command", "command": _WRITE,
              "working_directory": str(other)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is False
    assert decision["reason_code"] == "TURBO_NATIVE_BROKER_CWD_OUTSIDE_ROOT"


@pytest.mark.parametrize("command", [
    "diskpart",
    "icacls 'C:\\Windows' /grant Everyone:F",
    "Remove-Item -Recurse 'C:\\'",
])
def test_native_command_grammar_is_not_software_permission_boundary(configured, command):
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "command": command,
              "working_directory": str(turbo)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is True
    assert decision["authorization_granted"] is False


def test_native_rejects_empty_command_before_broker(configured):
    workspace, turbo, loop = configured
    result = evaluate_native_broker_confirmation(
        loop, {"tool": "run_command", "command": "",
               "working_directory": str(turbo)}, _gate(workspace, turbo),
    )
    assert result["auto_confirm"] is False
    assert result["reason_code"] == "TURBO_NATIVE_BROKER_COMMAND_TOO_LARGE"


def test_missing_or_reparse_cwd_not_auto_confirmed(configured, tmp_path):
    workspace, turbo, loop = configured
    missing = turbo / "does-not-exist"
    action = {"tool": "run_command", "command": _WRITE,
              "working_directory": str(missing)}
    result = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert result["auto_confirm"] is False
    target = turbo / "actual"
    target.mkdir()
    link = turbo / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        return
    action["working_directory"] = str(link)
    assert evaluate_native_broker_confirmation(
        loop, action, _gate(workspace, turbo)
    )["auto_confirm"] is False


def test_executor_native_unknown_skips_approval_but_calls_tool(tmp_path, monkeypatch):
    turbo = tmp_path / "turbo"
    turbo.mkdir()
    request = "Use Native Broker to write one test file inside the authorized turbo root"
    action = {
        "type": "tool_call", "name": "run_command",
        "arguments": {
            "command": _WRITE,
            "operation": "GENERAL",
            "working_directory": str(turbo),
            "success_criteria": "The test file is written by native broker",
            "verify": [{"action": "file_exists",
                        "path": str(turbo / "turbo_exact_verify.txt"),
                        "expect": True}],
        },
    }
    replies = [json.dumps(action), '{"type":"blocked","reason":"test ends"}']
    loop, state = _loop(tmp_path, monkeypatch, replies, request=request)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda workspace, proposed: _gate(tmp_path, turbo)
        if proposed and proposed.get("tool") == "run_command"
        else {"decision": "NOT_APPLICABLE", "state": None, "evidence": {}},
    )
    def no_human(*args, **kwargs):
        raise AssertionError("UNKNOWN approval must not be requested in scoped OS Turbo")
    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval", no_human
    )
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    decisions = [e for e in board["events"] if e["kind"] == "TURBO_DECISION"]
    assert len(decisions) == 1
    assert decisions[0]["risk_class"] == "UNKNOWN"
    assert decisions[0]["auto_confirm"] is True
    assert decisions[0]["reason_code"] == "TURBO_NATIVE_BROKER_SCOPED_FULL_ACCESS"
    assert len([e for e in board["events"] if e["kind"] == "TURBO_AUTO_ADMITTED"]) == 1
    assert not [e for e in board["events"] if e["kind"] == "HUMAN_APPROVAL_REQUESTED"]
    assert len(loop.executed) == 1
    assert loop.executed[0]["tool"] == "run_command"


def test_executor_native_outside_cwd_blocks_without_prompt(tmp_path, monkeypatch):
    turbo = tmp_path / "turbo"
    outside = tmp_path / "elsewhere"
    turbo.mkdir()
    outside.mkdir()
    request = "Attempt command outside native root"
    action = {
        "type": "tool_call", "name": "run_command",
        "arguments": {
            "command": _WRITE,
            "operation": "GENERAL",
            "working_directory": str(outside),
            "success_criteria": "test",
            "verify": [{"action": "file_exists",
                        "path": str(outside / "turbo_exact_verify.txt"),
                        "expect": True}],
        },
    }
    loop, _ = _loop(tmp_path, monkeypatch,
                    [json.dumps(action)], request=request)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda workspace, proposed: _gate(tmp_path, turbo)
        if proposed and proposed.get("tool") == "run_command"
        else {"decision": "NOT_APPLICABLE", "state": None, "evidence": {}},
    )
    def no_human(*args, **kwargs):
        raise AssertionError("Out-of-scope native command must block, not seek consent")
    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval", no_human
    )
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert not loop.executed
    assert [e for e in board["events"] if e["kind"] == "TURBO_NATIVE_CONFIRMATION_BLOCKED"]
    assert not [e for e in board["events"] if e["kind"] == "HUMAN_APPROVAL_REQUESTED"]


@pytest.mark.parametrize("revision", [None, "", "52", 0, -1, True])
def test_native_consent_never_auto_approves_invalid_policy_revision(configured, revision):
    workspace, turbo, loop = configured
    gate = _gate(workspace, turbo)
    gate["state"]["policy_revision"] = revision
    result = evaluate_native_broker_confirmation(
        loop, {"tool": "run_command", "command": _WRITE,
               "working_directory": str(turbo)}, gate,
    )
    assert result["auto_confirm"] is False
    assert result["reason_code"] == "TURBO_NATIVE_BROKER_POLICY_REVISION_UNVERIFIED"



def test_native_full_access_preserves_legacy_classifier_separately(configured):
    from agent_core.command_security import inspect_command
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "command": "diskpart",
              "working_directory": str(turbo)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is True
    assert decision["authorization_granted"] is False
    # ACL OFF and Turbo OFF still use the unchanged command blacklist.
    assert inspect_command("diskpart").code == "diskpart_forbidden"
    assert "command_security_code" not in decision


def test_native_precheck_rejects_outside_working_directory(configured, tmp_path):
    workspace, turbo, loop = configured
    other = tmp_path / "outside"
    other.mkdir()
    decision = evaluate_native_broker_confirmation(
        loop, {"tool": "run_command", "command": "Remove-Item -Recurse x",
               "working_directory": str(other)}, _gate(workspace, turbo),
    )
    assert decision["auto_confirm"] is False
    assert decision["reason_code"] == "TURBO_NATIVE_BROKER_CWD_OUTSIDE_ROOT"


def test_native_precheck_does_not_require_content_classifier(configured, monkeypatch):
    workspace, turbo, loop = configured
    def no_classifier(*args, **kwargs):
        raise AssertionError("legacy content classifier invoked")
    monkeypatch.setattr(
        "agent_core.turbo_execution_policy.inspect_native_command", no_classifier,
    )
    decision = evaluate_native_broker_confirmation(
        loop, {"tool": "run_command", "command": "python ../outside/verify.py",
               "working_directory": str(turbo)}, _gate(workspace, turbo),
    )
    assert decision["auto_confirm"] is True
    assert decision["authorization_granted"] is False


def test_native_precheck_replan_not_eligible_without_content_denial(configured):
    from agent_core.turbo_execution_policy import native_precheck_replan_eligible
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "command": "diskpart",
              "working_directory": str(turbo)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is True
    assert not native_precheck_replan_eligible(decision)
    assert not native_precheck_replan_eligible({
        "reason_code": "TURBO_NATIVE_BROKER_CWD_OUTSIDE_ROOT",
        "auto_confirm": False,
    })
