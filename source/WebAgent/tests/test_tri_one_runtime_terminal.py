"""Tri-One Runtime terminal authority and evidence-based goal admission."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.tri_one_runtime_terminal import (
    evaluate_goal_evidence, accept_runtime_terminal,
    terminal_verdict_from_loop, format_runtime_goal_summary,
)
from agent_core.tri_one_task_result import (
    make_task_result_from_webagent_loop, validate_task_result,
)


PATH = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
GOAL = "創立這個資料夾\n" + PATH


def _entry(*, path=PATH, status="SUCCEEDED", reason="DIRECTORY_CREATED",
           action_id="MIN-TEST-1", observed=True):
    receipt = {
        "tool": "create_directory",
        "status": status, "reason": reason,
        "executed": True, "requested_path": path, "resolved_path": path,
        "data": {
            "directory_created": True,
            "observed_exists": observed, "observed_is_directory": observed,
            "observation_status": "CONFIRMED" if observed else "UNKNOWN",
        },
    }
    return {"action": {"tool": "create_directory", "path": path,
                        "action_id": action_id},
            "receipt": receipt, "evidence_ref": "MIN-EV-EXAMPLE"}


def _loop(entry=None):
    entry = entry or _entry()
    aid = entry["action"]["action_id"]
    return SimpleNamespace(
        run_id="RR-TRI-ONE-TEST", task_id="TASK-TRI-ONE-TEST",
        task_epoch="epoch-123", turn_id=1, intent_digest="a" * 64,
        terminal_outcome="UNKNOWN", terminal_acceptance={},
        protocol_state="OBSERVED",
        action_result_ledger={
            aid: {"request_id": "RR-TRI-ONE-TEST",
                  "task_id": "TASK-TRI-ONE-TEST", "task_epoch": "epoch-123",
                  "result_status": "SUCCEEDED", "executed": True,
                  "evidence_status": "ACTIVE"},
        },
    )


def test_one_confirmed_mkdir_evidence_promotes_runtime_goal():
    proof = evaluate_goal_evidence(GOAL, [_entry()])
    assert proof.status == "PASS"
    assert proof.evidence_refs == ("MIN-TEST-1",)
    assert proof.goal_kind == "SINGLE_CREATE_DIRECTORY"
    assert PATH in format_runtime_goal_summary(proof)


def test_additional_successful_inspection_does_not_invalidate_goal():
    second = {
        "action": {"tool": "inspect_directory", "path": PATH,
                   "action_id": "MIN-READ-2"},
        "receipt": {"tool": "inspect_directory", "status": "SUCCEEDED",
                    "reason": "DIRECTORY_INSPECTED", "executed": True,
                    "data": {"observed_exists": True}},
    }
    assert evaluate_goal_evidence(GOAL, [_entry(), second]).verified


def test_contradictory_later_observation_is_not_ignored():
    second = {
        "action": {"tool": "inspect_directory", "path": PATH,
                   "action_id": "MIN-READ-2"},
        "receipt": {"tool": "inspect_directory", "status": "FAILED",
                    "reason": "DIRECTORY_NOT_FOUND", "executed": True, "data": {}},
    }
    verdict = evaluate_goal_evidence(GOAL, [_entry(), second])
    assert not verdict.verified
    assert verdict.reason == "CONTRADICTORY_LATER_OBSERVATION"


@pytest.mark.parametrize("goal", [
    "建立 " + PATH + " 並寫入 README.md",
    "建立 " + PATH + " 然後執行 git status",
    "刪除 " + PATH,
    "建立 " + PATH + " 和 " + PATH,
    "建立 E:\\Other",
])
def test_complex_or_foreign_goal_not_auto_completed(goal):
    assert not evaluate_goal_evidence(goal, [_entry()]).verified


@pytest.mark.parametrize("status,reason,observed", [
    ("REJECTED", "DIRECTORY_ALREADY_EXISTS_REOBSERVE", False),
    ("UNKNOWN", "DIRECTORY_CREATION_VERIFY_UNCERTAIN", False),
    ("SUCCEEDED", "DIRECTORY_CREATED", False),
])
def test_no_auto_success_from_unverified_creation(status, reason, observed):
    assert not evaluate_goal_evidence(
        GOAL, [_entry(status=status, reason=reason, observed=observed)]
    ).verified


def test_terminal_receipt_accepted_from_runtime_evidence_and_tri_one_v1():
    entry = _entry()
    loop = _loop(entry)
    proof = evaluate_goal_evidence(GOAL, [entry])
    receipt = accept_runtime_terminal(loop, proof)
    assert receipt["schema"] == "SMARTAGENT_TERMINAL_ACCEPTANCE_V1"
    assert receipt["admission_status"] == "ACCEPTED"
    assert receipt["evidence_verdict"] == "PASS"
    assert receipt["receipt_id"].startswith("TERM-")
    assert receipt["evidence_refs"] == [entry["action"]["action_id"]]
    terminal = terminal_verdict_from_loop(loop)
    assert terminal.state == "COMPLETED" and terminal.outcome == "SUCCESS"
    result = make_task_result_from_webagent_loop(loop, format_runtime_goal_summary(proof))
    assert result["schema"] == "TRI_ONE_TASK_RESULT_V1"
    assert result["task_state"] == "COMPLETED"
    assert result["outcome"] == "SUCCESS"
    assert result["completion_kind"] == "EVIDENCE_BACKED"
    assert result["evidence_refs"] == [entry["action"]["action_id"]]
    validate_task_result(result)


def test_unverified_finish_projects_paused_in_all_tri_one_modes():
    loop = _loop()
    loop.terminal_outcome = "UNKNOWN"
    loop.terminal_acceptance = {}
    terminal = terminal_verdict_from_loop(loop)
    assert terminal.state == "PAUSED" and terminal.outcome == "UNKNOWN"
    result = make_task_result_from_webagent_loop(loop, "尚未通過驗收")
    assert result["task_state"] == "PAUSED"
    assert result["outcome"] == "UNKNOWN"
    assert result["terminal_acceptance"]["status"] == "MISSING"
    assert result["completion_kind"] == "LEGACY_UNVERIFIED"


def test_fake_receipt_cannot_create_success():
    loop = _loop()
    proof = evaluate_goal_evidence(GOAL, [_entry()])
    loop.action_result_ledger["MIN-TEST-1"]["request_id"] = "RR-FOREIGN"
    with pytest.raises(ValueError, match="identity_or_status"):
        accept_runtime_terminal(loop, proof)
    assert loop.terminal_outcome == "UNKNOWN"
    assert loop.terminal_acceptance == {}


def test_foreign_or_mismatched_terminal_receipt_cannot_be_delivered_as_success():
    loop = _loop()
    loop.terminal_outcome = "SUCCESS"
    loop.terminal_acceptance = {
        "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
        "admission_status": "ACCEPTED", "receipt_id": "TERM-FORGED",
        "request_id": "RR-OTHER", "task_id": loop.task_id,
        "task_epoch": loop.task_epoch, "task_outcome": "SUCCESS",
        "runtime_state": "COMPLETED", "evidence_refs": ["MIN-TEST-1"],
        "evidence_verdict": "PASS",
    }
    terminal = terminal_verdict_from_loop(loop)
    assert terminal.state == "PAUSED"
    assert terminal.outcome == "UNKNOWN"
