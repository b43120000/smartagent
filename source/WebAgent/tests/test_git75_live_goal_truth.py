"""Git75 Live RCA regression: an uncompleted compound Goal NEVER turns green.

The exact 74.1 final comparator remains the source of Runtime ToolResult
truth. The new Stage gate only prevents premature SUCCESS and requests
Planner recovery; it does not grant tool permissions.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.goal75_goal_coverage import (
    original_goal_stage_ids, requires_stage_control,
    validate_stage_plan_coverage, final_stage_coverage,
)
from agent_core.goal75_stage_ledger import GoalStageLedger
from WebAgent import pokeclaw_executor_loop as exe


GOAL = "請執行 Universal Live E2E。\nP0 環境。\nP1 PowerShell。\nP2 CMD。\n" + (
    "P3 DIRECT。 P4 Negative Matching。 P5 Evidence。 P6 Truncation。 "
    "P7 Timeout。 P8 Recovery。 P9 全 Goal Acceptance。"
)


def _stages(n=10):
    return [
        {"stage_id": f"P{i}", "description": f"Original phase P{i}",
         "depends_on": [f"P{i-1}"] if i else [],
         "acceptance": [
             {"field": "data.verification_status",
              "operator": "eq", "expected": "PASS"},
         ]}
        for i in range(n)
    ]


def _ledger(root, goal=GOAL):
    return GoalStageLedger(
        root, task_id="TASK-G75-LIVE-RCA",
        request_id="RR-G75-LIVE-RCA",
        task_epoch="EPOCH-G75-LIVE-RCA", goal=goal,
    )


def _receipt(verdict):
    return {
        "status": "SUCCEEDED", "executed": True,
        "data": {"verification_status": verdict, "exit_code": 0,
                 "process_started": True},
    }


def test_original_p0_p9_is_independent_of_planner_shortcut():
    assert original_goal_stage_ids(GOAL) == tuple(f"P{i}" for i in range(10))
    assert original_goal_stage_ids("測試 P0–P9 全階段") == tuple(
        f"P{i}" for i in range(10))
    assert requires_stage_control(GOAL, goal_state_contract={"status": "UNCOMPILED"})
    only_one = [_stages()[0]]
    coverage = validate_stage_plan_coverage(GOAL, only_one)
    assert coverage["status"] == "REJECT"
    assert "P9" in coverage["missing_stage_ids"]
    assert validate_stage_plan_coverage(GOAL, _stages())["status"] == "PASS"


def test_exit_zero_with_verification_fail_never_commits(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.create(_stages())
    ledger.record_action(
        "P0", action_id="ACT-G75-FAIL",
        evidence_ref="MIN-EV-" + "0" * 24,
        receipt=_receipt("FAIL"), expected_match=False,
    )
    assert ledger.current()["stage_id"] == "P0"
    with pytest.raises(ValueError, match="acceptance_unverified"):
        ledger.commit("P0", evidence_refs=["MIN-EV-" + "0" * 24])
    assert final_stage_coverage(
        GOAL, ledger.read(), control_required=True)["status"] == "NOT_VERIFIED"
    ledger.recover("P0", reason_code="ASSERTION_MISMATCH",
                   strategy_id="ALTERNATIVE_TOOL")
    ledger.record_action(
        "P0", action_id="ACT-G75-RECOVERED",
        evidence_ref="MIN-EV-" + "1" * 24,
        receipt=_receipt("PASS"), expected_match=True,
    )
    ledger.commit("P0", evidence_refs=["MIN-EV-" + "1" * 24])
    assert ledger.current()["stage_id"] == "P1"
    assert final_stage_coverage(
        GOAL, ledger.read(), control_required=True)["status"] == "NOT_VERIFIED"


def test_weak_stage_predicates_rejected():
    weak = _stages()
    weak[2]["acceptance"] = [
        {"field": "data.exit_code", "operator": "eq", "expected": 0}
    ]
    r = validate_stage_plan_coverage(GOAL, weak)
    assert r["status"] == "REJECT"
    assert r["weak_stage_ids"] == ["P2"]


def test_uncompiled_small_goal_keeps_git74_1_path():
    assert not requires_stage_control(
        "請查看資料夾是否存在", goal_state_contract={"status": "UNCOMPILED"})
    assert not requires_stage_control(
        GOAL, answer_only_eligible=True)


def test_executed_comparator_pass_does_not_green_without_stage_coverage(
    tmp_path, monkeypatch,
):
    """Actual executor-only _finish with simulated immutable comparator PASS.

    Reproduces GAR: one real Git query exists, while nine original requested
    stages are unverified. Must be FAILED, not task_outcome=SUCCESS.
    """
    board = {
        "goal_state_contract": {"status": "UNCOMPILED"},
        "evidence_requirement": {"answer_only_eligible": False},
        "events": [], "executor_state": "FINISHED",
        "handoff_id": "HO-G75-RCA",
        "terminal": {"tool_evidence_refs": ["MIN-EV-" + "a" * 24],
                     "finish_comparison": {}},
    }
    comparison = {
        "verdict": "PASS", "failure_origin": "NONE",
        "reason_code": "MODEL_RUNTIME_AND_GOAL_MATCH",
        "goal_evidence_reason": "MODEL_DECLARED_GOAL_CHECKS_CONFIRMED",
        "goal_kind": "MODEL_GENERATED_GOAL_CHECKS",
        "coverage_basis": "MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH",
        "evidence_refs": ["MIN-EV-" + "a" * 24],
        "mismatches": [], "tool_results": [],
    }
    events = []
    monkeypatch.setattr(exe, "finish_executor_run",
                        lambda *a, **k: board)
    monkeypatch.setattr(exe, "record_finish_comparison",
                        lambda *a, **k: board)
    monkeypatch.setattr(exe, "append_executor_event",
                        lambda *a, **k: events.append(a[2] if len(a) > 2 else k))
    monkeypatch.setattr(exe, "compare_runtime_finish",
                        lambda *a, **k: comparison)
    monkeypatch.setattr(exe, "set_runtime_state", lambda *a, **k: None)
    monkeypatch.setattr(exe, "projection", lambda _b: {
        "executor_state": "FINISHED", "goal_driver_state": "DISABLED",
        "prerequisite_state": "DISABLED", "handoff_id": "HO-G75-RCA",
        "tool_evidence_refs": ["MIN-EV-" + "a" * 24],
        "parent_dispatch_state": "DELIVERED",
        "parent_consumption_state": "CONSUMED",
    })
    loop = SimpleNamespace(
        task_id="TASK-G75-LIVE-RCA", run_id="RR-G75-LIVE-RCA",
        task_epoch="EPOCH-G75-LIVE-RCA",
        task_request=GOAL, progress_root=tmp_path, turn_id=3,
        terminal_outcome="UNKNOWN", terminal_acceptance={},
        _emit=lambda *a, **k: None,
    )
    reply = exe._finish(
        loop, tmp_path, GOAL, kind="MODEL_REPORTED_COMPLETE_UNVERIFIED",
        summary="Git HEAD command completed", complete=True,
        expected_runtime=None, goal_checks=[],
    )
    assert loop.terminal_outcome == "FAILED"
    assert loop.terminal_acceptance["verification_status"] == "FAIL"
    assert loop.executor_result["goal_verified"] is False
    assert any(e["kind"] == "GOAL75_TERMINAL_COVERAGE_GATE" for e in events)
    assert "GOAL75_ORIGINAL_GOAL_COVERAGE_NOT_VERIFIED" in reply


def test_all_real_stages_committed_removes_additional_gate(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.create(_stages())
    for i in range(10):
        sid = f"P{i}"
        ref = "MIN-EV-" + f"{i:024x}"
        ledger.record_action(
            sid, action_id=f"ACT-{i}", evidence_ref=ref,
            receipt=_receipt("PASS"), expected_match=True,
        )
        ledger.commit(sid, evidence_refs=[ref])
    state = final_stage_coverage(
        GOAL, ledger.read(), control_required=True)
    assert state["status"] == "PASS"
    assert state["stage_count"] == 10
