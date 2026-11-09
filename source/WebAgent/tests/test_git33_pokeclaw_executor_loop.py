"""Git33: a general PokeClaw ToolCall -> ToolResult -> Model loop.

No task-specific stage regex, Goal Driver, Prerequisite or invented Goal PASS.
ToolResult uses exactly three PokeClaw fields; Runtime's additional statuses
and evidence are recorded separately in durable Executor Blackboard.
"""
from __future__ import annotations

import json
import os

import pytest

from agent_core.evidence_foundation import observation
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import attach_runtime_result, encode_result
from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.protocol_loop import extract_authorized_paths
from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json

TARGET = r"E:\SmartAgent_TriLayer_Blackboard_Test_20261109"
REQUEST = (
    f"請在 {TARGET} 執行三層 Blackboard 整合驗證：\n"
    "1. 使用 Executor 檢查目標資料夾是否存在。\n"
    "2. 如果不存在，建立資料夾；如果已存在，不要重複建立。\n"
    "3. 使用實際 ToolResult 確認目標是資料夾，不能只靠模型文字宣告成功。\n"
    "4. Executor 完成後，將最終結果及 Evidence 寫入 Shared Blackboard。\n"
    "5. 檢查 Blackboard 中 Executor、Prerequisite、Goal Driver 三層的獨立狀態，"
    "確認 Executor 成功不會直接將其他兩層標記為完成。\n"
    "6. 回報已觀測的三層狀態、Evidence References、Handoff ID，以及是否存在尚未消費的結果。\n"
    "限制：僅能在上述指定目標範圍操作，不得刪除、覆蓋或修改 E 槽其他內容。"
    "若某項 Blackboard 狀態無法觀測，請明確回報未觀測，不得自行推測。"
)


def _result(action, *, exists=False, status="SUCCEEDED"):
    tool = action["tool"]
    path = action.get("path") or (action.get("paths") or [""])[0]
    if tool == "observe_fact":
        return attach_runtime_result(
            "{}", status,
            "FACT_OBSERVED" if status == "SUCCEEDED" else "FACT_OBSERVATION_UNKNOWN",
            tool=tool, executed=True, requested_path=path, resolved_path=path,
            action_id=action["action_id"],
            data={
                "observations": [observation(
                    "file.exists", exists, source="filesystem.stat",
                    scope=path, raw_ref=action["action_id"],
                )],
                "observation_status": "CONFIRMED",
                "terminal_eligible": True,
            },
        )
    if tool == "create_directory":
        return encode_result(
            status, "DIRECTORY_CREATED" if status == "SUCCEEDED" else "UNKNOWN_MKDIR",
            tool=tool, executed=True, requested_path=path, resolved_path=path,
            data={
                "directory_created": True,
                "observed_exists": True, "observed_is_directory": True,
                "observation_status": "CONFIRMED",
                "no_parent_directories_created": True,
            },
        )
    if tool == "list_directory":
        return encode_result(
            status, "DIRECTORY_LISTED",
            tool=tool, executed=True, requested_path=path, resolved_path=path,
            data={"entries": [], "terminal_eligible": True},
        )
    if tool == "run_command":
        return encode_result(
            status, "COMMAND_OK" if status == "SUCCEEDED" else "COMMAND_EXIT_NONZERO",
            tool=tool, executed=True,
            data={
                "exit_code": 0 if status == "SUCCEEDED" else 1,
                "stdout": "test output", "stderr": "",
                "timed_out": False, "verification_status": "PASS",
            },
        )
    raise AssertionError("unknown mock tool " + tool)


def _loop(tmp_path, monkeypatch, replies, *, request=REQUEST, initial=False,
          preview=None):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kwargs: None)
    loop = FakeLoop(tmp_path, request, replies, "")
    loop.executor_only_mode = True
    state = {"exists": initial, "mutations": 0}
    def mock_execute(action):
        loop.executed.append(dict(action))
        if action["tool"] == "create_directory":
            assert not state["exists"]
            state["mutations"] += 1
            state["exists"] = True
            return _result(action)
        return _result(action, exists=state["exists"])
    loop._execute_action = mock_execute
    if preview:
        loop.tools.preview_tool_scope = preview
    return loop, state


def test_exact_original_request_path_parser_does_not_attach_chinese_task_header():
    assert extract_authorized_paths(REQUEST) == [TARGET]


def test_one_generic_agent_loop_completes_check_create_verify_finish_and_blackboard(
    tmp_path, monkeypatch
):
    replies = [
        tool_json("observe_fact", path=TARGET),
        tool_json("create_directory", path=TARGET),
        tool_json("list_directory", path=TARGET),
        json.dumps({"type": "finish", "summary": "已觀測不存在、建立一次、重新列出目錄；其他兩層未啟動。"}),
    ]
    loop, state = _loop(tmp_path, monkeypatch, replies)
    answer = run_minimal_core(loop, REQUEST)
    assert state["mutations"] == 1
    assert [act["tool"] for act in loop.executed] == [
        "observe_fact", "create_directory", "list_directory",
    ]
    assert len(loop.prompts) == 4
    assert all("POKECLAW_EXECUTOR_LOOP_V1" in prompt for prompt in loop.prompts)
    assert all("EXECUTOR_ONLY_DIRECTORY_SEQUENCE" not in prompt for prompt in loop.prompts)
    assert all("permitted_tools_by_phase" not in prompt for prompt in loop.prompts)
    assert "Runtime repair" not in loop.prompts[0]
    assert '"isSuccess":true' in loop.prompts[1]
    assert "file.exists" in loop.prompts[1]
    assert "已觀測不存在" in answer
    assert "Goal Driver：DISABLED" in answer
    assert "Prerequisite：DISABLED" in answer
    assert "HOF-" in answer
    assert "MIN-EV-" in answer
    assert loop.terminal_outcome == "FAILED"  # old finish omitted expected_runtime
    assert loop.terminal_acceptance["completion_kind"] == "EVIDENCE_BACKED"
    assert loop.terminal_acceptance["evidence_verdict"] == "FAIL"
    assert terminal_verdict_from_loop(loop).state == "COMPLETED"
    assert terminal_verdict_from_loop(loop).outcome == "FAILED"
    board = read_executor_run(
        tmp_path, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    assert board["executor_state"] == "MODEL_REPORTED_COMPLETE_UNVERIFIED"
    assert board["goal_driver_state"] == "DISABLED"
    assert board["prerequisite_state"] == "DISABLED"
    assert board["parent_dispatch_state"] == "NOT_DISPATCHED"
    assert board["parent_consumption_state"] == "NOT_APPLICABLE_EXECUTOR_ONLY"
    calls = [row for row in board["events"] if row["kind"] == "TOOL_RESULT"]
    assert len(calls) == 3
    assert len(board["events"]) == 7  # 3 admitted + 3 results + finish
    assert len(board["terminal"]["tool_evidence_refs"]) == 3
    assert len(loop.action_result_ledger) == 3
    for call in calls:
        assert set(call["toolResult"]) == {"isSuccess", "data", "error"}
        assert call["toolResult"]["isSuccess"] is True
        assert call["toolResult"]["error"] is None
        assert call["runtimeEvidence"]["status"] == "SUCCEEDED"
        assert call["runtimeEvidence"]["executed"] is True
        assert call["runtimeEvidence"]["evidence_ref"].startswith("MIN-EV-")


def test_existing_directory_skip_create_no_workflow_special_case(tmp_path, monkeypatch):
    replies = [
        tool_json("observe_fact", path=TARGET),
        tool_json("list_directory", path=TARGET),
        json.dumps({"type": "finish", "summary": "目錄原本存在，未建立新資料夾。"}),
    ]
    loop, state = _loop(tmp_path, monkeypatch, replies, initial=True)
    answer = run_minimal_core(loop, REQUEST)
    assert state["mutations"] == 0
    assert [a["tool"] for a in loop.executed] == ["observe_fact", "list_directory"]
    assert "未建立新資料夾" in answer


def test_observed_negative_fact_is_tool_success_not_failure(tmp_path, monkeypatch):
    request = "這個檔案存在嗎？\n" + TARGET
    replies = [
        tool_json("observe_fact", path=TARGET),
        '{"type":"finish","summary":"經查詢檔案不存在。"}',
    ]
    loop, state = _loop(tmp_path, monkeypatch, replies, request=request)
    text = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    call = [row for row in board["events"] if row["kind"] == "TOOL_RESULT"][0]
    assert call["toolResult"]["isSuccess"] is True
    assert '"value":false' in call["toolResult"]["data"]
    assert "不存在" in text
    assert board["terminal"]["goal_verified"] is False


def test_real_scope_rejection_returns_pokeclaw_error_before_execution(
    tmp_path, monkeypatch
):
    from agent_core.action_readiness import classify_scope_preflight
    def denied(action):
        if action["tool"] != "create_directory":
            return {"allowed": True}
        return {
            "allowed": False,
            "scope_diagnostic": classify_scope_preflight(
                "create_directory",
                "directory_target_not_exact_original_request:E:\\Bad",
                allowed=False,
            ),
            "directory_preconditions": {
                "write_authorization_verdict": "AUTHORIZED_BY_EXISTING_POLICY",
                "target_binding_verdict": "TARGET_NOT_EXACT",
                "parent_directory_verdict": "NOT_EVALUATED",
            },
        }
    replies = [
        tool_json("observe_fact", path=TARGET),
        tool_json("create_directory", path=TARGET),
        '{"type":"blocked","reason":"scope rejected, stop"}',
    ]
    loop, state = _loop(tmp_path, monkeypatch, replies, preview=denied)
    result = run_minimal_core(loop, REQUEST)
    assert state["mutations"] == 0
    assert [a["tool"] for a in loop.executed] == ["observe_fact"]
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    rejected = [row for row in board["events"] if row.get("runtimeEvidence", {}).get("status") == "REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["toolResult"] == {
        "isSuccess": False, "data": None,
        "error": "ACTION_NOT_EXECUTED:scope_rejected:DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST",
    }
    assert rejected[0]["runtimeEvidence"]["executed"] is False
    assert rejected[0]["runtimeEvidence"]["directory_preconditions"]["target_binding_verdict"] == "TARGET_NOT_EXACT"
    assert "scope_rejected:DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_REQUEST" in loop.prompts[2]
    assert board["terminal"]["admission_rejection_count"] == 1
    assert loop.terminal_acceptance == {}
    assert board["executor_state"] == "BLOCKED"


def test_invalid_schema_then_repair_without_goal_driver(tmp_path, monkeypatch):
    replies = [
        '{"type":"tool_call","name":"create_directory","arguments":{"action_id":"forged","path":"E:\\\\Bad"}}',
        tool_json("observe_fact", path=TARGET),
        '{"type":"finish","summary":"觀測完畢"}',
    ]
    loop, state = _loop(tmp_path, monkeypatch, replies)
    result = run_minimal_core(loop, REQUEST)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    denied = [row for row in board["events"] if row.get("runtimeEvidence", {}).get("status") == "REJECTED"]
    assert len(denied) == 1
    assert denied[0]["toolResult"]["isSuccess"] is False
    assert denied[0]["runtimeEvidence"]["executed"] is False
    assert len(loop.executed) == 1
    assert "觀測完畢" in result


def test_same_generic_loop_accepts_shell_tool_success_and_failure(tmp_path, monkeypatch):
    request = "執行 git status --short 並回報輸出和退出碼"
    replies = [
        tool_json("run_command", command="git status --short", operation="GIT_INSPECT"),
        '{"type":"finish","summary":"命令已執行並返回。"}',
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    output = run_minimal_core(loop, request)
    assert "命令已執行" in output
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    result = next(row for row in board["events"] if row["kind"] == "TOOL_RESULT")
    assert result["toolResult"]["isSuccess"] is True
    assert '"exit_code":0' in result["toolResult"]["data"]
    assert result["runtimeEvidence"]["status"] == "SUCCEEDED"


def test_shell_nonzero_exit_is_failure_with_typed_process_receipt(
    tmp_path, monkeypatch,
):
    request = "執行 git status --short 並回報失敗原因"
    replies = [
        tool_json("run_command", command="git status --short", operation="GIT_INSPECT"),
        '{"type":"blocked","reason":"命令 exit_code=1，不能宣告成功"}',
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies, request=request)
    def fail(action):
        loop.executed.append(dict(action))
        return _result(action, status="FAILED")
    loop._execute_action = fail
    answer = run_minimal_core(loop, request)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    result = next(row for row in board["events"] if row["kind"] == "TOOL_RESULT")
    assert result["toolResult"]["isSuccess"] is False
    assert result["toolResult"]["data"] is None
    assert "TOOL_FAILED" in result["toolResult"]["error"]
    assert '"exit_code":1' in result["toolResult"]["error"]
    assert result["runtimeEvidence"]["status"] == "FAILED"
    assert result["runtimeEvidence"]["process"]["exit_code"] == 1
    assert result["runtimeEvidence"]["process"]["timed_out"] is False
    assert "exit_code" in loop.prompts[1]
    assert board["executor_state"] == "BLOCKED"
    assert loop.terminal_acceptance == {}


def test_unknown_side_effect_stops_without_replay(tmp_path, monkeypatch):
    replies = [
        tool_json("create_directory", path=TARGET),
        tool_json("create_directory", path=TARGET),
    ]
    loop, _ = _loop(tmp_path, monkeypatch, replies)
    def unknown(action):
        loop.executed.append(action)
        return _result(action, status="UNKNOWN")
    loop._execute_action = unknown
    result = run_minimal_core(loop, REQUEST)
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert len(loop.executed) == 1
    assert board["executor_state"] == "EXECUTION_OUTCOME_UNKNOWN"
    assert board["terminal"]["outcome"] == "UNKNOWN"
    assert loop.terminal_acceptance == {}
    assert "禁止重送" in result


def test_true_pokeclaw_finish_without_any_tool_fails_closed(
    tmp_path, monkeypatch
):
    request = "請確認文件是否存在 E:\\example\\config.txt"
    loop, state = _loop(
        tmp_path, monkeypatch,
        ['{"type":"finish","summary":"應該存在"}'],
        request=request,
    )
    result = run_minimal_core(loop, request)
    assert state["mutations"] == 0
    assert not loop.executed
    assert loop.terminal_outcome == "FAILED"
    assert loop.terminal_acceptance["final_verdict"] == "FAIL"
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    assert board["executor_state"] == "MODEL_REPORTED_COMPLETE_UNVERIFIED"
    assert board["terminal"]["goal_verified"] is False
    assert board["terminal"]["finish_comparison"]["reason_code"] == "TOOL_EVIDENCE_REQUIRED"


@pytest.mark.skipif(os.name != "nt", reason="native Windows canonical path/Scope guard")
def test_live_windows_path_parser_and_real_directory_creation(tmp_path, monkeypatch):
    from WebAgent.tool_context import WebAgentToolContext
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    target = str(tmp_path / "Git33_Scope_Check")
    request = REQUEST.replace(TARGET, target)
    assert extract_authorized_paths(request) == [target]
    context = WebAgentToolContext(tmp_path)
    context.begin_run("RR-MINIMAL-TEST", request, [target])
    loop = FakeLoop(tmp_path, request, [
        tool_json("observe_fact", path=target),
        tool_json("create_directory", path=target),
        tool_json("list_directory", path=target),
        '{"type":"finish","summary":"目錄確實已建立，並重新列出。"}',
    ], "")
    loop.executor_only_mode = True
    loop.tools = context
    loop._execute_action = context.execute
    result = run_minimal_core(loop, request)
    assert (tmp_path / "Git33_Scope_Check").is_dir()
    assert len(loop.action_result_ledger) == 3
    assert terminal_verdict_from_loop(loop).state == "COMPLETED"
    assert "MIN-EV-" in result
