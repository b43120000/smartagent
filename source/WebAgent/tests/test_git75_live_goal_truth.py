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


def test_lean_goal_coverage_cannot_be_satisfied_by_one_fact():
    from agent_core.runtime_finish_comparator import _lean_missing_goal_phases
    ref = "MIN-EV-" + "a" * 24
    history = [{"evidence_ref": ref, "receipt": _receipt("PASS")}]
    checks = [{"criterion": f"P{i} original requirement", "evidence_ref": ref,
               "field": "data.verification_status", "operator": "eq", "expected": "PASS"}
              for i in range(10)]
    assert len(_lean_missing_goal_phases(GOAL, checks, history)) == 10


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

@pytest.mark.parametrize("recovery_kind,count", [
    ("blocked", 10), ("admission", 27), ("format", 13), ("finish", 5),
])
def test_lean_actual_loop_recovers_then_completes(tmp_path, monkeypatch, recovery_kind, count):
    import json
    from agent_core.executor_run_blackboard import read_executor_run
    from agent_core.tool_result_contract import encode_result
    from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json
    monkeypatch.setattr(exe, "set_runtime_state", lambda *a, **k: None)
    monkeypatch.setattr("agent_core.turbo_broker_admission.admit_action",
                        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"})
    request = "請確實執行 P0 Git 狀態、P1 Git 分支、P2 Git HEAD，保留證據。"
    loop = FakeLoop(tmp_path, request, [], "")
    loop.max_turns = 60
    commands = ["git status --short", "git branch --show-current", "git rev-parse HEAD"]
    def finish(checks=None):
        return json.dumps({"type": "finish", "summary": "P0 P1 P2 完成",
                           "goal_checks": checks})
    if recovery_kind == "blocked":
        retries = [json.dumps({"type":"blocked","reason":"schema issue"})] * count
    elif recovery_kind == "admission":
        retries = [tool_json("run_command", command=commands[0], operation="READ")] * count
    elif recovery_kind == "format":
        retries = ["invalid json"] * count
    else:
        retries = [finish()] * count
    steps = [tool_json("run_command", command=commands[0], operation="GIT_INSPECT")]
    steps += retries + [tool_json("run_command", command=cmd, operation="GIT_INSPECT")
                        for cmd in commands[1:]]
    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if steps:
            return steps.pop(0)
        refs = [entry["result_id"] for entry in loop.action_result_ledger.values()]
        assert len(refs) == 3
        return finish([{"criterion": f"P{i} actual evidence", "evidence_ref": ref,
                        "field": "data.fixture_phase", "operator": "eq", "expected": f"P{i}"}
                       for i, ref in enumerate(refs)])
    def execute(action):
        loop.executed.append(dict(action))
        i = commands.index(action["command"])
        return encode_result("SUCCEEDED", "COMMAND_EXITED", tool="run_command",
            executed=True, data={"exit_code":0,"process_started":True,"process_state":"EXITED",
                "verification_status":"PASS","fixture_phase":f"P{i}","stdout":f"P{i}",
                "stderr":"","timed_out":False})
    loop.planner, loop._execute_action = planner, execute
    answer = exe.run_pokeclaw_executor(loop, request)
    assert loop.terminal_outcome == "SUCCESS", answer
    assert len(loop.executed) == 3
    assert loop.turn_id == count + 4
    assert [a["command"] for a in loop.executed] == commands
    board = read_executor_run(tmp_path, task_id=loop.task_id, request_id=loop.run_id,
                              task_epoch=loop.task_epoch)
    assert board["terminal"]["goal_verified"]
    assert not (tmp_path / "localdata/runtime/goal75_stages").exists()


def test_lean_actual_loop_explores_existing_target_and_selects_next(tmp_path, monkeypatch):
    import json
    from agent_core.evidence_foundation import probe_file_exists
    from agent_core.tool_result_contract import attach_runtime_result
    from WebAgent.tests.test_minimal_core_v10 import FakeLoop, tool_json
    monkeypatch.setattr(exe, "set_runtime_state", lambda *a, **k: None)
    monkeypatch.setattr("agent_core.turbo_broker_admission.admit_action",
                        lambda *a, **k: {"decision":"NORMAL","reason_code":"TEST_ACL_OFF"})
    first, second = tmp_path / "Live_01", tmp_path / "Live_02"
    first.mkdir()
    sentinel = first / "preserve.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    goal = "P0 檢查 " + str(first) + "；P1 找到不存在的候選 " + str(second) + "；不可刪除。"
    loop = FakeLoop(tmp_path, goal, [], "")
    steps = [tool_json("observe_fact", path=str(first)), tool_json("observe_fact", path=str(second))]
    def planner(prompt, *_):
        if steps:
            return steps.pop(0)
        refs = [x["result_id"] for x in loop.action_result_ledger.values()]
        return json.dumps({"type":"finish","summary":"已觀測兩候選；未刪除",
            "goal_checks":[{"criterion":f"P{i} candidate fact","evidence_ref":ref,
                "field":"data.observations.0.value","operator":"eq","expected":i==0}
                for i,ref in enumerate(refs)]})
    def execute(action):
        loop.executed.append(dict(action))
        path=action["path"]
        from pathlib import Path
        fact=probe_file_exists(Path(path),raw_ref=action["action_id"])
        return attach_runtime_result("{}", "SUCCEEDED", "FACT_OBSERVED", tool="observe_fact",
            executed=True,requested_path=path,resolved_path=path,task_id=loop.task_id,
            request_id=loop.run_id,action_id=action["action_id"],data={
                "observations":[fact],"execution_finality":"COMPLETED",
                "observation_status":"CONFIRMED","terminal_eligible":True,
                "verification_status":"UNVERIFIED"})
    loop.planner, loop._execute_action=planner,execute
    answer=exe.run_pokeclaw_executor(loop,goal)
    assert loop.terminal_outcome=="SUCCESS",answer
    assert loop.turn_id==3 and len(loop.executed)==2
    assert sentinel.read_text(encoding="utf-8")=="unchanged" and not second.exists()


def test_lean_external_blocker_distinguishes_permission_and_schema():
    def board(reason, stage):
        return {"events":[{"kind":"ADMISSION_REJECTED","sequence":1,
            "admission_diagnostic":{"stage":stage},
            "runtimeEvidence":{"status":"REJECTED","reason_code":reason}}]}
    assert exe._lean_external_blocker(board("scope_rejected:PATH_NOT_AUTHORIZED","SCOPE_PREFLIGHT"))
    assert exe._lean_external_blocker(board("wrong_field_type","SCHEMA_VALIDATION")) is None
    assert exe._lean_external_blocker(board("PERMISSION_DENIED","SCHEMA_VALIDATION")) is None


def test_lean_read_uncertainty_is_not_mutation_uncertainty():
    from agent_core.runtime_finish_comparator import _execution_uncertainty
    item={"evidence_ref":"MIN-EV-"+"a"*24,"receipt":{"status":"UNKNOWN"},
          "action":{"tool":"observe_fact"},"mutating":False}
    blocked,recovered=_execution_uncertainty([item],lean_goal=True)
    assert not blocked and recovered[0]["reason_code"]=="READ_ATTEMPT_NOT_GOAL_EVIDENCE"
    assert _execution_uncertainty([{**item,"mutating":True}],lean_goal=True)[0]
    assert _execution_uncertainty([{**item,"mutating":None}],lean_goal=True)[0]
