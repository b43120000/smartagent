"""Git29: exact requested Windows target + separate parent-layer states."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from WebAgent.minimal_decision import MinimalDecision
from WebAgent.minimal_admission import (
    canonicalize_exact_target_inspection, admit_minimal_action,
)
from agent_core.executor_blackboard import (
    publish_executor_terminal, read_executor_terminal,
    acknowledge_prerequisite_handoff, acknowledge_goal_stage_handoff,
)
from agent_core.tri_layer_handoff import classify_parent_route, project_handoff
from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json
from WebAgent.minimal_core_loop import run_minimal_core


def _tools(target):
    class Tools:
        interface_name = "webdirect"
        current_request_text = "請在 " + target + " 檢查資料夾是否存在，再決定如何建立。"
        _authorized_local_paths = [target]
    return Tools()


@pytest.mark.parametrize("proposed", [
    "E:\\", r"E:\\SmartAgent_Tests", r"E:\\SmartAgent_Tests\\Exact",
])
def test_exact_target_read_narrowing_from_ancestor_or_equal(proposed):
    target = r"E:\\SmartAgent_Tests\\Exact"
    d = MinimalDecision("tool_call", "inspect_directory", {
        "paths": [proposed],
    })
    amended, receipt = canonicalize_exact_target_inspection(d, _tools(target))
    assert amended.arguments == {"paths": [target], "recursive": False}
    assert receipt["rewrite_reason_code"] == "EXACT_REQUEST_TARGET_READ_SCOPE_NARROWED"
    assert receipt["scope_expanded"] is False
    assert receipt["authorizes_action"] is False
    assert d.arguments["paths"] == [proposed]  # Provenance untouched.


@pytest.mark.parametrize("proposed", [
    r"E:\\SmartAgent_Other", r"F:\\SmartAgent_Tests",
    r"E:\\SmartAgent_Tests\\Exact\\sub",
])
def test_exact_target_read_does_not_rewrite_siblings_or_descendants(proposed):
    target = r"E:\\SmartAgent_Tests\\Exact"
    d = MinimalDecision("tool_call", "inspect_directory", {"paths": [proposed]})
    amended, receipt = canonicalize_exact_target_inspection(d, _tools(target))
    assert amended is d
    assert receipt == {}


def test_scope_narrowing_rejects_multi_path_or_unbound_goal():
    target = r"E:\\SmartAgent_Tests\\Exact"
    d = MinimalDecision("tool_call", "inspect_directory", {
        "paths": ["E:\\", r"F:\\"],
    })
    assert canonicalize_exact_target_inspection(d, _tools(target))[0] is d
    tools = _tools(target)
    tools.current_request_text = "只需要看另外一個檔案"
    one = MinimalDecision("tool_call", "inspect_directory", {
        "paths": ["E:\\"],
    })
    assert canonicalize_exact_target_inspection(one, tools)[0] is one


def test_unrelated_mutations_are_never_rewritten():
    target = r"E:\\SmartAgent_Tests\\Exact"
    d = MinimalDecision("tool_call", "create_directory", {
        "path": r"E:\\",
    })
    assert canonicalize_exact_target_inspection(d, _tools(target))[0] is d


def test_git29_main_executor_stores_original_and_narrowed_scope(tmp_path, monkeypatch):
    from agent_core.tool_result_contract import encode_result
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    target = r"E:\\SmartAgent_Tests\\Exact"
    goal = "請先檢查 " + target + " 是否存在，然後再判斷如何建立。"
    loop = FakeLoop(tmp_path, goal, [
        tool_json("inspect_directory", paths=["E:\\"]),
        '{"type":"blocked","reason":"Need further decision"}',
    ], encode_result(
        "SUCCEEDED", "DIRECTORY_ABSENCE_OBSERVED", tool="inspect_directory",
        executed=True, requested_path=target, resolved_path=target,
        data={"observation_status": "CONFIRMED", "observed_exists": False,
              "observed_path": target},
    ))
    loop.tools._authorized_local_paths = [target]
    loop.tools.current_request_text = goal
    run_minimal_core(loop, goal)
    assert len(loop.executed) >= 1
    action = loop.executed[0]
    assert action["paths"] == [target]
    assert action["recursive"] is False
    assert loop._planner_original_actions[action["action_id"]]["paths"] == ["E:\\"]
    assert (loop._runtime_action_rewrite_notes[action["action_id"]]
            ["rewrite_reason_code"] == "EXACT_REQUEST_TARGET_READ_SCOPE_NARROWED")


@pytest.mark.skipif(os.name != "nt", reason="Native Windows security scope check")
def test_real_windows_exact_target_scope_narrowing(tmp_path):
    from WebAgent.tool_context import WebAgentToolContext
    target = str(tmp_path / "Git29_Exact_Target_Never_Probed")
    ctx = WebAgentToolContext(tmp_path)
    ctx.begin_run("RR-GIT29", "請檢查 " + target + " 是否存在", [target])
    decision = MinimalDecision("tool_call", "inspect_directory", {
        "paths": [str(tmp_path)], "recursive": True,
    })
    before = admit_minimal_action(decision, ctx)
    assert before[0] is None and "scope_rejected" in before[1]
    amended, _ = canonicalize_exact_target_inspection(decision, ctx)
    after, rejection = admit_minimal_action(amended, ctx)
    assert not rejection
    assert after["paths"] == [target]
    assert after["recursive"] is False


def _blocked_loop():
    return SimpleNamespace(
        task_id="TASK-GIT29-EXECUTOR", run_id="RR-GIT29",
        task_epoch="EPOCH-GIT29", terminal_acceptance={},
        atomic_task_contract={
            "schema": "SMARTAGENT_ATOMIC_TASK_V1",
            "task_id": "TASK-GIT29-EXECUTOR",
            "parent_task_id": "TASK-GIT29-PARENT",
            "parent_task_epoch": "PARENT-EPOCH-GIT29",
            "stage_id": "STAGE-ENV",
        },
    )


def test_handoff_independent_status_and_owner_bound_acknowledgements(tmp_path):
    loop = _blocked_loop()
    result = {
        "outcome": "BLOCKED",
        "reason_code": "SCOPE_REJECTED:resolved_scope_expands_beyond_explicit_path",
        "goal_verified": False,
        "tool_status": "NOT_EXECUTED",
        "model_round": 2, "action_id": "", "evidence_ref": "",
    }
    record = publish_executor_terminal(loop, result, root=tmp_path, goal="Create folder")
    handoff = project_handoff(record)
    assert handoff["executor_handoff"]["executor_terminal_status"] == "BLOCKED"
    assert (handoff["prerequisite_handoff"]["prerequisite_handoff_state"]
            == "REPLAN_REQUIRED")
    assert (handoff["prerequisite_handoff"]["prerequisite_issue_code"]
            == "SCOPE_ADMISSION_REVIEW")
    assert (handoff["goal_stage_handoff"]["goal_stage_handoff_state"]
            == "WAITING_FOR_PREREQUISITE")
    assert handoff["stage_transition_committed"] is False
    assert handoff["next_parent_owner"] == "PREREQUISITE"
    assert all("status" not in handoff[k] for k in
               ("executor_handoff", "prerequisite_handoff", "goal_stage_handoff"))
    assert read_executor_terminal(tmp_path, task_id=loop.task_id,
                                  task_epoch="OTHER") is None
    with pytest.raises(ValueError, match="identity_mismatch"):
        acknowledge_prerequisite_handoff(
            tmp_path, executor_task_id=loop.task_id,
            parent_task_id="TASK-OTHER", parent_task_epoch="PARENT-EPOCH-GIT29",
            readiness_verdict="READY", evidence_refs=["EV-READINESS"],
        )
    record = acknowledge_prerequisite_handoff(
        tmp_path, executor_task_id=loop.task_id,
        parent_task_id="TASK-GIT29-PARENT",
        parent_task_epoch="PARENT-EPOCH-GIT29",
        readiness_verdict="READY", evidence_refs=["EV-READINESS"],
    )
    h = project_handoff(record)
    assert h["prerequisite_handoff"]["prerequisite_readiness_verdict"] == "READY"
    assert h["goal_stage_handoff"]["goal_stage_verification_verdict"] == "NOT_EVALUATED"
    assert h["executor_handoff"]["executor_terminal_status"] == "BLOCKED"
    record = acknowledge_goal_stage_handoff(
        tmp_path, executor_task_id=loop.task_id,
        parent_task_id="TASK-GIT29-PARENT",
        parent_task_epoch="PARENT-EPOCH-GIT29",
        stage_id="STAGE-ENV", stage_verdict="NEEDS_RECHECK",
        evidence_refs=["EV-GOAL-RECHECK"],
    )
    h = project_handoff(record)
    assert h["goal_stage_handoff"]["goal_stage_verification_verdict"] == "NEEDS_RECHECK"
    assert h["stage_transition_committed"] is False
    assert record["advances_parent_stage"] is False


def test_handoff_route_distinguishes_scope_permission_and_verified():
    x = classify_parent_route("BLOCKED", "scope_rejected:expanded")
    assert x["prerequisite_handoff_state"] == "REPLAN_REQUIRED"
    x = classify_parent_route("BLOCKED", "PERMISSION_DENIED")
    assert x["prerequisite_handoff_state"] == "AWAITING_AUTHORIZATION"
    x = classify_parent_route("NEEDS_PREREQUISITE", "PARENT_MISSING")
    assert x["next_owner"] == "PREREQUISITE"
    x = classify_parent_route("VERIFIED", "DIRECTORY_CREATED")
    assert x["goal_stage_handoff_state"] == "AWAITING_STAGE_VERIFICATION"


def test_paused_egress_carries_distinct_layer_receipts():
    worker = (Path(__file__).resolve().parents[2] /
              "RemoteAgent" / "telegram_webagent_worker.py").read_text(encoding="utf-8")
    assert '"tri_layer_handoff": layer_handoff' in worker
    assert '"prerequisite_handoff": dict(' in worker
    assert '"goal_stage_handoff": dict(' in worker
