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

def test_executor_only_actual_planner_loop_recovers_failed_p0_then_freezes_p0_p2(
    tmp_path, monkeypatch,
):
    """Production Executor-only loop, actual ledger+MIN-EV with simulated tool.

    A false exit-code green at P0 and a premature Planner BLOCKED must cause
    recovery, not a terminal SUCCESS. Earlier verified stages never replay.
    """
    import json
    from agent_core.executor_run_blackboard import read_executor_run
    from agent_core.tool_result_contract import encode_result
    from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json

    monkeypatch.setattr(
        "agent_core.task_progress.set_runtime_state", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **k: {"decision": "NORMAL",
                         "reason_code": "TEST_ACL_OFF"},
    )
    request = (
        "請確實執行三階段驗證：P0 Git 狀態查詢，"
        "P1 Git 分支資訊，P2 Git HEAD。"
        "每個階段有獨立真實證據，失敗要改方法且不可假報成功。"
    )
    loop = FakeLoop(tmp_path, request, [], "")
    loop.executor_only_mode = True
    loop.max_turns = 30
    phases = [
        {"stage_id": f"P{i}", "description": f"Original phase P{i}",
         "depends_on": [f"P{i-1}"] if i else [],
         "acceptance": [
             {"field": "data.fixture_phase",
              "operator": "eq", "expected": f"P{i}"},
             {"field": "data.verification_status",
              "operator": "eq", "expected": "PASS"},
         ]}
        for i in range(3)
    ]
    commands = [
        ("git status --short", "P0", "FAIL"),
        ("git log -1 --oneline", "P0", "PASS"),
        ("git branch --show-current", "P1", "PASS"),
        ("git rev-parse HEAD", "P2", "PASS"),
    ]
    sequence = {"turn": 0}

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        sequence["turn"] += 1
        t = sequence["turn"]
        if t == 1:
            return json.dumps({"type": "stage_plan", "stages": phases})
        if t == 2:
            return tool_json(
                "run_command", command=commands[0][0], operation="GIT_INSPECT")
        if 3 <= t <= 12:
            return json.dumps({"type": "blocked", "reason": "P0 first failed"})
        if t >= 13:
            t -= 9
        if t in (4, 6, 8):
            command = {4: commands[1][0], 6: commands[2][0],
                       8: commands[3][0]}[t]
            return tool_json(
                "run_command", command=command, operation="GIT_INSPECT")
        if t in (5, 7, 9):
            target = {5: "P0", 7: "P1", 9: "P2"}[t]
            ref = list(loop.action_result_ledger.values())[-1]["result_id"]
            return json.dumps({
                "type": "stage_commit", "stage_id": target,
                "evidence_refs": [ref],
            })
        if t == 10:
            ref = list(loop.action_result_ledger.values())[-1]["result_id"]
            return json.dumps({
                "type": "finish", "contract_version": 2,
                "summary": "P0～P2 的觀測與驗證已完成",
                "goal_checks": [{
                    "criterion": "P2 的 Git 根目錄驗證",
                    "evidence_ref": ref, "field": "data.fixture_phase",
                    "operator": "eq", "expected": "P2",
                }],
            }, ensure_ascii=False)
        board = read_executor_run(
            tmp_path, task_id=loop.task_id,
            request_id=loop.run_id, task_epoch=loop.task_epoch,
        ) or {}
        markers = [
            (row.get("kind"), row.get("comparator_reason_code"),
             row.get("reason_code"), row.get("stage_id"))
            for row in board.get("events", ())
            if "FINISH" in str(row.get("kind"))
               or "GOAL75" in str(row.get("kind"))
               or row.get("kind") == "ADMISSION_REJECTED"
        ]
        raise AssertionError(f"Unexpected Planner round {t}; events={markers[-16:]}; repair={prompt.split('Runtime repair information:')[-1][-1000:]}")

    def execute(action):
        loop.executed.append(dict(action))
        command = action.get("command")
        matches = [entry for entry in commands if entry[0] == command]
        assert len(matches) == 1, command
        _, phase, verification = matches[0]
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command",
            executed=True, data={
                "exit_code": 0, "process_started": True,
                "process_state": "EXITED",
                "verification_status": verification,
                "fixture_phase": phase,
                "stdout": "test result", "stderr": "",
                "timed_out": False,
            },
        )

    loop.planner = planner
    loop._execute_action = execute
    answer = exe.run_pokeclaw_executor(loop, request)
    ledger = _ledger(tmp_path, goal=request)  # Override FakeLoop task IDs.
    ledger = GoalStageLedger(
        tmp_path, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch, goal=request,
    )
    state = ledger.read()
    assert state is not None, answer
    assert [stage["status"] for stage in state["stages"]] == [
        "VERIFIED_COMMITTED",
    ] * 3
    assert len(loop.executed) == 4
    assert [e["command"] for e in loop.executed] == [
        value[0] for value in commands
    ]
    assert state["stages"][0]["strategy_revision"] >= 10
    assert len(state["stages"][0]["observations"]) == 2
    board = read_executor_run(
        tmp_path, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch,
    )
    assert any(row["kind"] == "GOAL75_STAGE_LOCAL_RECOVERY"
               for row in board["events"])
    assert sum(row["kind"] == "GOAL75_STAGE_VERIFIED_COMMITTED"
               for row in board["events"]) == 3
    assert loop.terminal_outcome == "SUCCESS", answer
    assert loop.terminal_acceptance["final_verdict"] == "PASS"
