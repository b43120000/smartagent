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
    assert rejected[0]["kind"] == "ADMISSION_REJECTED"
    assert "toolResult" not in rejected[0]
    assert rejected[0]["runtimeEvidence"]["executed"] is False
    assert "untyped verification" not in json.dumps(board)
    assert "git clone" not in json.dumps(board)
    assert len(loop.executed) == 1
    assert loop.executed[0]["command"] == "git status --short"
    assert len(loop.action_result_ledger) == 1
    assert "field_issues" in loop.prompts[1]
    assert '"field":"verify"' in loop.prompts[1]
    assert board["terminal"]["admission_rejection_count"] == 1


def test_repeated_same_schema_issue_stops_for_same_command(
    tmp_path, monkeypatch,
):
    request = "請使用 git 取得指定來源到 " + TARGET
    replies = []
    for i in range(5):
        replies.append(json.dumps({"type": "tool_call", "name": "run_command",
                                   "arguments": {
                                       "command": "git clone https://github.com/b43120000/test.git",
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
    # Git39: a command whose read-only status is UNKNOWN requires the
    # stronger mutation contract *before* per-action human approval.
    assert action is None and reason == "mutation_success_criteria_required"
    assert loop.tools._last_minimal_admission_diagnostic["stage"] == "POSTCONDITION_CONTRACT"
    assert loop.tools._last_command_risk_class == "UNKNOWN"


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


@pytest.mark.parametrize("operation", ["BUILD", "MUTATE", "TEST", "PROCESS", "INSPECT"])
def test_unproven_command_diagnoses_both_postconditions(tmp_path, monkeypatch, operation):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    args = {"command": "where.exe g++", "operation": operation}
    assert admit_minimal_action(proposed(args), loop.tools)[1] == "mutation_success_criteria_required"
    diag = loop.tools._last_minimal_admission_diagnostic
    assert diag["command_read_only_proven"] is False
    assert [(x["field"], x["expected_type"], x["actual_type"]) for x in diag["field_issues"]] == [
        ("success_criteria", "str", "MISSING"),
        ("verify", "list | dict", "MISSING"),
    ]
    args["success_criteria"] = "Compiler path is reported"
    args["verify"] = []
    assert admit_minimal_action(proposed(args), loop.tools)[1] == "mutation_verification_required"
    issue, = loop.tools._last_minimal_admission_diagnostic["field_issues"]
    assert (issue["field"], issue["actual_type"]) == ("verify", "list")


def test_verify_cannot_relabel_unproven_compiler_lookup(tmp_path, monkeypatch):
    loop, _ = _loop(tmp_path, monkeypatch, [])
    args = {"command": "where.exe g++", "operation": "VERIFY"}
    assert admit_minimal_action(proposed(args), loop.tools)[1] == "run_command_operation_mismatch"


def test_historical_compiler_lookup_sidecar(tmp_path, monkeypatch):
    # The Case C Blackboard retained identical arguments in rounds 59–61.
    args = {"command": "where.exe g++", "operation": "INSPECT", "timeout": 15000,
            "working_directory": r"E:\AIspace\case_c"}
    reply = json.dumps({"type": "tool_call", "name": "run_command", "arguments": args})
    request = "Discover the C++ compiler available for the build."
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    prompts = []

    def planner(prompt, *_):
        prompts.append(prompt)
        return reply

    loop.planner = planner
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = [row for row in board["events"] if row.get("kind") == "ADMISSION_REJECTED"]
    assert [row["admission_repair_attempt"] for row in rejected] == [1, 2, 3]
    for row, prompt in zip(rejected, prompts[1:]):
        assert row["runtimeEvidence"]["reason_code"] == "mutation_success_criteria_required"
        hint = prompt.split("Runtime repair information: ", 1)[1]
        assert '"command_risk_class":"UNKNOWN"' in hint
        assert '"field":"success_criteria"' in hint
        assert '"field":"verify"' in hint
    assert board["executor_state"] == "BLOCKED"
    assert not loop.executed


@pytest.mark.parametrize("repair", [False, True])
def test_executor_repairs_build_contract_before_deterministic_dispatch(
    tmp_path, monkeypatch, repair,
):
    request = "Build the requested project and verify its output."
    bad = {"command": "g++ -std=c++20 main.cpp -o goal.exe", "operation": "BUILD"}
    fixed = dict(
        bad, success_criteria="The compiler produces goal.exe",
        verify=[{"action": "file_exists", "path": str(tmp_path / "goal.exe"), "expect": True}],
    )
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    # Simulate an eligible ACL OFF / Auto Approve ON policy in isolation.
    approvals = []
    def approve(workspace, command, *, working_directory):
        approvals.append(command)
        return {
            "approved": True, "reason_code": "AUTO_APPROVE_SECONDARY_CONFIRMATION",
            "acl": "OFF", "turbo_requested": "OFF", "turbo_effective": "OFF",
            "auto_approve_requested": "ON", "auto_approve_effective": "ON",
            "authorization_scope": str(workspace), "policy_revision": 1,
            "executor_backend": "WINDOWS_EXECUTOR", "command_sha256": "a" * 64,
        }

    monkeypatch.setattr("agent_core.auto_approve_mode.evaluate_auto_approve", approve)

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        turn = len(loop.prompts)
        if turn == 3 and repair:
            recent = json.loads(prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1]
                                .split("\n", 1)[0])
            ref = recent[-1]["evidence_ref"]
            return json.dumps({
                "type": "finish", "summary": "Build exit verified",
                "expected_runtime": [{"evidence_ref": ref, "fields": {
                    "status": "SUCCEEDED", "data.exit_code": 0,
                }}],
                "goal_checks": [{"criterion": "Build exited successfully",
                                 "evidence_ref": ref, "field": "data.exit_code",
                                 "operator": "eq", "expected": 0}],
            })
        if turn > 1:
            hint = prompt.split("Runtime repair information: ", 1)[1]
            assert '"field":"success_criteria"' in hint
            assert '"field":"verify"' in hint
            assert '"operation":"BUILD"' in hint
            assert "Keep BUILD" in hint
        # Keep success_criteria missing while changing the secondary issue.
        arguments = fixed if repair and turn == 2 else (
            dict(bad, verify=fixed["verify"]) if turn == 3 else bad)
        return json.dumps({"type": "tool_call", "name": "run_command",
                           "arguments": arguments})

    loop.planner = planner
    run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    attempts = [e["admission_repair_attempt"] for e in board["events"]
                if e.get("kind") == "ADMISSION_REJECTED"]
    if repair:
        assert attempts == [1]
        assert len(loop.executed) == 1
        assert loop.executed[0]["operation"] == "BUILD"
        assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
        assert len(approvals) == 1
    else:
        assert attempts == [1, 2, 3]
        assert loop.executed == []
        assert board["executor_state"] == "BLOCKED"
        assert not approvals
