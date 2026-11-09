"""Git37: typed Action Admission diagnostics, bounded model repair, no scope bypass."""
from __future__ import annotations

import json

import pytest

from agent_core.action_dependencies import command_is_proven_observation
from agent_core.executor_run_blackboard import read_executor_run
from WebAgent.minimal_admission import admit_minimal_action
from WebAgent.minimal_core_loop import run_minimal_core, tool_catalog
from WebAgent.minimal_decision import parse_minimal_decision
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET


def proposed(arguments):
    return parse_minimal_decision(json.dumps({
        "type": "tool_call", "name": "run_command", "arguments": arguments,
    }))


@pytest.mark.parametrize("error_args,field,expected_reason,expected_type,actual_type", [
    ({"command": "git clone https://github.com/b43120000/test.git",
      "operation": "GIT_MUTATE", "success_criteria": "clone completed",
      "verify": "check that files exist"},
     "verify", "wrong_field_type", "list | dict", "str"),
    ({"command": "git clone https://github.com/b43120000/test.git",
      "operation": "GIT_MUTATE", "success_criteria": {"passed": True},
      "verify": [{"action": "file_exists", "path": TARGET, "expect": True}]},
     "success_criteria", "wrong_field_type", "str", "dict"),
    ({"operation": "GIT_MUTATE", "verify": [],
      "success_criteria": "clone completed"},
     "command", "missing_required_field", "str", "MISSING"),
])
def test_field_type_admission_preserves_typed_diagnostic(
    tmp_path, monkeypatch, error_args, field, expected_reason,
    expected_type, actual_type,
):
    loop, _ = _loop(tmp_path, monkeypatch, [], request="使用 Git 抓取 " + TARGET)
    action, reason = admit_minimal_action(proposed(error_args), loop.tools)
    assert action is None and reason == expected_reason
    diag = loop.tools._last_minimal_admission_diagnostic
    assert diag["stage"] == "SCHEMA_VALIDATION"
    assert diag["repair_class"] == "MODEL_SCHEMA_REPAIR"
    assert diag["reason_code"] == expected_reason
    assert diag["executed"] is False and diag["authorization_granted"] is False
    issue = next(row for row in diag["field_issues"] if row["field"] == field)
    assert issue["expected_type"] == expected_type
    assert issue["actual_type"] == actual_type
    serialized = json.dumps(diag)
    assert "git clone" not in serialized and "https://github.com" not in serialized


def test_dynamic_catalog_exposes_types_and_verify_object_contract(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    catalog = tool_catalog(loop.tools)
    cmd = next(item for item in catalog if item["name"] == "run_command")
    assert cmd["required_types"]["command"] == "str"
    assert cmd["required_types"]["operation"] == "str"
    assert cmd["optional_types"]["verify"] == "list | dict"
    assert cmd["optional_types"]["success_criteria"] == "str"
    assert cmd["verify_contract"]["sample"]["action"] == "file_exists"


def test_git_repair_feedback_then_fresh_admission_without_clone_dispatch(
    tmp_path, monkeypatch,
):
    request = "請用 Git 檢查來源並回報實際結果 " + TARGET
    bad = {
        "command": "git clone https://github.com/b43120000/test.git",
        "operation": "GIT_MUTATE",
        "success_criteria": "clone completed",
        "verify": "untyped verification",
    }
    replies = [
        json.dumps({"type": "tool_call", "name": "run_command", "arguments": bad}),
        json.dumps({"type": "tool_call", "name": "run_command", "arguments": {
            "command": "git status --short", "operation": "GIT_INSPECT",
        }}),
        json.dumps({"type": "blocked", "reason": "檢視已完成，沒有取得部署授權"}),
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = [e for e in board["events"]
                if e.get("runtimeEvidence", {}).get("status") == "REJECTED"]
    assert len(rejected) == 1
    diagnostic = rejected[0]["admission_diagnostic"]
    assert diagnostic["field_issues"][0]["field"] == "verify"
    assert diagnostic["field_issues"][0]["actual_type"] == "str"
    assert rejected[0]["admission_repair_attempt"] == 1
    assert rejected[0]["runtimeEvidence"]["executed"] is False
    assert "untyped verification" not in json.dumps(board)
    assert "git clone" not in json.dumps(board)
    assert len(loop.executed) == 1
    assert loop.executed[0]["command"] == "git status --short"
    assert len(loop.action_result_ledger) == 1
    assert "field_issues" in loop.prompts[1]
    assert '"field":"verify"' in loop.prompts[1]
    assert board["terminal"]["admission_rejection_count"] == 1


def test_repeated_same_schema_issue_stops_even_if_command_changes(
    tmp_path, monkeypatch,
):
    request = "請使用 git 取得指定來源到 " + TARGET
    replies = []
    for i in range(5):
        replies.append(json.dumps({"type": "tool_call", "name": "run_command",
                                   "arguments": {
                                       "command": f"git clone https://github.com/b43120000/test.git {i}",
                                       "operation": "GIT_MUTATE",
                                       "success_criteria": "clone completed",
                                       "verify": "bad string",
                                   }}))
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    result = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = [e for e in board["events"]
                if e.get("runtimeEvidence", {}).get("status") == "REJECTED"]
    assert len(rejected) == 3
    assert [e["admission_repair_attempt"] for e in rejected] == [1, 2, 3]
    assert board["terminal"]["admission_rejection_count"] == 3
    assert loop.executed == []
    assert "三次未解" in result
    assert board["executor_state"] == "BLOCKED"


def test_read_only_label_is_not_a_permission_or_git_command_rewrite(
    tmp_path, monkeypatch,
):
    exact = "git ls-remote https://github.com/b43120000/test.git HEAD"
    other = "git ls-remote https://github.com/b43120000/test.git"
    assert command_is_proven_observation({
        "tool": "run_command", "operation": "GIT_INSPECT", "command": exact,
    })
    assert not command_is_proven_observation({
        "tool": "run_command", "operation": "GIT_INSPECT", "command": other,
    })
    loop, _ = _loop(tmp_path, monkeypatch, [])
    action, reason = admit_minimal_action(proposed({
        "command": other, "operation": "GIT_INSPECT",
    }), loop.tools)
    assert action is None and reason == "command_not_proven_read_only"
    assert loop.tools._last_minimal_admission_diagnostic["stage"] == "COMMAND_CLASSIFICATION"


def test_scope_rejection_remains_independent_of_valid_schema(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    loop.tools.preview_tool_scope = lambda _action: {
        "allowed": False,
        "scope_diagnostic": {
            "reason_code": "WRITE_CEILING_DENIED",
            "failure_stage": "AUTHORIZATION",
            "category": "COMMAND",
            "cause_digest": "a" * 64,
        },
    }
    action, reason = admit_minimal_action(parse_minimal_decision(json.dumps({
        "type": "tool_call", "name": "list_directory",
        "arguments": {"path": TARGET},
    })), loop.tools)
    assert action is None
    assert reason == "scope_rejected:WRITE_CEILING_DENIED"
    d = loop.tools._last_minimal_admission_diagnostic
    assert d["repair_class"] == "AUTHORITY_OR_SCOPE_REASSESSMENT"
    assert d["scope_failure_stage"] == "AUTHORIZATION"
    assert d["authorization_granted"] is False
