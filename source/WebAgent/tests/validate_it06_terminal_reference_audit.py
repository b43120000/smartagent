"""IT06 terminal control-plane provenance: offline, no browser/Telegram.

Run: PYTHONPATH=source python source/WebAgent/tests/validate_it06_terminal_reference_audit.py
"""
from pathlib import Path
from types import SimpleNamespace

from agent_core.evidence_identity_audit import audit_terminal_references
from WebAgent.protocol_loop import WebAgentProtocolLoop


REQUEST = "IT06-REQUEST"
EPOCH = "IT06-EPOCH"
REF = "PROTOCOL_SPEC_REJECTION"


def blocker():
    return {
        "action_id": "IT06-R002-ORIGINAL-PREDICATE-A2",
        "reason": "evidence_file_exists_boolean_required",
        "request_id": REQUEST,
        "task_epoch": EPOCH,
        "executed": False,
        "verification_status": "SPEC_INVALID",
    }


def audit(refs, record=None, ledger=None):
    return audit_terminal_references(
        refs, ledger or {}, REQUEST, EPOCH, protocol_spec_rejection=record,
    )


def test_exact_runtime_blocker_is_scoped_not_observation():
    value = audit([REF], blocker())
    assert value["identity_status"] == "SCOPED", value
    assert value["identity_reason"] == "SCOPED", value
    assert value["action_ref_count"] == 0, value
    assert value["protocol_ref_count"] == 1, value
    assert value["unknown_ref_count"] == 0, value


def test_spoofed_missing_stale_foreign_and_executed_are_not_scoped():
    candidates = [
        None, {}, {**blocker(), "request_id": "FOREIGN"},
        {**blocker(), "task_epoch": "FOREIGN"},
        {**blocker(), "executed": True},
        {**blocker(), "verification_status": "PASS"},
        {**blocker(), "reason": "not_our_protocol_rejection"},
        {**blocker(), "action_id": ""},
    ]
    for record in candidates:
        value = audit([REF], record)
        assert value["identity_status"] == "SUSPECT", (record, value)
        assert value["identity_reason"] == "UNKNOWN_REF", (record, value)
        assert value["unknown_ref_count"] == 1, (record, value)
    foreign = audit(["REAL-ACTION"], blocker(), {
        "REAL-ACTION": {"request_id": "FOREIGN", "task_epoch": EPOCH}
    })
    assert foreign["identity_reason"] == "FOREIGN", foreign
    incomplete = audit(["REAL-ACTION"], blocker(), {
        "REAL-ACTION": {"request_id": REQUEST}
    })
    assert incomplete["identity_reason"] == "INCOMPLETE", incomplete
    mixed = audit([REF, "UNKNOWN-ACTION"], blocker())
    assert mixed["identity_reason"] == "UNKNOWN_REF", mixed
    # A model can name the marker but cannot supply trusted Runtime evidence.
    assert audit([REF])["identity_status"] == "SUSPECT"


def test_native_terminal_receipt_never_ignores_suspect():
    loop = object.__new__(WebAgentProtocolLoop)
    loop.run_id = REQUEST
    loop.task_id = "IT06-TASK"
    loop.task_epoch = EPOCH
    loop.turn_id = 4
    loop.accepted_ack_ids = ["ACK-REAL"]
    loop.action_ledger = {}
    loop.action_result_ledger = {}
    loop.continuation_source = None
    loop.pending_result_recovery = {}
    loop._protocol_spec_rejection = blocker()
    loop.progress_ledger = SimpleNamespace(
        decision="INTERRUPT", outcome="UNKNOWN",
        matched_condition="blocked", matched_condition_id="COND-BLOCKED",
        evidence_refs=[REF],
    )
    events = []
    loop._emit = lambda stage, **data: events.append((stage, data))
    loop._terminal_evidence_verdict = lambda refs, **kw: "UNVERIFIED"
    loop._effective_verification_status = lambda refs, **kw: "UNKNOWN"
    receipt = loop._accepted_terminal_receipt(runtime_state="INTERRUPTED")
    assert receipt["admission_status"] == "ACCEPTED"
    assert receipt["evidence_refs"] == [REF]
    assert receipt["decision"] == "INTERRUPT" and receipt["task_outcome"] == "UNKNOWN"
    audits = [data for stage, data in events if stage == "terminal_evidence_identity_audit"]
    assert len(audits) == 1 and audits[0]["identity_status"] == "SCOPED"
    assert audits[0]["protocol_ref_count"] == 1
    assert not loop.action_result_ledger, "control ref must not synthesize Tool results"
    loop._protocol_spec_rejection = {**blocker(), "request_id": "FOREIGN"}
    previous = loop.terminal_acceptance
    try:
        loop._accepted_terminal_receipt(runtime_state="INTERRUPTED")
    except RuntimeError as exc:
        assert str(exc) == "terminal_reference_identity_not_scoped:UNKNOWN_REF", exc
    else:
        raise AssertionError("SUSPECT audit admitted a terminal Receipt")
    assert loop.terminal_acceptance is previous
    assert len([1 for stage, _ in events if stage == "runtime_terminal_accepted"]) == 1


if __name__ == "__main__":
    test_exact_runtime_blocker_is_scoped_not_observation()
    test_spoofed_missing_stale_foreign_and_executed_are_not_scoped()
    test_native_terminal_receipt_never_ignores_suspect()
    print("IT06_TERMINAL_REFERENCE_AUDIT_PASS")
