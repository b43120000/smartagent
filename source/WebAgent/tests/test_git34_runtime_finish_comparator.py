"""Git34: Runtime Finish Comparator, negative-goal semantics and Tri-One mapping."""
from __future__ import annotations

import json

import pytest

from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import encode_result
from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
from agent_core.tri_one_task_result import make_task_result_from_webagent_loop
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET, REQUEST
from WebAgent.tests.test_minimal_core_v10 import tool_json


def _run(tmp_path, monkeypatch, *, request, present, expected=None, tool="inspect_directory",
         runtime_status="SUCCEEDED", exit_code=0):
    replies = [tool_json(tool, **(
        {"paths": [TARGET], "recursive": False} if tool == "inspect_directory"
        else {"command": "git status --short", "operation": "GIT_INSPECT"}
    ))]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    def execute(action):
        loop.executed.append(dict(action))
        if tool == "inspect_directory":
            return encode_result(
                runtime_status, "DIRECTORY_INSPECTED", tool=tool,
                executed=True, requested_path=TARGET, resolved_path=TARGET,
                data={
                    "observed_exists": present,
                    "observed_is_directory": present,
                    "observation_status": "CONFIRMED",
                },
            )
        return encode_result(
            runtime_status, "COMMAND_EXIT_NONZERO" if exit_code else "COMMAND_OK",
            tool=tool, executed=True,
            data={"process_state": "EXITED", "exit_code": exit_code,
                  "timed_out": False, "verification_status": "FAIL"
                  if exit_code else "PASS", "expectation_status": "PASS",
                  "stdout": "sample", "stderr": ""},
        )
    loop._execute_action = execute
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return replies[0]
        history = json.loads(prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1].split("\n", 1)[0])
        ref = history[-1]["evidence_ref"]
        if expected == "missing":
            return json.dumps({"type": "finish", "summary": "已檢查"})
        fields = (
            {"status": runtime_status, "data.observed_exists": present,
             "data.observed_is_directory": present, "data.observation_status": "CONFIRMED"}
            if tool == "inspect_directory" else
            {"status": runtime_status, "data.exit_code": exit_code,
             "data.process_state": "EXITED"}
        )
        if expected == "mismatch":
            fields["data.observed_exists"] = not present
        if expected == "unknown":
            fields["status"] = "UNKNOWN"
        return json.dumps({
            "type": "finish", "summary": "已依 Runtime ToolResult 完成檢查",
            "expected_runtime": [{"evidence_ref": ref, "fields": fields}],
        }, ensure_ascii=False)
    loop.planner = planner
    reply = run_minimal_core(loop, request)
    return loop, reply, read_executor_run(tmp_path, task_id=loop.task_id)


@pytest.mark.parametrize("present", [True, False])
def test_single_inspect_fact_query_passes_for_positive_and_negative(tmp_path, monkeypatch, present):
    request = "這個資料夾是否存在？\n" + TARGET
    loop, reply, board = _run(tmp_path, monkeypatch, request=request, present=present)
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
    assert loop.terminal_acceptance["verification_status"] == "PASS"
    assert terminal_verdict_from_loop(loop).outcome == "SUCCESS"
    assert board["terminal"]["goal_verified"] is True
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    assert board["events"][1]["runtimeEvidence"]["status"] == "SUCCEEDED"
    assert "Runtime 最終驗收：PASS" in reply


def test_goal_assertion_absence_passes_without_confusing_false_with_tool_failure(
    tmp_path, monkeypatch,
):
    loop, _, board = _run(
        tmp_path, monkeypatch, request="請確認資料夾不存在 " + TARGET,
        present=False,
    )
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
    assert board["terminal"]["finish_comparison"]["tool_results"][0]["execution_status"] == "SUCCEEDED"


def test_model_runtime_field_mismatch_is_terminal_fail(tmp_path, monkeypatch):
    loop, reply, board = _run(
        tmp_path, monkeypatch, request="這個資料夾是否存在？\n" + TARGET,
        present=False, expected="mismatch",
    )
    assert terminal_verdict_from_loop(loop).outcome == "FAILED"
    assert loop.terminal_acceptance["final_verdict"] == "FAIL"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "MODEL_RUNTIME_MISMATCH"
    assert board["terminal"]["finish_comparison"]["mismatches"][0]["field"] == "data.observed_exists"
    assert "Runtime 最終驗收：FAIL" in reply


def test_missing_claim_is_fail_not_unknown(tmp_path, monkeypatch):
    loop, _, board = _run(
        tmp_path, monkeypatch, request="這個資料夾是否存在？\n" + TARGET,
        present=True, expected="missing",
    )
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "EXPECTED_RUNTIME_COVERAGE_MISMATCH"


def test_even_matching_unknown_is_not_pass(tmp_path, monkeypatch):
    loop, _, board = _run(
        tmp_path, monkeypatch, request="這個資料夾是否存在？\n" + TARGET,
        present=True, expected="unknown", runtime_status="UNKNOWN",
    )
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "RUNTIME_EXECUTION_NOT_CONFIRMED"


def test_unproven_multistep_goal_fails_closed(tmp_path, monkeypatch):
    loop, _, board = _run(tmp_path, monkeypatch, request=REQUEST, present=True)
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "MODEL_GOAL_CHECKS_REQUIRED"


def test_command_nonzero_tool_failure_can_pass_explicit_negative_goal(tmp_path, monkeypatch):
    loop, _, board = _run(
        tmp_path, monkeypatch,
        request="執行 git status --short 並回報失敗原因",
        present=False, tool="run_command",
        runtime_status="FAILED", exit_code=1,
    )
    assert loop.terminal_outcome == "SUCCESS"
    assert board["terminal"]["finish_comparison"]["tool_results"][0]["execution_status"] == "FAILED"
    assert board["terminal"]["finish_comparison"]["goal_kind"] == "COMMAND_EXPECTED_FAILURE"


def test_task_result_has_separate_final_and_tool_status(tmp_path, monkeypatch):
    loop, _, board = _run(
        tmp_path, monkeypatch, request="這個資料夾是否存在？\n" + TARGET,
        present=False,
    )
    loop.intent_digest = "f" * 64
    result = make_task_result_from_webagent_loop(loop, "確認資料夾不存在")
    assert result["final_verdict"] == "PASS"
    assert result["goal_acceptance"]["verdict"] == "PASS"
    assert result["goal_acceptance"]["tool_results"][0]["execution_status"] == "SUCCEEDED"
    assert result["outcome"] == "SUCCESS"


def test_task_result_fail_is_binary_and_preserves_tool_success(tmp_path, monkeypatch):
    loop, _, _ = _run(
        tmp_path, monkeypatch, request="這個資料夾是否存在？\n" + TARGET,
        present=False, expected="mismatch",
    )
    loop.intent_digest = "f" * 64
    result = make_task_result_from_webagent_loop(loop, "不符合模型預期")
    assert result["final_verdict"] == "FAIL"
    assert result["goal_acceptance"]["reason_code"] == "MODEL_RUNTIME_MISMATCH"
    assert result["goal_acceptance"]["tool_results"][0]["execution_status"] == "SUCCEEDED"
