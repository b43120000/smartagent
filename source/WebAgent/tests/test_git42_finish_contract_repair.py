"""Git42: typed Finish errors, no fabricated claims, bounded repair-only turns."""
from __future__ import annotations

import json
import pytest

from agent_core.finish_schema_diagnostic import diagnose_goal_check_schema
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import encode_result
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET
from WebAgent.tests.test_minimal_core_v10 import tool_json

REF = "MIN-EV-TEST001"
HISTORY = [{"evidence_ref": REF, "receipt": {"status": "SUCCEEDED",
            "data": {"observed_exists": True}}}]
CLAIMS = {REF: {"status": "SUCCEEDED", "data.observed_exists": True}}


def check(**changes):
    result = {"criterion": "目標狀態", "evidence_ref": REF,
              "field": "data.observed_exists", "operator": "eq",
              "expected": True}
    result.update(changes)
    return result


@pytest.mark.parametrize("changes,code,field", [
    ({"operator": "contains"}, "INVALID_OPERATOR", "operator"),
    ({"field": "data.unknown"}, "FIELD_NOT_DECLARED_IN_EXPECTED_RUNTIME", "field"),
    ({"field": "status"}, "FIELD_SYNTAX_NOT_DATA_SCALAR", "field"),
    ({"evidence_ref": "MIN-EV-NOTFOUND"}, "EVIDENCE_REF_NOT_IN_RUNTIME_CLAIMS", "evidence_ref"),
    ({"expected": [True]}, "EXPECTED_NOT_SCALAR", "expected"),
    ({"criterion": ""}, "CRITERION_TYPE_OR_LENGTH", "criterion"),
    ({"extra": "unsafe contents"}, "CHECK_KEYS_MISMATCH", "goal_checks"),
])
def test_precise_goal_check_diagnostic(changes, code, field):
    diag = diagnose_goal_check_schema(HISTORY, CLAIMS, [check(**changes)])
    assert diag["schema"] == "SMARTAGENT_FINISH_SCHEMA_DIAGNOSTIC_V1"
    assert diag["check_index"] == 0
    assert diag["field"] == field
    assert diag["constraint_code"] == code
    assert diag["evidence_unmodified"] is True
    assert diag["authorization_granted"] is False
    assert "unsafe contents" not in json.dumps(diag)


def test_repeated_criterion_diagnostic_identifies_second_check():
    diag = diagnose_goal_check_schema(HISTORY, CLAIMS, [check(), check()])
    assert diag["check_index"] == 1
    assert diag["constraint_code"] == "DUPLICATED_CRITERION"


def test_correct_goal_checks_have_no_diagnostic():
    assert diagnose_goal_check_schema(HISTORY, CLAIMS, [check()]) is None


def _finish_case(tmp_path, monkeypatch, *, mode: str):
    request = "根據實際觀測值確認本次檢查狀態，回報證據"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    loop.max_turns = 2
    calls = []
    def execute(action):
        calls.append(dict(action))
        return encode_result(
            "SUCCEEDED", "DIRECTORY_INSPECTED",
            tool="inspect_directory", executed=True,
            requested_path=TARGET, resolved_path=TARGET,
            data={"observed_exists": True,
                  "observed_is_directory": True,
                  "observation_status": "CONFIRMED"},
        )
    loop._execute_action = execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        round_no = len(loop.prompts)
        if round_no == 1:
            return tool_json("inspect_directory", paths=[TARGET], recursive=False)
        if mode == "tool_in_grace" and round_no == 3:
            return tool_json("inspect_directory", paths=[TARGET], recursive=False)
        actual = json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1]
                  .split("\n", 1)[0]
        )
        ref = actual[-1]["evidence_ref"]
        bad = (mode == "invalid" or round_no == 2)
        if round_no >= 3:
            assert "FIELD_NOT_DECLARED_IN_EXPECTED_RUNTIME" in prompt
            assert "check_index" in prompt
            assert "Runtime available Finish fields" in prompt
        data = {
            "type": "finish", "summary": "已完成實際檢查",
            "expected_runtime": [{
                "evidence_ref": ref,
                "fields": {"status": "SUCCEEDED", "data.observed_exists": True},
            }],
            "goal_checks": [{
                "criterion": "實際可觀測檢查",
                "evidence_ref": ref,
                "field": "data.observed_is_directory" if bad else "data.observed_exists",
                "operator": "eq", "expected": True,
            }],
        }
        return json.dumps(data, ensure_ascii=False)
    loop.planner = planner
    output = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    return loop, board, calls, output


def test_last_action_round_finish_can_be_repaired_in_nonexecuting_grace(tmp_path, monkeypatch):
    loop, board, calls, output = _finish_case(tmp_path, monkeypatch, mode="fixed")
    assert len(calls) == 1
    assert len(loop.prompts) == 3
    assert loop.terminal_outcome == "SUCCESS"
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    repairs = [row for row in board["events"] if row["kind"] == "MODEL_FINISH_SCHEMA_REPAIR"]
    assert len(repairs) == 1
    assert repairs[0]["round"] == 2
    assert repairs[0]["repair_only"] is True
    diag = repairs[0]["finish_diagnostic"]
    assert diag["field"] == "field"
    assert diag["constraint_code"] == "FIELD_NOT_DECLARED_IN_EXPECTED_RUNTIME"
    assert "Runtime 最終驗收：PASS" in output


def test_repeated_invalid_finish_reports_precise_terminal_not_max_rounds(tmp_path, monkeypatch):
    loop, board, calls, output = _finish_case(tmp_path, monkeypatch, mode="invalid")
    assert len(calls) == 1
    assert len(loop.prompts) == 4
    assert board["executor_state"] == "FINISH_CONTRACT_INVALID"
    assert board["terminal"]["goal_verified"] is False
    invalid = [row for row in board["events"] if row["kind"] == "MODEL_FINISH_CONTRACT_INVALID"]
    assert invalid[-1]["reason_code"] == "MODEL_GOAL_CHECK_SCHEMA_INVALID"
    assert invalid[-1]["finish_diagnostic"]["check_index"] == 0
    assert "FINISH_CONTRACT_INVALID" in output
    assert loop.terminal_outcome != "SUCCESS"


def test_repair_only_grace_refuses_tool_call_without_side_effect(tmp_path, monkeypatch):
    loop, board, calls, output = _finish_case(tmp_path, monkeypatch, mode="tool_in_grace")
    assert len(calls) == 1
    assert len(loop.prompts) == 3
    assert board["executor_state"] == "FINISH_CONTRACT_INVALID"
    invalid = [row for row in board["events"] if row["kind"] == "MODEL_FINISH_CONTRACT_INVALID"]
    assert invalid[-1]["reason_code"] == "REPAIR_ONLY_TOOLCALL_PROHIBITED"
    assert len([x for x in board["events"] if x["kind"] == "TOOL_RESULT"]) == 1
