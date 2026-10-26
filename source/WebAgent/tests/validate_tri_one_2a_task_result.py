"""2A Source contract regression; pure fixtures, no browser/network/disk writes.

Run from checkout: PYTHONPATH=source python source/WebAgent/tests/validate_tri_one_2a_task_result.py
"""
from __future__ import annotations

from copy import deepcopy

from agent_core.tri_one_task_result import (
    SCHEMA, TaskResultContractError, enabled, make_task_result, validate_task_result,
)


def case_receipt(*, refs=None, outcome="SUCCESS", state="COMPLETED", request_id="RR-A", task_id="TASK-A", task_epoch="EPOCH-A"):
    refs = [] if refs is None else list(refs)
    return {
        "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
        "admission_status": "ACCEPTED",
        "receipt_id": "TERM-TEST-ONLY",
        "request_id": request_id, "task_id": task_id, "task_epoch": task_epoch,
        "task_outcome": outcome, "runtime_state": state,
        "evidence_refs": refs, "evidence_verdict": "PASS" if refs else "UNVERIFIED",
    }


def build(**overrides):
    values = dict(
        request_id="RR-A", task_id="TASK-A", task_epoch="EPOCH-A",
        intent_digest="DIGEST-A", source_interface="LOCAL_TEST",
        trusted_reply_route={"transport": "LOCAL_TEST", "origin": "safe-fixture"},
        state="COMPLETED", outcome="SUCCESS",
        terminal_acceptance=case_receipt(),
        evidence_refs=[], operational_action_count=0,
        answer_text="existing model response", artifacts=[],
    )
    values.update(overrides)
    return make_task_result(**values)


def rejected(reason, fn):
    try:
        fn()
    except TaskResultContractError as error:
        assert str(error) == reason, (reason, str(error))
    else:
        raise AssertionError(f"expected rejection: {reason}")


def run():
    # 2A-U01: opt-in, default OFF, no global mutation.
    assert not enabled({})
    assert not enabled({"TRI_ONE_TASK_RESULT_V1": "off"})
    assert enabled({"TRI_ONE_TASK_RESULT_V1": "true"})

    # 2A-U02: deterministic schema/result ID, no delivery or evidence PASS claims.
    a = build()
    b = build()
    assert a == b and a["schema"] == SCHEMA
    assert a["task_state"] == "COMPLETED" and a["outcome"] == "SUCCESS"
    assert a["completion_kind"] == "LEGACY_UNVERIFIED"
    assert a["terminal_acceptance"]["status"] == "ACCEPTED"
    assert a["delivery"]["status"] == "PENDING"
    assert not a["delivery"]["delivery_receipt_id"]
    assert validate_task_result(a)["result_id"] == a["result_id"]

    # 2A-U03: evidence-backed only with real accepted scoped receipt.
    evidence = build(
        terminal_acceptance=case_receipt(refs=["ACT-1"]),
        evidence_refs=["ACT-1"], operational_action_count=1,
    )
    assert evidence["completion_kind"] == "EVIDENCE_BACKED"
    rejected("unverified_evidence_backed_claim", lambda: build(
        terminal_acceptance=case_receipt(),
        completion_kind="EVIDENCE_BACKED",
    ))

    # 2A-U04: reject missing, foreign or mismatched identity.
    rejected("missing_task_identity", lambda: build(task_epoch=""))
    rejected("foreign_or_contradictory_receipt", lambda: build(task_id="TASK-OTHER"))
    rejected("foreign_or_contradictory_receipt", lambda: build(outcome="UNKNOWN"))
    rejected("receipt_evidence_mismatch", lambda: build(
        evidence_refs=["ACT-2"], terminal_acceptance=case_receipt(refs=["ACT-1"]),
    ))

    # 2A-U05: no counterfeit terminal, no fake answer-only lane.
    rejected("success_without_terminal_receipt", lambda: build(terminal_acceptance={}))
    rejected("answer_only_not_implemented_in_2a", lambda: build(completion_kind="ANSWER_ONLY"))
    rejected("unaccepted_terminal_receipt", lambda: build(
        terminal_acceptance={**case_receipt(), "admission_status": "REJECTED"},
    ))

    # 2A-U06: queued/unknown workflow should not be silently upgraded.
    unknown = build(outcome="UNKNOWN", terminal_acceptance=None)
    assert unknown["outcome"] == "UNKNOWN"
    assert unknown["terminal_acceptance"]["status"] == "MISSING"
    rejected("noncompleted_success", lambda: build(
        state="PAUSED", terminal_acceptance=case_receipt(state="PAUSED"),
    ))

    # 2A-U07: completion and delivery independent; cannot fake delivered.
    altered = deepcopy(a)
    altered["delivery"]["status"] = "DELIVERED"
    rejected("delivery_receipt_missing", lambda: validate_task_result(altered))
    altered["delivery"]["status"] = "PENDING"
    assert validate_task_result(altered)["delivery"]["status"] == "PENDING"

    # 2A-U08: routes are trusted adapter data; route itself stays private.
    assert "safe-fixture" not in str(a) and a["reply_route_id"].startswith("ROUTE-")
    rejected("route_origin_mismatch", lambda: build(
        trusted_reply_route={"transport": "TELEGRAM", "chat_id": 444},
    ))
    rejected("missing_trusted_reply_route", lambda: build(trusted_reply_route=None))

    # 2A-U09: never pretend task result is a transport delivery receipt.
    rejected("unverified_success", lambda: validate_task_result({
        **a, "terminal_acceptance": {"status": "MISSING"},
    }))
    return {"tri_one_2a_task_result": "PASS", "cases": 9}


if __name__ == "__main__":
    print(run())
