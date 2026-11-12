"""Git39: unknown-command human approval and history-safe Finish comparator.

No Task/Goal workflow is inferred; all decisions pass the same Executor Loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agent_core.action_dependencies import command_is_proven_observation
from agent_core.command_risk import classify_command
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import encode_result
from agent_core.unknown_command_approval import (
    APPROVAL_TIMEOUT_SECONDS, request_unknown_command_approval,
)
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop


def _tool(command, operation, *, criteria=True):
    args = {"command": command, "operation": operation}
    if criteria:
        args["success_criteria"] = "The command exited successfully"
        args["verify"] = [{"action": "file_exists", "path": r"E:\safe\report.txt", "expect": True}]
    return json.dumps({"type": "tool_call", "name": "run_command", "arguments": args})


def _denials(board):
    return [e for e in board["events"] if e.get("kind") == "ADMISSION_REJECTED"]


def test_command_risk_generic_non_privileged_categories():
    sha = r"Get-FileHash -LiteralPath 'E:\safe\data.csv' -Algorithm SHA256"
    action = {"tool": "run_command", "operation": "INSPECT", "command": sha}
    assert command_is_proven_observation(action)
    assert classify_command(action) == "PROVEN_READ"
    assert classify_command({**action, "command": sha + " | Set-Content 'E:\\out'"}) != "PROVEN_READ"
    assert classify_command({**action, "command": "git clone https://github.com/a/b.git", "operation": "GIT_MUTATE"}) == "MUTATING"
    assert classify_command({**action, "command": "python arbitrary.py"}) == "UNKNOWN"
    assert classify_command({**action, "command": "diskpart"}) == "FORBIDDEN"
    assert classify_command({**action, "command": "git clone https://github.com/a/b.git; python arbitrary.py"}) == "UNKNOWN"


def test_unknown_approval_timeout_is_600_seconds_and_never_grants(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    tools = loop.tools
    tools.security_approval_chat_id = "12345"
    tools.security_approval_ledger_root = tmp_path
    sent = []
    tools._security_approval_notifier = lambda rec, manifest: sent.append((rec, manifest))
    tools.notify_security_approval = lambda rec, manifest: tools._security_approval_notifier(rec, manifest)
    observed = {"clock": 100.0, "consumed": False, "request": None}

    class TestLedger:
        def __init__(self, root): pass
        def request(self, binding, *, ttl_sec):
            observed["request"] = (binding, ttl_sec)
            return {"approval_id": "APR-" + "A" * 20, "expires_at": 100.0 + ttl_sec, "state": "PENDING"}
        def get(self, approval_id):
            return {"state": "PENDING"}
        def consume(self, approval_id, binding):
            observed["consumed"] = True
            raise AssertionError("unapproved grant must not be consumed")

    monkeypatch.setattr("agent_core.unknown_command_approval.SecurityApprovalLedger", TestLedger)
    cmd = {
        "tool": "run_command", "action_id": "MIN-ABC",
        "command": "python arbitrary.py", "operation": "GENERAL",
        "working_directory": str(tmp_path),
    }
    result = request_unknown_command_approval(
        loop, cmd, clock=lambda: observed["clock"],
        sleep=lambda seconds: observed.__setitem__("clock", observed["clock"] + seconds),
    )
    assert APPROVAL_TIMEOUT_SECONDS == 600
    assert result["state"] == "TIMEOUT"
    assert observed["clock"] == 700
    assert observed["request"][1] == 600
    assert observed["request"][0]["approval_kind"] == "UNKNOWN_COMMAND"
    assert observed["consumed"] is False
    assert len(sent) == 1
    assert sent[0][1]["command"] == "python arbitrary.py"
    assert sent[0][1]["timeout_sec"] == 600


def test_unknown_command_approval_is_one_time_and_bound_to_exact_action(
    tmp_path, monkeypatch,
):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    tools = loop.tools
    tools.security_approval_chat_id = "12345"
    tools.security_approval_ledger_root = tmp_path
    observed = {"consumptions": [], "bindings": []}

    class TestLedger:
        def __init__(self, root): pass
        def request(self, binding, *, ttl_sec):
            observed["bindings"].append(dict(binding))
            return {"approval_id": "APR-" + "B" * 20, "expires_at": 700.0, "state": "PENDING"}
        def get(self, approval_id):
            return {"state": "APPROVED"}
        def consume(self, approval_id, binding):
            assert binding == observed["bindings"][-1]
            observed["consumptions"].append((approval_id, binding))

    monkeypatch.setattr("agent_core.unknown_command_approval.SecurityApprovalLedger", TestLedger)
    tools._security_approval_notifier = lambda record, manifest: None
    tools.notify_security_approval = lambda record, manifest: tools._security_approval_notifier(record, manifest)
    cmd = {"tool": "run_command", "action_id": "MIN-ABC",
           "command": "python arbitrary.py", "operation": "GENERAL"}
    result = request_unknown_command_approval(loop, cmd, clock=lambda: 100.0)
    assert result["state"] == "APPROVED"
    assert len(observed["consumptions"]) == 1
    identity = observed["bindings"][0]
    assert identity["action_id"] == cmd["action_id"]
    assert identity["task_id"] == loop.task_id
    assert identity["request_id"] == loop.run_id
    assert identity["approval_kind"] == "UNKNOWN_COMMAND"
    assert result["command_sha256"] != ""


def test_executor_unknown_command_approval_then_dispatch(tmp_path, monkeypatch):
    request = "確認報表資料並回報工具執行證據"
    calls = []
    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval",
        lambda loop, action: calls.append(action) or {
            "state": "APPROVED", "reason": "approved_once_consumed",
            "approval_id": "APR-A", "command_sha256": "abc",
        },
    )
    replies = [
        _tool("python arbitrary.py", "GENERAL"),
        '{"type":"blocked","reason":"已觀測命令，但尚有其他工作待辦"}',
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert len(calls) == 1
    assert len(loop.executed) == 1
    assert [e["kind"] for e in board["events"] if e["kind"].startswith("HUMAN_APPROVAL")] == [
        "HUMAN_APPROVAL_REQUESTED", "HUMAN_APPROVAL_DECIDED"
    ]
    # Git52 adds TURBO_DECISION before human-approval events. Their ordinal
    # position is no longer fixed; the kind and decision sequence are.
    decided = [e for e in board["events"] if e["kind"] == "HUMAN_APPROVAL_DECIDED"]
    assert len(decided) == 1
    assert decided[0]["state"] == "APPROVED"
    assert len([e for e in board["events"] if e["kind"] == "TOOL_RESULT"]) == 1
    assert not _denials(board)


def test_timeout_then_second_unauthorized_command_pauses_for_continuation(
    tmp_path, monkeypatch,
):
    request = "請取得資料，若不能執行就回報拒絕"
    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval",
        lambda loop, action: {
            "state": "TIMEOUT", "reason": "human_approval_timeout_600s",
            "approval_id": "APR-EXPIRED", "command_sha256": "abc",
        },
    )
    replies = [
        _tool("python arbitrary.py", "GENERAL"),
        _tool("python different.py", "GENERAL"),
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    output = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert board["executor_state"] == "BLOCKED"
    assert loop.executed == []
    assert "FAIL：" in output
    assert "接續任務" in output
    assert len([e for e in board["events"] if e["kind"] == "HUMAN_APPROVAL_REQUESTED"]) == 1
    assert len([e for e in board["events"] if e["kind"] == "TOOL_RESULT"]) == 0


def test_timeout_then_known_safe_alternative_runs(tmp_path, monkeypatch):
    request = "確認目前 Git 狀態"
    monkeypatch.setattr(
        "agent_core.unknown_command_approval.request_unknown_command_approval",
        lambda loop, action: {
            "state": "TIMEOUT", "reason": "human_approval_timeout_600s",
            "approval_id": "APR-EXPIRED", "command_sha256": "abc",
        },
    )
    replies = [
        _tool("python arbitrary.py", "GENERAL"),
        _tool("git status --short", "GIT_INSPECT", criteria=False),
        '{"type":"blocked","reason":"替代命令已完成，尚未滿足其他驗收"}',
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    run_minimal_core(loop, request)
    assert len(loop.executed) == 1
    assert loop.executed[0]["command"] == "git status --short"


def test_prior_executed_rejection_cannot_veto_later_proven_success(
    tmp_path, monkeypatch,
):
    request = "確認 Git 狀態成功執行，exit_code=0"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    executed = []
    def execute(action):
        executed.append(action)
        if len(executed) == 1:
            return encode_result(
                "REJECTED", "COMMAND_SCOPE_REJECTED", tool="run_command",
                executed=False, data={"exit_code": None},
            )
        return encode_result(
            "SUCCEEDED", "COMMAND_OK", tool="run_command",
            executed=True, data={"exit_code": 0, "observation_confirmed": True},
        )
    loop._execute_action = execute
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) <= 2:
            return _tool("git status --short", "GIT_INSPECT", criteria=False)
        from agent_core.executor_run_blackboard import read_executor_run
        board = read_executor_run(tmp_path, task_id=loop.task_id)
        actual = [e for e in board["events"] if e["kind"] == "TOOL_RESULT"]
        assert len(actual) == 2
        ref1 = actual[0]["runtimeEvidence"]["evidence_ref"]
        ref2 = actual[1]["runtimeEvidence"]["evidence_ref"]
        return json.dumps({
            "type": "finish",
            "summary": "Git 已取得 exit_code 0 的成功觀測",
            "expected_runtime": [
                {"evidence_ref": ref1, "fields": {"status": "REJECTED"}},
                {"evidence_ref": ref2, "fields": {
                    "status": "SUCCEEDED", "data.exit_code": 0,
                }},
            ],
            "goal_checks": [{
                "criterion": "Git 退出碼為 0",
                "evidence_ref": ref2,
                "field": "data.exit_code", "operator": "eq", "expected": 0,
            }],
        }, ensure_ascii=False)
    loop.planner = planner
    answer = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert len(executed) == 2
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    assert loop.terminal_outcome == "SUCCESS"
    assert "已取得" in answer


def test_only_rejections_cannot_claim_pass(tmp_path, monkeypatch):
    request = "完成實際 Git 狀態讀取"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    loop._execute_action = lambda action: encode_result(
        "REJECTED", "COMMAND_SCOPE_REJECTED", tool="run_command",
        executed=False,
    )
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return _tool("git status --short", "GIT_INSPECT", criteria=False)
        board = read_executor_run(tmp_path, task_id=loop.task_id)
        ref = next(e for e in board["events"] if e["kind"] == "TOOL_RESULT")["runtimeEvidence"]["evidence_ref"]
        return json.dumps({
            "type": "finish", "summary": "這不是真的完成",
            "expected_runtime": [{"evidence_ref": ref, "fields": {"status": "REJECTED"}}],
            "goal_checks": [{"criterion": "聲稱完成", "evidence_ref": ref,
                             "field": "status", "operator": "eq", "expected": "REJECTED"}],
        }, ensure_ascii=False)
    loop.planner = planner
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert board["terminal"]["finish_comparison"]["verdict"] == "FAIL"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "NO_EXECUTED_GOAL_EVIDENCE"


def test_strict_hash_literal_read_from_bound_cwd_stays_request_scoped(tmp_path):
    from types import SimpleNamespace
    from agent_core.security_context import SecurityContext
    from agent_core.tools import resolve_command_working_directory
    allowed = tmp_path / "case_a"
    allowed.mkdir()
    data = allowed / "data.csv"
    data.write_bytes(b"a,b\n1,2\n")
    outside = tmp_path / "other.csv"
    outside.write_bytes(b"secret\n")
    context = SecurityContext.for_workspace(tmp_path)
    agent = SimpleNamespace(_authorized_local_paths=[str(allowed)])
    command = f"Get-FileHash -LiteralPath '{data}' -Algorithm SHA256"
    got = resolve_command_working_directory(
        None, context, agent, command=command, operation="INSPECT"
    )
    assert got == tmp_path.resolve()
    with pytest.raises(Exception):
        resolve_command_working_directory(
            None, context, agent,
            command=f"Get-FileHash -LiteralPath '{outside}' -Algorithm SHA256",
            operation="INSPECT",
        )
