"""Git56 Finish semantic repair: model assertions != real task state.

Unit tests are deterministic and preserve original requests, Runtime ToolResults,
Goal Contracts and kernel authorization. No false success is inferred from
a successful repair invitation.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from agent_core.finish_semantic_repair import diagnose_finish_semantic_mismatch
from agent_core.runtime_finish_comparator import compare_runtime_finish
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import encode_result
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
from WebAgent.tests.test_minimal_core_v10 import tool_json


def _mismatch(reason, *, field="data.exit_code", expected=0, actual=1):
    return {
        "verdict": "FAIL", "reason_code": reason,
        "mismatches": [{
            "evidence_ref": "MIN-EV-" + "a" * 24,
            "field": field, "criterion": "回報真實測試結果",
            "operator": "eq", "expected": expected, "actual": actual,
        }],
    }


def test_typed_runtime_fact_mismatch_repair_is_not_authorization():
    state = diagnose_finish_semantic_mismatch(
        _mismatch("MODEL_RUNTIME_MISMATCH", field="data.exit_code"),
        "請檢視這次執行紀錄並回報 Exit Code",
    )
    assert state["repair_class"] == "MODEL_FACT_DECLARATION_REPAIR"
    assert state["failure_origin"] == "PLANNER_ASSERTION_MISMATCH"
    assert state["repair_only"] is True
    assert state["new_tool_result_required"] is False
    assert state["goal_acceptance_not_granted"] is True
    assert state["evidence_unmodified"] is True
    assert state["authorization_granted"] is False
    assert state["corrections"][0]["runtime_actual_value"] == 1


def test_boolean_native_identity_false_may_repair_planner_factual_claim():
    state = diagnose_finish_semantic_mismatch(
        _mismatch("MODEL_RUNTIME_MISMATCH",
                  field="data.sandbox_evidence.native_executable_identity_verified",
                  expected=True, actual=False),
        "檢視 Git/Python 程序資訊並如實回報",
    )
    assert state["corrections"][0]["runtime_actual_value"] is False
    assert state["repair_only"] is True


def test_generic_observation_predicate_mismatch_can_be_reviewed():
    state = diagnose_finish_semantic_mismatch(
        _mismatch("GOAL_CHECK_RUNTIME_MISMATCH"),
        "檢視執行紀錄，依實際程序退出碼確認是否符合本次檢查目標",
    )
    assert state["repair_class"] == "MODEL_GOAL_PREDICATE_SEMANTIC_REVIEW"
    assert state["goal_contract_unchanged"] is True


@pytest.mark.parametrize("goal", [
    "請確保資料夾存在 E:\\demo",
    "必須成功完成部署",
    "建立一個資料夾並驗證",
    "請刪除這個檔案",
])
def test_desired_state_can_never_be_relabelled_to_observation(goal):
    result = diagnose_finish_semantic_mismatch(
        _mismatch("GOAL_CHECK_RUNTIME_MISMATCH",
                  field="data.observed_exists",
                  expected=True, actual=False),
        goal,
    )
    assert result is not None
    assert result["repair_class"] == "GOAL_STATE_NOT_RELABELABLE"
    assert result["repair_only"] is False


@pytest.mark.parametrize("field,actual", [
    ("data.stdout", "my-password-secret"),
    ("data.stderr", "PRIVATE"),
    ("data.command", "secret"),
    ("data.path", "E:\\sensitive"),
    ("data.sandbox_evidence.private_key", "abc"),
    ("data.exit_code", "[MISSING]"),
    ("data.exit_code", {"redacted_sha256": "abcd"}),
])
def test_never_expose_secret_or_absent_evidence_in_repair(field, actual):
    assert diagnose_finish_semantic_mismatch(
        _mismatch("MODEL_RUNTIME_MISMATCH", field=field, actual=actual),
        "請檢查結果",
    ) is None


def test_exact_state_mismatch_is_real_goal_failure_not_planner_repair():
    from agent_core.requirement_goal_state import (
        compile_requirement_goal_state, evaluate_frozen_goal_contract,
    )
    from agent_core.tool_result_contract import decode_result
    goal = r"請確保資料夾存在 E:\demo"
    contract = compile_requirement_goal_state(goal)
    assert contract["status"] == "COMPILED"
    observed = decode_result(encode_result(
        "SUCCEEDED", "DIRECTORY_INSPECTED", tool="inspect_directory",
        executed=True, data={
            "observed_exists": False, "observed_is_directory": False,
            "observation_status": "CONFIRMED",
        },
    ))
    state = evaluate_frozen_goal_contract(contract, [{
        "action": {"tool": "inspect_directory", "paths": [r"E:\demo"]},
        "receipt": observed, "evidence_ref": "MIN-EV-" + "c" * 24,
    }], "已檢查")
    assert state["verdict"] == "FAIL"
    assert state["unsatisfied_condition_ids"] == ["S01"]


def _exercise_loop(tmp_path, monkeypatch, *, wrong_kind, stuck=False):
    request = "檢視執行紀錄，依實際程序退出碼確認是否符合本次檢查目標"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    executed = []
    def run(action):
        executed.append(dict(action))
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED",
            tool="run_command", executed=True,
            data={
                "process_state": "EXITED", "exit_code": 1,
                "stdout": "", "stderr": "",
                "verification_status": "UNVERIFIED", "timed_out": False,
            },
        )
    loop._execute_action = run
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return tool_json("run_command", command="git status --short",
                             operation="GIT_INSPECT")
        # One unique real Evidence ref is available after command execution.
        tail = prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1].split("\n", 1)[0]
        ref = json.loads(tail)[-1]["evidence_ref"]
        wrong = len(loop.prompts) == 2 or stuck
        fields = {"status": "SUCCEEDED", "data.exit_code": 1}
        checks = [{
            "criterion": "回報真實的退出碼",
            "evidence_ref": ref, "field": "data.exit_code",
            "operator": "eq", "expected": 0 if wrong else 1,
        }]
        if wrong_kind == "fact" and wrong:
            fields["data.exit_code"] = 0
            checks[0]["expected"] = 1
        return json.dumps({
            "type": "finish",
            "summary": "已依 Runtime Evidence 檢查實際 Exit Code",
            "expected_runtime": [{
                "evidence_ref": ref, "fields": fields,
            }],
            "goal_checks": checks,
        }, ensure_ascii=False)
    loop.planner = planner
    result = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    return loop, result, board, executed


@pytest.mark.parametrize("wrong_kind", ["fact", "predicate"])
def test_real_executor_loop_repairs_finish_semantics_without_reexecuting_tools(
    tmp_path, monkeypatch, wrong_kind,
):
    loop, reply, board, executed = _exercise_loop(
        tmp_path, monkeypatch, wrong_kind=wrong_kind)
    assert len(executed) == 1
    assert len(loop.prompts) == 3
    assert loop.terminal_outcome == "SUCCESS", reply
    repairs = [e for e in board["events"]
               if e["kind"] == "MODEL_FINISH_SEMANTIC_REPAIR"]
    assert len(repairs) == 1
    assert repairs[0]["failure_origin"] == "PLANNER_ASSERTION_MISMATCH"
    assert repairs[0]["repair_only"] is True
    assert repairs[0]["finish_diagnostic"]["goal_acceptance_not_granted"] is True
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    assert "Runtime 最終驗收：PASS" in reply


def test_repeated_wrong_semantic_finish_exhausts_repair_without_false_pass(
    tmp_path, monkeypatch,
):
    loop, reply, board, executed = _exercise_loop(
        tmp_path, monkeypatch, wrong_kind="predicate", stuck=True)
    assert len(executed) == 1
    assert loop.terminal_outcome != "SUCCESS"
    assert board["executor_state"] == "FINISH_CONTRACT_INVALID"
    assert board["terminal"]["goal_verified"] is False
    assert sum(e["kind"] == "MODEL_FINISH_SEMANTIC_REPAIR"
               for e in board["events"]) >= 1
    assert "FINISH_CONTRACT_INVALID" in reply or "Finish 契約" in reply


def test_comparator_failure_origin_classes():
    from agent_core.runtime_finish_comparator import _fail
    assert _fail("MODEL_RUNTIME_MISMATCH")["failure_origin"] == \
        "PLANNER_FACT_DECLARATION_ERROR"
    assert _fail("GOAL_CHECK_RUNTIME_MISMATCH")["failure_origin"] == \
        "PLANNER_PREDICATE_OR_GOAL_UNRESOLVED"
    assert _fail("FROZEN_GOAL_STATE_NOT_SATISFIED")["failure_origin"] == \
        "GOAL_STATE_NOT_SATISFIED"
    assert _fail("TOOL_EVIDENCE_REQUIRED")["failure_origin"] == \
        "EVIDENCE_OR_FINISH_CONTRACT_INCOMPLETE"
