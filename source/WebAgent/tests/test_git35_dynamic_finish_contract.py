"""Git35: task-generated Finish fields, dynamic goal checks and schema repair."""
from __future__ import annotations

import json

import pytest

from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import encode_result
from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
from agent_core.tri_one_task_result import make_task_result_from_webagent_loop
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET
from WebAgent.tests.test_minimal_core_v10 import tool_json


def _case(tmp_path, monkeypatch, *, request, tool="inspect_directory",
          present=True, exit_code=0, repair=False, with_checks=False,
          wrong_goal_check=False, claimed_fields=None, expect_status=None):
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    calls = []
    proposal = (
        tool_json("inspect_directory", paths=[TARGET], recursive=False)
        if tool == "inspect_directory" else
        tool_json("run_command", command="git status --short", operation="GIT_INSPECT")
    )
    def execute(action):
        calls.append(dict(action))
        if tool == "inspect_directory":
            return encode_result(
                "SUCCEEDED", "DIRECTORY_INSPECTED", tool=tool,
                executed=True, requested_path=TARGET, resolved_path=TARGET,
                data={
                    "observed_exists": present,
                    "observed_is_directory": present,
                    "observation_status": "CONFIRMED",
                },
            )
        return encode_result(
            "FAILED" if exit_code else "SUCCEEDED",
            "COMMAND_EXIT_NONZERO" if exit_code else "COMMAND_OK",
            tool=tool, executed=True,
            data={
                "process_state": "EXITED", "exit_code": exit_code,
                "timed_out": False, "verification_status":
                    "FAIL" if exit_code else "PASS",
            },
        )
    loop._execute_action = execute
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return proposal
        evidence = json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1]
                  .split("\n", 1)[0]
        )
        ref = evidence[-1]["evidence_ref"]
        assert ref in prompt
        if repair and len(loop.prompts) == 2:
            return json.dumps({
                "type": "finish", "summary": "第一次只有工具狀態",
                "expected_runtime": [{
                    "evidence_ref": ref, "fields": {"status": "SUCCEEDED"},
                }],
            })
        fields = claimed_fields or (
            {"status": "SUCCEEDED",
             "data.observed_exists": present} if tool == "inspect_directory" else
            {"status": expect_status or ("FAILED" if exit_code else "SUCCEEDED"),
             "data.exit_code": exit_code, "data.process_state": "EXITED"}
        )
        result = {
            "type": "finish", "summary": "根據實際結果完成",
            "expected_runtime": [{"evidence_ref": ref, "fields": fields}],
        }
        if with_checks:
            key, value = (
                ("data.observed_exists", present)
                if tool == "inspect_directory" else ("data.exit_code", exit_code)
            )
            result["goal_checks"] = [{
                "criterion": "原始需求的可觀測條件",
                "evidence_ref": ref,
                "field": key,
                "operator": "eq",
                "expected": (not value if wrong_goal_check else value),
            }]
        return json.dumps(result, ensure_ascii=False)
    loop.planner = planner
    answer = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    return loop, answer, board, calls


def test_dynamic_inspect_finish_does_not_require_fixed_tool_field_list(tmp_path, monkeypatch):
    loop, answer, board, calls = _case(
        tmp_path, monkeypatch,
        request="這資料夾是否存在\n" + TARGET, present=True,
        claimed_fields={"status": "SUCCEEDED", "data.observed_exists": True},
    )
    assert len(calls) == 1
    assert loop.terminal_outcome == "SUCCESS"
    assert board["terminal"]["finish_comparison"]["coverage_basis"] == "RUNTIME_INDEPENDENT_GOAL_PROOF"
    assert "Runtime available Finish fields" in loop.prompts[1]
    assert "data.observed_is_directory" in loop.prompts[1]
    assert "Runtime 最終驗收：PASS" in answer


def test_incomplete_model_finish_repairs_without_rerunning_tools(tmp_path, monkeypatch):
    loop, _, board, calls = _case(
        tmp_path, monkeypatch,
        request="這資料夾是否存在\n" + TARGET, present=True, repair=True,
    )
    assert loop.terminal_outcome == "SUCCESS"
    assert len(calls) == 1
    assert len(loop.prompts) == 3
    repairs = [e for e in board["events"] if e["kind"] == "MODEL_FINISH_SCHEMA_REPAIR"]
    assert len(repairs) == 1
    assert repairs[0]["reason_code"] == "MODEL_FINISH_GOAL_FACT_NOT_DECLARED"
    assert "不完整" in loop.prompts[2]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_arbitrary_goal_model_generates_criteria_runtime_only_checks_facts(
    tmp_path, monkeypatch, exit_code,
):
    request = "檢視執行紀錄，依實際程序退出碼確認是否符合本次檢查目標"
    loop, _, board, calls = _case(
        tmp_path, monkeypatch,
        request=request, tool="run_command",
        exit_code=exit_code, with_checks=True,
    )
    assert loop.terminal_outcome == "SUCCESS"
    assert len(calls) == 1
    comparison = board["terminal"]["finish_comparison"]
    assert comparison["goal_kind"] == "MODEL_GENERATED_GOAL_CHECKS"
    assert comparison["coverage_basis"] == "MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH"
    assert comparison["tool_results"][0]["execution_status"] == (
        "FAILED" if exit_code else "SUCCEEDED"
    )
    assert loop.terminal_acceptance["goal_coverage_basis"] == comparison["coverage_basis"]


def test_arbitrary_goal_wrong_runtime_predicate_is_fail(tmp_path, monkeypatch):
    loop, _, board, calls = _case(
        tmp_path, monkeypatch,
        request="彙整這次退出碼，確認檢查結果是否符合預期",
        tool="run_command", with_checks=True, wrong_goal_check=True,
    )
    assert len(calls) == 1
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "GOAL_CHECK_RUNTIME_MISMATCH"


def test_arbitrary_goal_without_checks_fail_closed(tmp_path, monkeypatch):
    loop, _, board, _ = _case(
        tmp_path, monkeypatch,
        request="彙整執行紀錄並確認符合驗收條件",
        tool="run_command", with_checks=False,
    )
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "MODEL_GOAL_CHECKS_REQUIRED"


def test_known_goal_cannot_pass_by_picking_negative_predicate(tmp_path, monkeypatch):
    loop, _, board, calls = _case(
        tmp_path, monkeypatch,
        request="請確保資料夾存在 " + TARGET, present=False, with_checks=True,
    )
    assert len(calls) == 1
    assert loop.terminal_outcome == "FAILED"
    assert board["terminal"]["finish_comparison"]["reason_code"] == "KNOWN_GOAL_POSTCONDITION_NOT_SATISFIED"


def test_tri_one_preserves_declared_goal_basis_and_tool_failure(tmp_path, monkeypatch):
    loop, _, board, _ = _case(
        tmp_path, monkeypatch,
        request="彙整執行紀錄並確認退出碼符合目前設定",
        tool="run_command", exit_code=1, with_checks=True,
    )
    loop.intent_digest = "a" * 64
    result = make_task_result_from_webagent_loop(loop, "完成執行結果確認")
    assert result["outcome"] == "SUCCESS"
    assert result["final_verdict"] == "PASS"
    assert result["goal_acceptance"]["coverage_basis"] == "MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH"
    assert result["goal_acceptance"]["tool_results"][0]["execution_status"] == "FAILED"
