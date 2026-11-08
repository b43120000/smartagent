"""Git28: bounded Unknown Resolution, no mutation replay, durable parent handoff."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_core.executor_blackboard import (
    TERMINAL, consume_executor_terminal, executor_child_results,
    project_executor_for_parent, publish_executor_terminal,
    read_executor_terminal,
)
from agent_core.tool_result_contract import attach_runtime_result, encode_result
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json


def _mock_resolution_loop(tmp_path, *, followup: str, inspect_result: str):
    target = r"E:\AgentResolutionFolder"
    goal = "在 " + target + " 建立資料夾"
    first = tool_json("create_directory", path=target)
    loop = FakeLoop(tmp_path, goal, [
        first, followup,
        '{"type":"blocked","reason":"No more authorized observations"}',
    ], "")
    def run(action):
        loop.executed.append(dict(action))
        if action["tool"] == "create_directory":
            return encode_result(
                "UNKNOWN", "DIRECTORY_CREATE_OUTCOME_UNCERTAIN",
                tool="create_directory", executed=True,
                requested_path=target,
                data={"possible_partial_execution": True},
            )
        return inspect_result
    loop._execute_action = run
    return goal, target, loop


def _directory_observation(target, *, present: bool, authorized: bool = True):
    row = {
        "path": target, "authorized": authorized, "exists": present,
        "errors": ([] if present else ["path_not_found"]),
        "inaccessible_count": 0,
    }
    body = json.dumps({"tool": "inspect_directory", "paths": [row]})
    return attach_runtime_result(
        body, "SUCCEEDED", "DIRECTORY_INSPECTED" if present else "DIRECTORY_ABSENCE_OBSERVED",
        tool="inspect_directory", executed=True,
        requested_path=target, resolved_path=target,
        data={"observation_status": "CONFIRMED", "terminal_eligible": present},
    )


def test_unknown_mkdir_resolves_by_single_trusted_read_without_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **k: None)
    target = r"E:\AgentResolutionFolder"
    goal, target, loop = _mock_resolution_loop(
        tmp_path, followup=tool_json(
            "inspect_directory", paths=[target], recursive=False,
        ),
        inspect_result=_directory_observation(target, present=True),
    )
    answer = run_minimal_core(loop, goal)
    assert loop.terminal_outcome == "SUCCESS", answer
    assert loop.executor_result["terminal_status"] == "VERIFIED"
    assert loop.executor_result["resolution_attempts"] == 1
    assert loop.executor_result["original_tool_status"] == "UNKNOWN"
    assert loop.executor_result["side_effect_outcome_confirmed"] is True
    assert [x["tool"] for x in loop.executed] == ["create_directory", "inspect_directory"]
    assert len(loop.expected_protocols) == 2   # original + child resolution
    assert loop.executor_blackboard_terminal["terminal_status"] == "VERIFIED"
    assert loop.executor_blackboard_terminal["parent_consumption"]["status"] == "PENDING"
    record = read_executor_terminal(
        tmp_path, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch,
    )
    assert record["terminal_status"] == "VERIFIED"
    assert len(record["evidence_refs"]) == 1
    assert record["side_effect_may_have_occurred"] is True


def test_unknown_mkdir_resolution_scope_denial_is_blocked_not_failed(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **k: None)
    target = r"E:\AgentResolutionFolder"
    goal, _, loop = _mock_resolution_loop(
        tmp_path,
        followup=tool_json("inspect_directory", paths=[target], recursive=False),
        inspect_result=encode_result(
            "REJECTED", "ACCESS_DENIED", tool="inspect_directory", executed=False,
            requested_path=target,
        ),
    )
    run_minimal_core(loop, goal)
    assert len(loop.executed) == 2
    assert sum(x["tool"] == "create_directory" for x in loop.executed) == 1
    assert loop.executor_result["terminal_status"] == "BLOCKED"
    assert loop.executor_result["original_tool_status"] == "UNKNOWN"
    assert loop.executor_result["side_effect_may_have_occurred"] is True
    assert loop.executor_result["side_effect_outcome_confirmed"] is False
    assert "ACCESS_DENIED" in loop.executor_result["reason_code"]
    assert loop.executor_blackboard_terminal["terminal_status"] == "BLOCKED"
    assert loop.executor_blackboard_terminal["goal_verified"] is False


def test_unknown_mkdir_resolution_rejects_mutation_replay_even_if_planner_asks(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state", lambda *a, **k: None)
    target = r"E:\AgentResolutionFolder"
    goal, _, loop = _mock_resolution_loop(
        tmp_path,
        followup=tool_json("create_directory", path=target),
        inspect_result="",
    )
    run_minimal_core(loop, goal)
    assert [x["tool"] for x in loop.executed] == ["create_directory"]
    assert len(loop.expected_protocols) == 3  # original and two resolution attempts
    assert loop.executor_result["terminal_status"] == "BLOCKED"
    assert loop.executor_result["resolution_attempts"] == 2


def test_executor_blackboard_rejects_unknown_or_fabricated_success(tmp_path):
    loop = SimpleNamespace(
        task_id="TASK-CHILD-1", run_id="RR-CHILD-1", task_epoch="EPOCH-1",
        terminal_acceptance={"evidence_verdict": "PASS",
                             "evidence_refs": ["A-OBSERVED"]},
        atomic_task_contract={
            "schema": "SMARTAGENT_ATOMIC_TASK_V1", "task_id": "TASK-CHILD-1",
            "parent_task_id": "TASK-PARENT", "parent_task_epoch": "PE-1",
            "stage_id": "ENV_READY", "atomic_task_id": "ATOMIC-1",
        },
    )
    with pytest.raises(ValueError, match="nonterminal"):
        publish_executor_terminal(loop, {"outcome": "UNKNOWN"}, root=tmp_path, goal="goal")
    with pytest.raises(ValueError, match="unverified_success"):
        publish_executor_terminal(
            loop, {"outcome": "VERIFIED", "goal_verified": False},
            root=tmp_path, goal="goal", evidence_refs=["A-OBSERVED"],
        )
    proof = {
        "outcome": "VERIFIED", "goal_verified": True,
        "reason_code": "POSTCONDITION_VERIFIED",
        "original_tool_status": "UNKNOWN",
        "last_observed_tool_status": "SUCCEEDED",
        "side_effect_may_have_occurred": True,
        "side_effect_outcome_confirmed": True,
        "model_round": 2, "action_id": "A-ACTION",
    }
    record = publish_executor_terminal(
        loop, proof, root=tmp_path, goal="goal", evidence_refs=["A-OBSERVED"],
        resolution_attempts=1, resolution_state="EVIDENCE_VERIFIED",
    )
    assert record["terminal_status"] == "VERIFIED"
    assert record["parent_task_id"] == "TASK-PARENT"
    assert record["stage_id"] == "ENV_READY"
    assert record["advances_parent_stage"] is False
    assert publish_executor_terminal(
        loop, proof, root=tmp_path, goal="goal", evidence_refs=["A-OBSERVED"],
        resolution_attempts=1, resolution_state="EVIDENCE_VERIFIED",
    ) == record
    assert read_executor_terminal(tmp_path, task_id="TASK-CHILD-1",
                                  task_epoch="OTHER") is None
    children = executor_child_results(
        tmp_path, parent_task_id="TASK-PARENT", parent_task_epoch="PE-1",
    )
    assert [r["task_id"] for r in children] == ["TASK-CHILD-1"]
    assert executor_child_results(
        tmp_path, parent_task_id="TASK-PARENT", parent_task_epoch="WRONG",
    ) == []
    parent_view = project_executor_for_parent(children[0])
    assert parent_view["terminal_status"] == "VERIFIED"
    assert parent_view["parent_consumption"]["status"] == "PENDING"
    with pytest.raises(ValueError, match="parent_binding_mismatch"):
        consume_executor_terminal(
            tmp_path, child_task_id="TASK-CHILD-1", parent_task_id="TASK-OTHER",
            parent_task_epoch="PE-1", stage_id="ENV_READY",
            stage_verification="PASS", consumer_evidence_refs=["V-1"],
        )
    after = consume_executor_terminal(
        tmp_path, child_task_id="TASK-CHILD-1", parent_task_id="TASK-PARENT",
        parent_task_epoch="PE-1", stage_id="ENV_READY",
        stage_verification="PASS", consumer_evidence_refs=["V-1"],
    )
    assert after["parent_consumption"]["status"] == "CONSUMED"
    assert after["parent_consumption"]["stage_verification"] == "PASS"
    assert after["advances_parent_stage"] is False
    assert TERMINAL == {"VERIFIED", "BLOCKED", "NEEDS_PREREQUISITE"}


def test_blackboard_exposes_same_child_receipt_to_goal_and_prerequisite(tmp_path, monkeypatch):
    from WebAgent.minimal_goal_driver import build_goal_driver_context
    from agent_core import planner_context_snapshot
    # The canonical Snapshot owns projection; Goal and Prerequisite consume
    # the same entry without independent interpretation of Terminal Status.
    child = {"task_id": "TASK-CHILD", "terminal_status": "VERIFIED",
             "goal_verified": True, "evidence_refs": ["EV-1"],
             "advances_parent_stage": False}
    original = planner_context_snapshot.build_planner_context_snapshot
    monkeypatch.setattr(planner_context_snapshot, "build_planner_context_snapshot",
                        lambda runtime: {
                            "executor_blackboard": {
                                "task_result": {}, "child_results": [child],
                            },
                            "persistent_goal_gaps": {"active_count": 0, "gaps": []},
                            "goal_prerequisite_resolution": {},
                        })
    fake = SimpleNamespace(
        run_id="RR-TEST", task_id="TASK-PARENT", task_epoch="PE-1",
        action_result_ledger={}, action_ledger={}, tools=SimpleNamespace(),
        authorized_paths=[],
    )
    state = build_goal_driver_context(fake, "執行一個專案", [])
    assert state["executor_child_results"] == state["prerequisite_executor_results"]
    assert state["executor_child_results"][0]["terminal_status"] == "VERIFIED"
    assert state["executor_results_do_not_complete_parent_stage"] is True
    monkeypatch.setattr(planner_context_snapshot, "build_planner_context_snapshot", original)
