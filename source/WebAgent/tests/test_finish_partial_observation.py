"""Exercise the real Finish comparator and Blackboard with isolated receipts."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_core.executor_run_blackboard import (
    append_executor_event, begin_executor_run, finish_executor_run, record_finish_comparison,
)
from agent_core.finish_evidence_requirement import assess_finish_evidence_requirement
from agent_core.runtime_finish_comparator import compare_runtime_finish
from agent_core.tool_result_contract import decode_result, encode_result


ROOT = r"C:\isolated"
MISSING = ROOT + r"\target.txt"
GOAL = "Report the observed result of this inspection and verify the requested file."


def inspection(status="PARTIAL", reason="DIRECTORY_INSPECTION_INCOMPLETE", *, errors=None):
    action = {"tool": "inspect_directory", "paths": [ROOT, MISSING], "recursive": False}
    receipt = decode_result(encode_result(status, reason, tool=action["tool"], executed=True))
    raw = json.dumps({
        "tool": action["tool"], "recursive": False, "_runtime_result": receipt,
        "paths": [
            {"path": ROOT, "authorized": True, "exists": True, "errors": [],
             "inaccessible_count": 0},
            {"path": MISSING, "authorized": True, "exists": False,
             "errors": errors if errors is not None else ["path_not_found"],
             "inaccessible_count": 0},
        ],
    })
    return action, raw


def listing(name="target.txt", resolved=ROOT):
    action = {"tool": "list_directory", "path": ROOT}
    return action, encode_result(
        "SUCCEEDED", "DIRECTORY_LISTED", tool=action["tool"], executed=True,
        requested_path=ROOT, resolved_path=resolved,
        data={"entries": [{"name": name, "kind": "FILE"}], "recursive": False,
              "terminal_eligible": True},
    )


def run_finish(tmp_path, actions, *, claim_override=None, check_override=None,
               damage=False, goal=GOAL):
    loop = SimpleNamespace(task_id="TASK-TEST", run_id="RR-TEST", task_epoch="EPOCH-TEST",
                           turn_id=len(actions) + 1, action_result_ledger={},
                           _canonical_action_proposals={})
    begin_executor_run(tmp_path, loop, goal,
                       evidence_requirement=assess_finish_evidence_requirement(goal))
    claims, checks = [], []
    for index, (action, raw) in enumerate(actions):
        action_id, ref = f"ACT-{index}", f"MIN-EV-{index:024x}"
        receipt = decode_result(raw)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        meta = {"status": receipt["status"], "executed": receipt["executed"],
                "evidence_ref": ref, "result_sha256": digest}
        append_executor_event(tmp_path, loop, {
            "kind": "TOOL_RESULT", "action_id": action_id, "toolName": action["tool"],
            "toolResult": {"isSuccess": receipt["status"] == "SUCCEEDED"},
            "runtimeEvidence": meta,
        })
        loop._canonical_action_proposals[action_id] = {**action, "action_id": action_id}
        loop.action_result_ledger[action_id] = {
            "result": raw, "result_sha256": digest, "result_id": ref, "action_id": action_id,
            "request_id": loop.run_id, "task_id": loop.task_id, "task_epoch": loop.task_epoch,
            "executed": receipt["executed"], "result_status": receipt["status"],
            "evidence_status": "ACTIVE",
        }
        fields = {"status": receipt["status"]}
        if receipt["status"] == "SUCCEEDED":
            fields["data.terminal_eligible"] = True
            checks.append({"criterion": f"listing {index}", "field": "data.terminal_eligible",
                           "operator": "eq", "evidence_ref": ref, "expected": True})
        claims.append({"evidence_ref": ref, "fields": fields})
    if damage:
        loop.action_result_ledger["ACT-0"]["result"] += " "
    if claim_override:
        claim_override(claims)
    if check_override:
        check_override(checks, claims)
    finish_executor_run(tmp_path, loop, terminal_kind="MODEL_REPORTED_COMPLETE_UNVERIFIED",
                        summary="Comparator regression")
    comparison = compare_runtime_finish(loop, tmp_path, goal, claims, checks)
    saved = record_finish_comparison(tmp_path, loop, comparison)["terminal"]["finish_comparison"]
    if "reconciled_observations" in comparison:
        assert saved["reconciled_observations"] == comparison["reconciled_observations"]
    unconfirmed = (comparison.get("finish_diagnostic") or {}).get("unconfirmed_evidence")
    if unconfirmed:
        assert saved["unconfirmed_evidence"] == unconfirmed
    return comparison


def test_followup_completes_partial_without_rewriting_history(tmp_path):
    result = run_finish(tmp_path, [inspection(), listing()])
    assert result["verdict"] == "PASS", result
    initial, followup = result["evidence_refs"]
    assert result["reconciled_observations"] == [
        {"evidence_ref": initial, "resolved_by": [followup]},
    ]
    assert result["tool_results"][0]["execution_status"] == "PARTIAL"


@pytest.mark.parametrize("actions", [
    [inspection(), listing("sibling.txt")],
    [listing(), inspection()],
    [inspection(), listing(resolved=r"C:\other")],
    [inspection(errors=["permission_denied"]), listing()],
    [inspection(status="UNKNOWN"), listing()],
    [inspection(status="RUNNING"), listing()],
    [({"tool": "write_file"}, encode_result("PARTIAL", "WRITE_IO_ERROR",
                                           tool="write_file", executed=True)), listing()],
    [({"tool": "delete_path"}, encode_result("PARTIAL", "DELETE_IO_ERROR",
                                            tool="delete_path", executed=True)), listing()],
])
def test_unconfirmed_execution_or_wrong_scope_fails_closed(tmp_path, actions):
    result = run_finish(tmp_path, actions)
    assert result["reason_code"] == "RUNTIME_EXECUTION_NOT_CONFIRMED", result
    assert result["finish_diagnostic"]["unconfirmed_evidence"]


def test_claim_coverage_and_raw_integrity_still_required(tmp_path_factory):
    actions = [inspection(), listing()]
    omitted = run_finish(tmp_path_factory.mktemp("missing"), actions,
                         claim_override=lambda claims: claims.pop(0))
    assert omitted["reason_code"] == "EXPECTED_RUNTIME_COVERAGE_MISMATCH"
    wrong = run_finish(tmp_path_factory.mktemp("wrong"), actions,
                       claim_override=lambda claims: claims[0]["fields"].update(status="SUCCEEDED"))
    assert wrong["reason_code"] == "MODEL_RUNTIME_MISMATCH"
    damaged = run_finish(tmp_path_factory.mktemp("damaged"), actions, damage=True)
    assert damaged["reason_code"] == "EVIDENCE_DIGEST_MISMATCH"


def test_reconciled_partial_cannot_supply_a_goal_fact(tmp_path):
    def add_partial_check(checks, claims):
        claims[0]["fields"]["data.terminal_eligible"] = False
        checks.append({**checks[0], "criterion": "partial check",
                       "evidence_ref": claims[0]["evidence_ref"], "expected": False})

    action, raw = inspection()
    payload = json.loads(raw)
    payload["_runtime_result"]["data"]["terminal_eligible"] = False
    result = run_finish(tmp_path, [(action, json.dumps(payload)), listing()],
                        check_override=add_partial_check)
    assert result["reason_code"] == "GOAL_CHECK_LACKS_SUCCEEDED_EVIDENCE"


def test_followup_does_not_satisfy_unrelated_frozen_goal(tmp_path):
    result = run_finish(tmp_path, [inspection(), listing()],
                        goal=r"請確保資料夾存在 C:\isolated\other")
    assert result["reason_code"] == "FROZEN_GOAL_STATE_NOT_SATISFIED", result
    assert result["goal_contract_sha256"]
