"""IT5 Runtime Evidence correctness regressions.

Use PYTHONPATH=source python source/WebAgent/tests/validate_runtime_evidence_it5.py.
No model, browser, or production filesystem access; only temporary fixture files.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_core.tool_result_contract import decode_result, attach_runtime_result
from agent_core.evidence_identity_audit import audit_terminal_references
from agent_core.task_state import (
    TASK_PAUSED, TASK_RUNNING, RemoteTaskQueue, TaskRecord, TaskStateStore
)
from WebAgent.protocol_loop import WebAgentProtocolLoop, extract_authorized_paths


def test_legacy_scope_and_envelope_priority() -> None:
    raw = '[TOOL_SCOPE_REJECTED] {"allowed":false}'
    outcome = decode_result(raw)
    assert outcome["status"] == "REJECTED", outcome
    assert outcome["reason"] == "TOOL_SCOPE_REJECTED", outcome
    assert outcome["executed"] is False
    facts = WebAgentProtocolLoop._classify_action_evidence(
        {"tool": "inspect_project_scope", "paths": []}, raw
    )
    assert facts["result_reason"] == "TOOL_SCOPE_REJECTED", facts
    assert facts["executed"] is False
    enriched = attach_runtime_result(
        raw, "REJECTED", "WORKING_DIRECTORY_REJECTED",
        tool="inspect_project_scope", executed=False
    )
    decoded = decode_result(enriched)
    assert decoded["reason"] == "WORKING_DIRECTORY_REJECTED", decoded
    assert decoded["executed"] is False
    assert decode_result('[SECURITY_CONFIRMATION_REQUIRED]')["reason"] == "SECURITY_CONFIRMATION_REQUIRED"


def test_path_prose_does_not_expand_scope() -> None:
    f = r"C:\fixture\visible_code.py"
    p = r"C:\fixture\present.txt"
    assert extract_authorized_paths(f + " 的結構化狀態") == [f]
    assert extract_authorized_paths(p + " 的 file.exists equals true") == [p]
    assert extract_authorized_paths(p + " 的事前 file.exists equals true 成功條件") == [p]
    assert extract_authorized_paths('"' + f + ' name"') == [f + " name"]
    assert extract_authorized_paths(r"C:\fixture\name with spaces.txt") == [r"C:\fixture\name with spaces.txt"]
    # Do not discard ADS-looking substrings to fabricate a permitted path.
    dangerous = extract_authorized_paths(r"C:\fixture\file.txt:secret")
    assert dangerous and ":secret" in dangerous[0]


def test_identity_missing_is_not_scoped() -> None:
    ledger = {
        "same": {"request_id": "RR", "task_epoch": "E"},
        "missing_epoch": {"request_id": "RR"},
        "other": {"request_id": "OTHER", "task_epoch": "E"},
    }
    assert audit_terminal_references(["same"], ledger, "RR", "E")["identity_status"] == "SCOPED"
    incomplete = audit_terminal_references(["missing_epoch"], ledger, "RR", "E")
    assert incomplete["identity_status"] == "INCOMPLETE", incomplete
    assert incomplete["incomplete_ref_count"] == 1
    assert audit_terminal_references(["other"], ledger, "RR", "E")["identity_status"] == "FOREIGN"
    assert audit_terminal_references(["unknown"], ledger, "RR", "E")["identity_status"] == "UNKNOWN_REF"
    assert audit_terminal_references(["same", "missing_epoch"], ledger, "RR", "E")["identity_status"] == "INCOMPLETE"


def test_queue_paused_is_not_terminal_or_replayable() -> None:
    with TemporaryDirectory(prefix="smartagent_it5_queue_") as tmp:
        store = TaskStateStore(Path(tmp) / "queue.json")
        task = TaskRecord(task_id="TASK-IT5-PAUSE", state=TASK_RUNNING,
                          request_id="REQUEST-IT5", task_epoch="EPOCH-IT5")
        store.put(task)
        queue = RemoteTaskQueue(store)
        paused = queue.pause_running(task.task_id, "IT5_BLOCKED", result={"evidence": "retained"})
        assert paused is not None
        assert paused.state == TASK_PAUSED
        assert paused.completed_at is None
        assert paused.result_ledger["paused"]["evidence"] == "retained"
        assert queue.pause_running(task.task_id, "DO_NOT_OVERWRITE") is None
        with store.process_lock():
            store.load()
            stored = store.get(task.task_id)
            assert stored.state == TASK_PAUSED
            assert stored.completed_at is None
            assert stored.error == "IT5_BLOCKED"


if __name__ == "__main__":
    test_legacy_scope_and_envelope_priority()
    test_path_prose_does_not_expand_scope()
    test_identity_missing_is_not_scoped()
    test_queue_paused_is_not_terminal_or_replayable()
    print("RUNTIME_EVIDENCE_IT5_REGRESSION_PASS")
