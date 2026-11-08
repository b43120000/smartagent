"""Git32: isolated Executor completes check/create/type/Blackboard without parents."""
from __future__ import annotations

import json
import os

import pytest

from agent_core.evidence_foundation import observation
from agent_core.tool_result_contract import encode_result, attach_runtime_result
from agent_core.executor_blackboard import read_executor_terminal
from agent_core.tri_one_runtime_terminal import terminal_verdict_from_loop
from WebAgent.executor_only_validation import (
    directory_sequence_contract, sequence_progress, next_action_gate,
)
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json


TARGET = r"E:\SmartAgent_TriLayer_Blackboard_Test_20261109"
REQUEST = (
    "請在 " + TARGET + " 執行三層 Blackboard 整合驗證：\n"
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


def _fact(action, exists):
    return attach_runtime_result(
        "{}", "SUCCEEDED", "FACT_OBSERVED",
        tool="observe_fact", executed=True,
        requested_path=action["path"], resolved_path=action["path"],
        action_id=action["action_id"],
        data={
            "observations": [observation(
                "file.exists", exists, source="filesystem.stat",
                scope=action["path"], raw_ref=action["action_id"],
            )],
            "observation_status": "CONFIRMED",
            "terminal_eligible": True,
        },
    )


def _created(action):
    return encode_result(
        "SUCCEEDED", "DIRECTORY_CREATED",
        tool="create_directory", executed=True,
        requested_path=action["path"], resolved_path=action["path"],
        data={
            "directory_created": True,
            "observed_exists": True, "observed_is_directory": True,
            "observation_status": "CONFIRMED", "no_parent_directories_created": True,
        },
    )


def _listed(action):
    return encode_result(
        "SUCCEEDED", "DIRECTORY_LISTED",
        tool="list_directory", executed=True,
        requested_path=action["path"], resolved_path=action["path"],
        data={"entries": [], "terminal_eligible": True},
    )


def _inspected(action, exists):
    return encode_result(
        "SUCCEEDED",
        "DIRECTORY_INSPECTED" if exists else "DIRECTORY_ABSENCE_OBSERVED",
        tool="inspect_directory", executed=True,
        requested_path=action["paths"][0], resolved_path=action["paths"][0],
        data={
            "observed_exists": exists, "observed_is_directory": exists,
            "observed_path": action["paths"][0],
            "observation_status": "CONFIRMED",
            "terminal_eligible": bool(exists),
        },
    )


def _run(tmp_path, monkeypatch, steps, *, initial_exists=False, goal=REQUEST):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    replies = [tool_json(name, **args) for name, args in steps]
    loop = FakeLoop(tmp_path, goal, replies, "")
    loop.executor_only_mode = True
    state = {"exists": initial_exists, "created": 0}

    def execute(action):
        loop.executed.append(dict(action))
        tool = action["tool"]
        if tool == "observe_fact":
            return _fact(action, state["exists"])
        if tool == "inspect_directory":
            return _inspected(action, state["exists"])
        if tool == "create_directory":
            assert not state["exists"]  # Actual executed mutation must never replay
            state["created"] += 1
            state["exists"] = True
            return _created(action)
        if tool == "list_directory":
            assert state["exists"], "Cannot list an absent directory"
            return _listed(action)
        raise AssertionError("Tool outside explicit Executor-only task")
    loop._execute_action = execute
    return loop, state, run_minimal_core(loop, goal)


@pytest.mark.parametrize("initial_tool", ["observe_fact", "inspect_directory"])
@pytest.mark.parametrize("final_tool", ["list_directory", "inspect_directory"])
def test_missing_directory_conditional_create_and_independent_recheck(
    tmp_path, monkeypatch, initial_tool, final_tool
):
    first = ((initial_tool, {"path": TARGET}) if initial_tool == "observe_fact"
             else (initial_tool, {"paths": [TARGET], "recursive": False}))
    last = ((final_tool, {"path": TARGET}) if final_tool == "list_directory"
            else (final_tool, {"paths": [TARGET], "recursive": False}))
    loop, state, reply = _run(tmp_path, monkeypatch, [
        first,
        ("create_directory", {"path": TARGET}),
        last,
    ])
    assert state["created"] == 1
    assert len(loop.executed) == 3
    assert len(loop.expected_protocols) == 3
    assert "Executor-only 整合驗證完成" in reply
    assert "本次已建立" in reply
    assert "MIN-EV-" in reply and "HOF-" in reply
    assert "DISABLED_EXECUTOR_ONLY" in reply
    assert loop.terminal_outcome == "SUCCESS"
    assert terminal_verdict_from_loop(loop).state == "COMPLETED"
    assert loop.terminal_acceptance["matched_condition_id"] == "EXECUTOR_ONLY_DIRECTORY_SEQUENCE"
    record = read_executor_terminal(
        tmp_path, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    assert record["terminal_status"] == "VERIFIED"
    assert record["goal_verified"] is True
    assert record["orchestration_mode"] == "EXECUTOR_ONLY"
    assert len(record["verification_action_refs"]) == 3
    assert len(record["tool_evidence_refs"]) == 3
    assert all(ref.startswith("MIN-EV-") for ref in record["tool_evidence_refs"])
    assert record["terminal_receipt_id"] == loop.terminal_acceptance["receipt_id"]
    handoff = record["tri_layer_handoff"]
    assert handoff["handoff_id"]
    assert handoff["parent_agents_enabled"] is False
    assert handoff["parent_dispatch_state"] == "NOT_DISPATCHED"
    assert handoff["next_parent_owner"] == ""
    assert handoff["prerequisite_handoff"]["prerequisite_handoff_state"] == "DISABLED_EXECUTOR_ONLY"
    assert handoff["prerequisite_handoff"]["prerequisite_readiness_verdict"] == "NOT_EVALUATED"
    assert handoff["goal_stage_handoff"]["goal_stage_handoff_state"] == "DISABLED_EXECUTOR_ONLY"
    assert handoff["goal_stage_handoff"]["goal_stage_verification_verdict"] == "NOT_EVALUATED"
    assert handoff["stage_transition_committed"] is False


@pytest.mark.parametrize("final_tool", ["list_directory", "inspect_directory"])
def test_existing_directory_never_recreated(tmp_path, monkeypatch, final_tool):
    steps = [
        ("observe_fact", {"path": TARGET}),
        ((final_tool, {"path": TARGET}) if final_tool == "list_directory"
         else (final_tool, {"paths": [TARGET], "recursive": False})),
    ]
    loop, state, answer = _run(
        tmp_path, monkeypatch, steps, initial_exists=True,
    )
    assert state["created"] == 0
    assert all(x["tool"] != "create_directory" for x in loop.executed)
    assert "未重複建立" in answer
    assert loop.terminal_outcome == "SUCCESS"
    receipt = read_executor_terminal(tmp_path, task_id=loop.task_id)
    assert len(receipt["verification_action_refs"]) == 2
    assert receipt["terminal_status"] == "VERIFIED"


def test_execute_first_without_observation_is_denied_before_tool(tmp_path, monkeypatch):
    loop, state, answer = _run(tmp_path, monkeypatch, [
        ("create_directory", {"path": TARGET}),
        ("create_directory", {"path": TARGET}),
    ])
    assert state["created"] == 0
    assert not loop.executed
    assert loop.executor_result["terminal_status"] == "BLOCKED"
    assert loop.terminal_outcome == "UNKNOWN"
    assert loop.terminal_acceptance == {}
    record = read_executor_terminal(tmp_path, task_id=loop.task_id)
    assert record["terminal_status"] == "BLOCKED"
    assert record["last_admission_rejection"]["reason_code"].startswith("EXECUTOR_ONLY_")


def test_unsupported_extra_steps_do_not_promote_to_sequence():
    changed = REQUEST.replace("6. 回報", "6. 回報") + "\n7. 再刪除資料夾"
    assert directory_sequence_contract(changed) == {}
    another = REQUEST + "\n請檢查 " + r"E:\Another"
    assert directory_sequence_contract(another) == {}


def test_phase_requires_independent_type_verification():
    c = directory_sequence_contract(REQUEST)
    assert c and c["authorization_granted"] is False
    no_history = sequence_progress(c, [])
    assert no_history["phase"] == "INITIAL_CHECK"
    action = {"tool": "create_directory", "path": TARGET}
    assert "OUT_OF_ORDER" in next_action_gate(c, [], action)
    assert next_action_gate(c, [], {
        "tool": "observe_fact", "path": r"E:\Another",
    }) == "EXECUTOR_ONLY_PATH_NOT_EXACT_TARGET"


def test_goal_driver_context_is_never_called_in_executor_only_mode(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Goal Driver must not run in Executor-only mode")
    monkeypatch.setattr("WebAgent.minimal_core_loop.build_goal_driver_context", forbidden)
    loop, state, answer = _run(tmp_path, monkeypatch, [
        ("observe_fact", {"path": TARGET}),
        ("create_directory", {"path": TARGET}),
        ("list_directory", {"path": TARGET}),
    ])
    assert state["created"] == 1
    assert loop.executor_result["terminal_status"] == "VERIFIED"


def test_unverified_directory_positive_stat_alone_never_completes(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    loop = FakeLoop(tmp_path, REQUEST, [], "")
    loop.executor_only_mode = True
    contract = directory_sequence_contract(REQUEST)
    action = {"tool": "observe_fact", "path": TARGET, "action_id": "A-1"}
    from agent_core.tool_result_contract import decode_result
    row = {"action": action, "receipt": decode_result(_fact(action, True))}
    assert sequence_progress(contract, [row])["phase"] == "TYPE_VERIFY_EXISTING"


@pytest.mark.skipif(os.name != "nt", reason="Live Windows Scope/ToolContext integration")
def test_windows_real_tool_admission_create_and_independent_verify(
    tmp_path, monkeypatch
):
    from WebAgent.tool_context import WebAgentToolContext
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **kw: None)
    target = str(tmp_path / "Git32_Executor_Only")
    request = REQUEST.replace(TARGET, target)
    ctx = WebAgentToolContext(tmp_path)
    ctx.begin_run("RR-MINIMAL-TEST", request, [target])
    loop = FakeLoop(tmp_path, request, [
        tool_json("observe_fact", path=target),
        tool_json("create_directory", path=target),
        tool_json("list_directory", path=target),
    ], "")
    loop.executor_only_mode = True
    loop.tools = ctx
    loop._execute_action = ctx.execute
    reply = run_minimal_core(loop, request)
    assert "整合驗證完成" in reply
    assert (tmp_path / "Git32_Executor_Only").is_dir()
    assert terminal_verdict_from_loop(loop).is_complete
