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



# Live RCA 2026-11-08 / TASK-02D4A8B2A612BB5E546B:
# observe_fact(False) -> create_directory -> observe_fact(True) -> Finish.
CONDITIONAL_PATH = r"E:\SmartAgent_TriOne_Live_Test_B"
CONDITIONAL_GOAL = (
    "檢查 " + CONDITIONAL_PATH +
    " 是否存在，如果不存在就建立，最後確認資料夾確實存在。"
)


def _observed_entry(value: bool, *, action_id: str, path: str = CONDITIONAL_PATH,
                    freshness: str = "CURRENT", observed: str = "CONFIRMED",
                    fact_ref: str = "", executed: bool = True):
    from agent_core.evidence_foundation import observation
    fact = observation(
        "file.exists", value, source="filesystem.stat", scope=path,
        raw_ref=fact_ref or action_id, status=observed,
    )
    fact["freshness"] = freshness
    return {
        "action": {"tool": "observe_fact", "path": path, "action_id": action_id},
        "receipt": {
            "tool": "observe_fact", "status": "SUCCEEDED", "reason": "FACT_OBSERVED",
            "executed": executed, "action_id": action_id,
            "requested_path": path, "resolved_path": path,
            "data": {"observations": [fact], "observation_status": observed},
        },
    }


def _conditional_history():
    return [
        _observed_entry(False, action_id="MIN-CHECK-BEFORE"),
        _entry(path=CONDITIONAL_PATH, action_id="MIN-CREATE-NEW"),
        _observed_entry(True, action_id="MIN-CHECK-AFTER"),
    ]


def test_live_conditional_directory_goal_verified_by_three_real_receipt_shapes():
    history = _conditional_history()
    proof = evaluate_goal_evidence(CONDITIONAL_GOAL, history)
    assert proof.verified
    assert proof.goal_kind == "CONDITIONAL_DIRECTORY_EXISTS"
    assert proof.reason == "CONDITIONAL_DIRECTORY_CREATED_AND_REOBSERVED"
    assert proof.evidence_refs == (
        "MIN-CHECK-BEFORE", "MIN-CREATE-NEW", "MIN-CHECK-AFTER")
    assert CONDITIONAL_PATH in format_runtime_goal_summary(proof)


def test_conditional_directory_goal_has_no_premature_completion_after_mkdir():
    history = _conditional_history()
    assert not evaluate_goal_evidence(CONDITIONAL_GOAL, history[:1]).verified
    assert not evaluate_goal_evidence(CONDITIONAL_GOAL, history[:2]).verified


@pytest.mark.parametrize("change", [
    "bad_fact_ref", "wrong_scope", "not_fresh", "unconfirmed",
    "missing_execution", "wrong_target", "no_creation", "post_false",
    "extra_goal", "unobserved_later_mutation",
])
def test_conditional_directory_fail_closed_when_required_evidence_missing(change):
    history = _conditional_history()
    goal = CONDITIONAL_GOAL
    if change == "bad_fact_ref":
        history[2]["receipt"]["data"]["observations"][0]["raw_evidence_ref"] = "MIN-FOREIGN"
    elif change == "wrong_scope":
        history[2]["receipt"]["data"]["observations"][0]["scope"] = r"E:\Elsewhere"
    elif change == "not_fresh":
        history[2]["receipt"]["data"]["observations"][0]["freshness"] = "STALE"
    elif change == "unconfirmed":
        history[2]["receipt"]["data"]["observations"][0]["observation_status"] = "UNKNOWN"
    elif change == "missing_execution":
        history[2]["receipt"]["executed"] = False
    elif change == "wrong_target":
        history[1]["receipt"]["requested_path"] = r"E:\Foreign"
    elif change == "no_creation":
        history[1]["receipt"]["status"] = "UNKNOWN"
    elif change == "post_false":
        history[2]["receipt"]["data"]["observations"][0]["value"] = False
    elif change == "extra_goal":
        goal += " 並且寫入 README.md"
    elif change == "unobserved_later_mutation":
        history.append(_entry(path=CONDITIONAL_PATH, action_id="MIN-POST-MUTATION"))
    assert not evaluate_goal_evidence(goal, history).verified


def test_conditional_directory_acceptance_uses_all_three_action_ledger_refs():
    history = _conditional_history()
    proof = evaluate_goal_evidence(CONDITIONAL_GOAL, history)
    loop = _loop()
    for action_id in proof.evidence_refs:
        loop.action_result_ledger[action_id] = {
            "request_id": loop.run_id, "task_id": loop.task_id,
            "task_epoch": loop.task_epoch, "executed": True,
            "result_status": "SUCCEEDED", "evidence_status": "ACTIVE",
        }
    receipt = accept_runtime_terminal(loop, proof)
    assert receipt["evidence_refs"] == list(proof.evidence_refs)
    assert terminal_verdict_from_loop(loop).state == "COMPLETED"
    result = make_task_result_from_webagent_loop(
        loop, format_runtime_goal_summary(proof))
    assert result["completion_kind"] == "EVIDENCE_BACKED"
    assert result["evidence_refs"] == list(proof.evidence_refs)
    validate_task_result(result)


def test_conditional_directory_ledger_identity_mismatch_cannot_be_accepted():
    history = _conditional_history()
    proof = evaluate_goal_evidence(CONDITIONAL_GOAL, history)
    loop = _loop()
    for action_id in proof.evidence_refs:
        loop.action_result_ledger[action_id] = {
            "request_id": loop.run_id, "task_id": loop.task_id,
            "task_epoch": loop.task_epoch, "executed": True,
            "result_status": "SUCCEEDED", "evidence_status": "ACTIVE",
        }
    loop.action_result_ledger["MIN-CHECK-BEFORE"]["task_epoch"] = "other-epoch"
    with pytest.raises(ValueError, match="identity_or_status"):
        accept_runtime_terminal(loop, proof)
    assert loop.terminal_outcome == "UNKNOWN"



def test_preexisting_file_is_not_mistaken_for_verified_directory():
    # An existence-only stat cannot tell a regular file from a folder.
    rows = [_observed_entry(True, action_id="MIN-EXISTS-1"),
            _observed_entry(True, action_id="MIN-EXISTS-2")]
    assert not evaluate_goal_evidence(CONDITIONAL_GOAL, rows).verified


def test_preexisting_directory_with_confirmed_listing_is_verified():
    listing = {
        "action": {"tool": "list_directory", "path": CONDITIONAL_PATH,
                   "action_id": "MIN-LIST-EXISTING"},
        "receipt": {
            "tool": "list_directory", "status": "SUCCEEDED",
            "reason": "DIRECTORY_LISTED", "executed": True,
            "requested_path": CONDITIONAL_PATH,
            "data": {"terminal_eligible": True, "entries": []},
        },
    }
    rows = [_observed_entry(True, action_id="MIN-EXISTS-1"),
            listing, _observed_entry(True, action_id="MIN-EXISTS-2")]
    proof = evaluate_goal_evidence(CONDITIONAL_GOAL, rows)
    assert proof.verified
    assert proof.reason == "CONDITIONAL_DIRECTORY_ALREADY_PRESENT"
    assert proof.evidence_refs == ("MIN-LIST-EXISTING", "MIN-EXISTS-2")


def test_goal_gap_reconstructs_real_observe_fact_false_and_preserves_create_resolution():
    from types import SimpleNamespace
    from agent_core.tool_result_contract import attach_runtime_result
    from agent_core.goal_gap_ledger import reconstruct_goal_gaps

    action_ledger, action_result_ledger = {}, {}
    loop = SimpleNamespace(
        run_id="RR-TRI-ONE-FACT-TEST", task_epoch="epoch-fact-1",
        task_request=CONDITIONAL_GOAL,
        progress_ledger=SimpleNamespace(goal=CONDITIONAL_GOAL),
        action_ledger=action_ledger, action_result_ledger=action_result_ledger,
    )

    def record_history(entry):
        action, receipt = entry["action"], entry["receipt"]
        action_id = action["action_id"]
        result = attach_runtime_result(
            "{}", receipt["status"], receipt["reason"],
            tool=action["tool"], executed=receipt["executed"],
            requested_path=receipt.get("requested_path", action.get("path", "")),
            resolved_path=receipt.get("resolved_path"),
            data=receipt["data"], action_id=action_id,
            request_id=loop.run_id, task_id="TASK-TRI-ONE-FACT-TEST",
        )
        action_ledger[action_id] = {"action": action, "result": result}
        action_result_ledger[action_id] = {
            "tool": action["tool"], "request_id": loop.run_id,
            "task_epoch": loop.task_epoch, "result": result,
            "result_status": receipt["status"], "result_reason": receipt["reason"],
            "executed": receipt["executed"], "evidence_status": "ACTIVE",
        }
        return reconstruct_goal_gaps(loop)

    before = record_history(_observed_entry(False, action_id="MIN-FACT-MISSING"))
    assert before["active_count"] == 1, before
    assert before["active_gaps"][0]["observed_fact_ref"] == "MIN-FACT-MISSING"
    after_create = record_history(_entry(
        path=CONDITIONAL_PATH, action_id="MIN-FACT-CREATED"))
    assert after_create["active_count"] == 0, after_create
    after_restat = record_history(_observed_entry(
        True, action_id="MIN-FACT-REOBSERVED"))
    assert after_restat["active_count"] == 0
    assert after_restat["gaps"][0]["status"] == "RESOLVED"
    assert after_restat["gaps"][0]["observed_fact_ref"] == "MIN-FACT-CREATED"
