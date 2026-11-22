"""Git75 regression + Stage-local recovery + Big Blackboard integration UT."""
from __future__ import annotations

import json

import pytest

from agent_core.goal75_blackboard import emit_transition, read_events
from agent_core.goal75_stage_ledger import GoalStageLedger
from agent_core.goal75_recovery import (
    repair_single_decision, verify_terminal_blocker,
    choose_stage_recovery, goal_finish_readiness,
)
from WebAgent.minimal_decision import parse_minimal_decision


def _stages(count=10):
    return [
        {
            "stage_id": f"S{i:02d}",
            "description": f"Verify independent stage {i}",
            "depends_on": [f"S{i-1:02d}"] if i > 1 else [],
            "acceptance": [
                {"field": "data.stage", "operator": "eq", "expected": i},
                {"field": "data.exit_code", "operator": "eq", "expected": 0},
            ],
        }
        for i in range(1, count + 1)
    ]


def _ledger(tmp_path, task="TASK-G75-UNIT"):
    return GoalStageLedger(
        tmp_path, task_id=task, request_id="RR-G75-UNIT",
        task_epoch="EPOCH-G75-UNIT", goal="Ten stage task",
    )


def _receipt(stage, code=0):
    return {
        "tool": "run_command", "status": "SUCCEEDED" if code == 0 else "FAILED",
        "executed": True, "reason": "PROCESS_EXITED",
        "data": {"stage": stage, "exit_code": code, "process_state": "EXITED"},
    }


def test_10_stage_freeze_and_stage3_only_recovery(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.create(_stages())
    assert first["plan_sha256"] == ledger.create(_stages())["plan_sha256"]
    commits = {}
    for i in range(1, 11):
        sid = f"S{i:02d}"
        assert ledger.current()["stage_id"] == sid
        if i == 3:
            ledger.record_action(
                sid, action_id="A03-FAILED",
                evidence_ref="MIN-EV-failed000000000000000000",
                receipt=_receipt(i, 13),
            )
            with pytest.raises(ValueError, match="acceptance_unverified"):
                ledger.commit(sid, evidence_refs=["MIN-EV-failed000000000000000000"])
            ledger.recover(
                sid, reason_code="FAILED_COMMAND", strategy_id="ALTERNATIVE_TOOL"
            )
        ref = f"MIN-EV-{i:024d}"
        ledger.record_action(
            sid, action_id=f"A{i:02d}-SUCCESS",
            evidence_ref=ref, receipt=_receipt(i),
        )
        state = ledger.commit(sid, evidence_refs=[ref])
        commits[sid] = next(s["commit"]["commit_sha256"] for s in state["stages"]
                            if s["stage_id"] == sid)
        with pytest.raises(ValueError, match="frozen"):
            ledger.recover(sid, reason_code="RETRY", strategy_id="BAD")
    done = ledger.read()
    assert done["status"] == "STAGES_VERIFIED_GOAL_FINAL_CHECK_REQUIRED"
    assert ledger.current() is None
    assert all(s["status"] == "VERIFIED_COMMITTED" for s in done["stages"])
    assert next(s for s in done["stages"] if s["stage_id"] == "S03")["strategy_revision"] == 1
    assert all(next(s for s in done["stages"] if s["stage_id"] == sid)["commit"]["commit_sha256"] == sha
               for sid, sha in commits.items())
    events = read_events(tmp_path, "TASK-G75-UNIT")
    assert any(e["module"] == "GOAL75_STAGE_LEDGER" and
               e["event"] == "STAGE_LOCAL_REPLAN" and e["stage_id"] == "S03"
               for e in events)
    assert sum(e["event"] == "STAGE_VERIFIED_COMMITTED" for e in events) == 10
    assert goal_finish_readiness(done, independent_goal_verified=True)["status"] == "PASS"
    assert goal_finish_readiness(done, independent_goal_verified=False)["status"] == "NOT_READY"


def test_stage_plan_rejects_rewrite_and_out_of_order(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.create(_stages(3))
    with pytest.raises(ValueError, match="immutable"):
        ledger.create(_stages(4))
    with pytest.raises(ValueError, match="dependency_not_committed"):
        ledger.record_action("S02", action_id="A2", evidence_ref="MIN-EV-000000000000000000000002",
                             receipt=_receipt(2))
    with pytest.raises(ValueError, match="dependency_not_preceding"):
        _ledger(tmp_path, "TASK-G75-OTHER").create([{
            "stage_id": "S01", "description": "Invalid",
            "depends_on": ["S02"],
            "acceptance": [{"field": "status", "operator": "eq", "expected": "SUCCEEDED"}],
        }])


def test_uncertain_execution_never_replayed_or_committed(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.create(_stages(1))
    with pytest.raises(ValueError, match="finality_not_verified"):
        ledger.record_action("S01", action_id="A1",
                             evidence_ref="MIN-EV-000000000000000000000001",
                             receipt={"status": "UNKNOWN", "executed": True,
                                      "data": {"possible_partial_execution": True}})
    item = choose_stage_recovery(
        reason_code="POSSIBLE_PARTIAL_EXECUTION", stage_id="S01",
        attempt=1, uncertain_mutation=True,
    )
    assert item["status"] == "PAUSED_UNRESOLVED"
    assert item["route"] == "RECONCILE_WITHOUT_REPLAY"
    assert item["mutation_replay_authorized"] is False


def test_runtime_blocker_requires_objective_scope_and_no_alternative():
    raw = {
        "runtime_verified": True, "scope_bound": True,
        "goal_required": True, "alternative_exhausted": True,
    }
    assert verify_terminal_blocker(
        "SCOPE_ACCESS_DENIED", evidence=raw)["status"] == "TERMINAL_BLOCKED_VERIFIED"
    assert verify_terminal_blocker(
        "SCOPE_ACCESS_DENIED", evidence=raw,
        authorized_alternatives=1)["status"] == "RECOVERING"
    for reason in ("MAX_ROUNDS", "FINISH_CONTRACT_INVALID",
                   "FAILED_COMMAND", "GOAL_EVIDENCE_NOT_VERIFIED"):
        assert verify_terminal_blocker(reason, evidence=raw)["status"] == "RECOVERING"
    assert verify_terminal_blocker(
        "SCOPE_ACCESS_DENIED", evidence={"runtime_verified": False},
    )["status"] == "RECOVERING"


def test_canonical_schema_repair_is_lossless_and_no_command_rewrite():
    example = '{"type":"tool_call","name":"observe_fact","args":{"path":"X:\\\\test"}}'
    repaired = repair_single_decision(example)
    assert repaired
    assert repaired[1]["changes"] == ["args_to_arguments"]
    assert repaired[1]["authorization_granted"] is False
    decision = parse_minimal_decision(repaired[0], original_goal=r"X:\test")
    assert decision.kind == "tool_call"
    assert decision.arguments["path"] == r"X:\test"
    assert repair_single_decision(
        '{"type":"tool_call","name":"run_command","args":{"command":"rm -rf ."},'
        '"arguments":{"command":"true"}}'
    ) is None
    assert repair_single_decision(
        '{"type":"tool_call","name":"observe_fact","args":{},"args":{}}'
    ) is None


def test_minimal_parser_accepts_stage_contract_without_granting_action():
    stage = _stages(2)
    decision = parse_minimal_decision(json.dumps(
        {"type": "stage_plan", "stages": stage}
    ))
    assert decision.kind == "stage_plan"
    assert len(decision.stage_plan) == 2
    commit = parse_minimal_decision(json.dumps({
        "type": "stage_commit", "stage_id": "S01",
        "evidence_refs": ["MIN-EV-" + "a" * 24],
    }))
    assert commit.kind == "stage_commit"
    assert commit.tool == ""


def test_hash_chain_mixed_modules_and_redacted_values(tmp_path):
    root = tmp_path
    a = emit_transition(root, task_id="TASK-G75-TRACE",
                        request_id="RR-G75-TRACE", task_epoch="LEGACY",
                        module="TASK_PROGRESS_LEDGER", event="AUTHORITATIVE_WRITE",
                        before={"runtime_state": "PROCESSING"},
                        after={"runtime_state": "PAUSED", "secret": "hide-this"})
    b = emit_transition(root, task_id="TASK-G75-TRACE",
                        request_id="RR-G75-TRACE", task_epoch="EPOCH-G75",
                        module="SHARED_BLACKBOARD", event="SNAPSHOT_CHANGED",
                        before={"sections": {"goal": "hash1"}},
                        after={"sections": {"goal": "hash2"}})
    assert b["prev_event_sha256"] == a["event_sha256"]
    assert b["changed_sections"] == ["goal"]
    events = read_events(root, "TASK-G75-TRACE")
    assert len(events) == 2
    text = (root / "localdata/runtime/goal75_big_blackboard/TASK-G75-TRACE.jsonl").read_text()
    assert "hide-this" not in text
    assert events[0]["visible_changes"]["runtime_state"]["after"] == "PAUSED"
