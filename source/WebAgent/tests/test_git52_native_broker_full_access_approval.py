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
    "",
])
def test_hard_security_denials_not_exempt(configured, command):
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "command": command,
              "working_directory": str(turbo)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is False
    assert decision["reason_code"] == "TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED"


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



def test_native_hard_deny_preserves_safe_classifier(configured):
    import hashlib
    workspace, turbo, loop = configured
    action = {"tool": "run_command", "command": "diskpart",
              "working_directory": str(turbo)}
    decision = evaluate_native_broker_confirmation(loop, action, _gate(workspace, turbo))
    assert decision["auto_confirm"] is False
    assert decision["reason_code"] == "TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED"
    assert decision["command_security_code"] == "diskpart_forbidden"
    assert decision["command_security_approval_eligible"] is False
    assert decision["command_security_detail_sha256"] == hashlib.sha256(
        b"use a typed, runtime-authorized operation"
    ).hexdigest()
    assert "command" not in decision
    assert "detail" not in decision
    assert decision["authorization_granted"] is False


def test_executor_native_precheck_records_rejection_cause(tmp_path, monkeypatch):
    turbo = tmp_path / "turbo"
    turbo.mkdir()
    request = "Native broker hard-denied command precheck"
    action = {
        "type": "tool_call", "name": "run_command",
        "arguments": {
            "command": "diskpart",
            "operation": "GENERAL",
            "working_directory": str(turbo),
            "success_criteria": "will be denied",
            "verify": [{"action": "file_exists",
                        "path": str(turbo / "never.txt"),
                        "expect": True}],
        },
    }
    loop, _ = _loop(tmp_path, monkeypatch, [json.dumps(action)], request=request)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda workspace, proposed: _gate(tmp_path, turbo)
        if proposed and proposed.get("tool") == "run_command"
        else {"decision": "NOT_APPLICABLE", "state": None, "evidence": {}},
    )
    def no_human(*args, **kwargs):
        raise AssertionError("hard security denial must never request human consent")
    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval", no_human
    )
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert not loop.executed
    decisions = [e for e in board["events"] if e["kind"] == "TURBO_DECISION"]
    blocks = [e for e in board["events"]
              if e["kind"] == "TURBO_NATIVE_CONFIRMATION_BLOCKED"]
    assert len(decisions) == len(blocks) == 1
    assert decisions[0]["decision"] == "NATIVE_PRECHECK_BLOCKED"
    assert decisions[0]["command_security_code"] == "diskpart_forbidden"
    assert decisions[0]["reason_code"] == "TURBO_NATIVE_BROKER_COMMAND_SECURITY_DENIED"
    assert blocks[0]["command_security_code"] == "diskpart_forbidden"
    assert blocks[0]["command_security_approval_eligible"] is False
    assert len(blocks[0]["command_sha256"]) == 64
    assert blocks[0]["broker_decision"] == "BROKER_REQUIRED"
    assert blocks[0]["executed"] is False
    assert blocks[0]["authorization_granted"] is False
    assert not [e for e in board["events"] if e["kind"] == "HUMAN_APPROVAL_REQUESTED"]
