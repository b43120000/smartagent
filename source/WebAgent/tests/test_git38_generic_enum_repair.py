"""Git38: generic Enum Contract Repair, event separation and goal proof."""
from __future__ import annotations

import json

import pytest

from agent_core.command_operation import COMMAND_OPERATIONS
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_contract_metadata import enum_constraints
from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
from WebAgent.minimal_admission import admit_minimal_action
from WebAgent.minimal_core_loop import run_minimal_core, tool_catalog
from WebAgent.minimal_decision import parse_minimal_decision
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET


def command_call(command: str, operation: str):
    return json.dumps({
        "type": "tool_call", "name": "run_command",
        "arguments": {"command": command, "operation": operation},
    })


def _denials(board):
    return [e for e in board["events"] if e["kind"] == "ADMISSION_REJECTED"]


def test_catalog_enum_is_dynamic_and_identical_to_authoritative_runtime(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    run = next(r for r in tool_catalog(loop.tools) if r["name"] == "run_command")
    assert run["allowed_values"]["operation"] == sorted(COMMAND_OPERATIONS)
    assert enum_constraints("run_command")["operation"] == sorted(COMMAND_OPERATIONS)
    assert "GIT_CLONE" not in run["allowed_values"]["operation"]
    assert "GIT_MUTATE" in run["allowed_values"]["operation"]
    assert run["required_types"]["operation"] == "str"
    assert enum_constraints("list_directory") == {}


@pytest.mark.parametrize("value,shown", [
    ("GIT_CLONE", "GIT_CLONE"),
    ("GIT_FETCH", "GIT_FETCH"),
    ("GIT_CLONE; secret=$token", "[REDACTED_ENUM]"),
])
def test_enum_rejection_diagnostic_is_typed_and_not_raw_command(
    tmp_path, monkeypatch, value, shown,
):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    cmd = "git clone https://github.com/b43120000/test.git SECRET_SHOULD_NEVER_LEAK"
    decision = parse_minimal_decision(command_call(cmd, value))
    admitted, reason = admit_minimal_action(decision, loop.tools)
    assert admitted is None
    assert reason == "run_command_operation_invalid"
    diag = loop.tools._last_minimal_admission_diagnostic
    assert diag["repair_class"] == "MODEL_SCHEMA_REPAIR"
    assert diag["executed"] is False
    issue = diag["field_issues"][0]
    assert issue["field"] == "operation"
    assert issue["reason"] == "invalid_enum"
    assert issue["actual_value"] == shown
    assert issue["allowed_values"] == sorted(COMMAND_OPERATIONS)
    assert "SECRET_SHOULD_NEVER_LEAK" not in json.dumps(diag)
    assert "https://github.com" not in json.dumps(diag)


def test_semantic_operation_mismatch_does_not_expose_command(
    tmp_path, monkeypatch,
):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    decision = parse_minimal_decision(command_call(
        "python --version", "GIT_MUTATE",
    ))
    admitted, reason = admit_minimal_action(decision, loop.tools)
    assert admitted is None
    assert reason == "run_command_operation_mismatch"
    issue = loop.tools._last_minimal_admission_diagnostic["field_issues"][0]
    assert issue["field"] == "operation"
    assert issue["reason"] == "operation_mismatch"
    assert issue["constraint_code"] == "GIT_MUTATE_requires_git_command"
    assert "python --version" not in json.dumps(issue)


def test_one_loop_enum_rejection_then_corrected_tool_and_verified_finish(
    tmp_path, monkeypatch,
):
    request = "彙整執行紀錄並確認退出碼符合目前設定"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return command_call("git status --short", "GIT_STATUS")
        if len(loop.prompts) == 2:
            assert '"field":"operation"' in prompt
            assert '"actual_value":"GIT_STATUS"' in prompt
            assert "GIT_INSPECT" in prompt
            assert 'ToolResult":' not in prompt.split(
                "Recent actual PokeClaw ToolResults:\n", 1
            )[1].split("\n", 1)[0]
            return command_call("git status --short", "GIT_INSPECT")
        evidence = json.loads(prompt.split(
            "Recent actual PokeClaw ToolResults:\n", 1
        )[1].split("\n", 1)[0])
        ref = evidence[-1]["evidence_ref"]
        return json.dumps({
            "type": "finish", "summary": "已觀測實際 Git 命令退出碼為 0",
            "expected_runtime": [{
                "evidence_ref": ref,
                "fields": {"status": "SUCCEEDED", "data.exit_code": 0},
            }],
            "goal_checks": [{
                "criterion": "實際退出碼符合條件",
                "evidence_ref": ref,
                "field": "data.exit_code", "operator": "eq", "expected": 0,
            }],
        }, ensure_ascii=False)
    loop.planner = planner
    answer = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    denied = _denials(board)
    actual = [x for x in board["events"] if x["kind"] == "TOOL_RESULT"]
    assert len(denied) == 1
    assert len(actual) == 1
    assert denied[0]["runtimeEvidence"]["executed"] is False
    assert denied[0]["runtimeEvidence"]["evidence_ref"] == ""
    assert denied[0]["admission_diagnostic"]["field_issues"][0]["actual_value"] == "GIT_STATUS"
    assert "toolResult" not in denied[0]
    assert actual[0]["toolResult"]["isSuccess"] is True
    assert actual[0]["runtimeEvidence"]["evidence_ref"].startswith("MIN-EV-")
    assert len(loop.executed) == 1
    assert loop.executed[0]["operation"] == "GIT_INSPECT"
    assert board["terminal"]["admission_rejection_count"] == 1
    assert board["terminal"]["tool_evidence_refs"] == [
        actual[0]["runtimeEvidence"]["evidence_ref"]
    ]
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    assert loop.terminal_outcome == "SUCCESS"
    assert terminal_verdict_from_loop(loop).outcome == "SUCCESS"
    assert "已觀測實際" in answer


def test_distinct_commands_do_not_share_enum_failure_budget(
    tmp_path, monkeypatch,
):
    request = "請檢查不同來源狀態 " + TARGET
    commands = [
        "git status --short",
        "git rev-parse HEAD",
        "git branch --show-current",
    ]
    replies = [
        command_call(cmd, "GIT_STATUS") for cmd in commands
    ] + ['{"type":"blocked","reason":"示範：來源尚未取得授權"}']
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = _denials(board)
    assert len(rejected) == 3
    assert [r["admission_repair_attempt"] for r in rejected] == [1, 1, 1]
    assert [r["admission_rejection_streak"] for r in rejected] == [1, 2, 3]
    assert board["terminal"]["kind"] == "BLOCKED"
    assert "三次未解" not in loop.prompts[-1]
    assert len(loop.executed) == 0


def test_identical_command_and_invalid_enum_stops_at_three(
    tmp_path, monkeypatch,
):
    request = "請使用 Git 檢查來源 " + TARGET
    loop, _ = _loop(
        tmp_path, monkeypatch,
        [command_call("git status --short", "GIT_STATUS")] * 5,
        request=request,
    )
    result = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = _denials(board)
    assert len(rejected) == 3
    assert [r["admission_repair_attempt"] for r in rejected] == [1, 2, 3]
    assert board["terminal"]["kind"] == "BLOCKED"
    assert "三次未解" in result
    assert loop.executed == []


def test_global_consecutive_repair_limit_remains_bounded(
    tmp_path, monkeypatch,
):
    request = "請取得來源並檢查相關版本 " + TARGET
    loop, _ = _loop(
        tmp_path, monkeypatch,
        [command_call(f"git status --short -v{i}", "GIT_STATUS") for i in range(14)],
        request=request,
    )
    loop.max_turns = 16
    result = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = _denials(board)
    assert len(rejected) == 10
    assert [r["admission_repair_attempt"] for r in rejected] == [1] * 10
    assert board["terminal"]["admission_rejection_count"] == 10
    assert board["terminal"]["kind"] == "BLOCKED"
    assert "連續 Admission 拒絕達上限" in result
    assert loop.executed == []


def test_rejection_without_real_tool_result_cannot_finish_text_or_tool(
    tmp_path, monkeypatch,
):
    request = "執行 git status --short 並取得 Git 實際結果"
    replies = [
        command_call("git status --short", "GIT_CLONE"),
        '{"type":"finish","summary":"執行成功","expected_runtime":[]}',
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert len(_denials(board)) == 1
    assert not any(e["kind"] == "TOOL_RESULT" for e in board["events"])
    assert board["terminal"]["finish_comparison"]["verdict"] == "FAIL"
    assert board["terminal"]["finish_comparison"]["reason_code"] in {
        "NO_TOOL_FINISH_WITH_PENDING_ACTION", "ADMISSION_REJECTED_NOT_GOAL_PROOF",
    }
    assert loop.terminal_outcome == "FAILED"
