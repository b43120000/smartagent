#!/usr/bin/env python3
"""Focused validation for the Runtime-owned Action Loop state contract."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_core.action_loop_state import (
    ACTION_LOOP_STATE_SCHEMA,
    build_action_loop_state,
    phase_for_action,
    phase_for_tool,
    semantic_action_signature,
)
from agent_core.task_progress import initialize_progress, record_model_progress, read_progress
from WebAgent.protocol_loop import WebAgentProtocolLoop


def canonical_progress(**extra) -> dict:
    payload = {
        "tool": "report_progress",
        "action_id": "P-1",
        "base_evaluation": "Runtime contract test",
        "total_steps": 2,
        "current_step": 1,
        "steps": [
            {"step": 1, "desc": "collect evidence", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "finish", "status": "PENDING"},
        ],
        "current_focus": "collect evidence",
        "next_action": "query_project",
        "completion_contract": {
            "success": ["verified"],
            "failure": ["verification failed"],
            "in_progress": ["evidence pending"],
            "interrupted": ["runtime unavailable"],
        },
        "decision": "CONTINUE",
        "outcome": "PENDING",
        "matched_condition": "evidence pending",
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "evidence is not complete",
    }
    payload.update(extra)
    return payload


def validate_state_classification() -> None:
    initial = build_action_loop_state()
    assert initial["schema"] == ACTION_LOOP_STATE_SCHEMA
    assert initial["phase"] == "DISCOVERY"
    assert initial["required_transition"] == "CONTINUE_PLAN"

    empty_query = {
        "tool": "query_project", "action_id": "Q-1",
        "project_root": r"C:\SmartAgentWorkspace", "queries": [{"operation": "search_text", "query": "missing"}],
    }
    empty = build_action_loop_state(
        action=empty_query,
        result='{"status":"READY","results":[{"operation":"search_text","status":"OK","matches":[]}]}',
        execution_status="SUCCEEDED",
    )
    assert empty["evidence_state"] == "MISSING"
    assert empty["required_transition"] == "REPLAN_REQUIRED"
    assert empty["blocked_action_signatures"] == [semantic_action_signature(empty_query)]

    found = build_action_loop_state(
        action=empty_query,
        result='{"status":"READY","results":[{"operation":"search_text","status":"OK","matches":[{"path":"a.py"}]}]}',
        execution_status="SUCCEEDED",
    )
    assert found["evidence_state"] == "AVAILABLE"
    assert found["required_transition"] == "CONTINUE_PLAN"


def validate_action_phase_and_lifecycle_contract() -> None:
    assert phase_for_action({"tool": "run_command"}) == "EXECUTE"
    assert phase_for_action({
        "tool": "run_command", "verifies_action_id": "A-EXEC",
    }) == "VERIFY"
    assert phase_for_tool("upload_file") == "EXECUTE"
    assert phase_for_tool("web_search") == "DISCOVERY"
    try:
        phase_for_tool("invented_tool")
    except ValueError as exc:
        assert "action_loop_tool_phase_unmapped" in str(exc)
    else:
        raise AssertionError("unmapped tools must not silently become DISCOVERY")

    action = {"tool": "write_file", "action_id": "A-WRITE", "path": "a.txt", "content": "x"}
    state = build_action_loop_state(
        action=action,
        result="written",
        execution_status="SUCCEEDED",
        verification_status="UNKNOWN",
        action_records=[{
            "action": action,
            "result": "written",
            "execution_status": "SUCCEEDED",
            "verification_status": "UNKNOWN",
        }],
    )
    assert state["phase"] == "VERIFY"
    assert state["required_transition"] == "VERIFY_REQUIRED"
    assert state["action_states"][0]["role"] == "EXECUTE"
    assert state["action_states"][0]["lifecycle"] == "EXECUTED_UNVERIFIED"
    assert "runtime_state_ref" in state["response_contract"]["runtime_owned_progress_fields"]

    expected_failure = build_action_loop_state(
        action={
            "tool": "run_command", "operation": "BUILD",
            "action_id": "A-EXPECTED-FAILURE", "expected_failure": {"exit_codes": [7]},
        },
        result="exit_code: 7\nVERIFICATION_STATUS: FAIL\nEXPECTATION_STATUS: PASS",
        execution_status="FAILED",
        verification_status="PASS",
        expectation_status="PASS",
        action_records=[{
            "action": {
                "tool": "run_command", "operation": "BUILD",
                "action_id": "A-EXPECTED-FAILURE", "expected_failure": {"exit_codes": [7]},
            },
            "result": "exit_code: 7\nVERIFICATION_STATUS: FAIL\nEXPECTATION_STATUS: PASS",
            "execution_status": "FAILED",
            "verification_status": "PASS",
            "expectation_status": "PASS",
        }],
    )
    assert expected_failure["execution_status"] == "FAILED"
    assert expected_failure["expectation_status"] == "PASS"
    assert expected_failure["evidence_state"] == "SUFFICIENT"
    assert expected_failure["required_transition"] == "CONTINUE_PLAN"
    assert expected_failure["action_states"][0]["lifecycle"] == "EXPECTED_FAILURE_PASS"


def validate_prerequisite_and_verification_routes() -> None:
    prerequisite = {
        "tool": "query_project",
        "project_root": r"C:\SmartAgentWorkspace",
        "queries": [{"operation": "read_range", "path": "a.py", "start_line": 1, "end_line": 80}],
    }
    state = build_action_loop_state(
        pending_recovery={
            "active": True,
            "prerequisite_satisfied": False,
            "blocked_tool": "apply_edit_plan",
            "forbidden_actions": ["apply_edit_plan"],
            "missing_content_paths": ["a.py"],
            "next_actions": [prerequisite],
        },
    )
    assert state["required_transition"] == "PREREQUISITE_REQUIRED"
    assert state["allowed_next_actions"] == ["query_project"]
    assert state["blocked_actions"] == ["apply_edit_plan"]
    assert state["missing_evidence"] == ["a.py"]
    assert state["prerequisite_actions"][0]["queries"][0]["operation"] == "read_range"

    verify = build_action_loop_state(
        pending_verification={
            "missing_condition": "checkpoint HEAD must resolve",
            "required_action": "run_command",
            "supported_verify_actions": ["run_command"],
        },
    )
    assert verify["phase"] == "VERIFY"
    assert verify["required_transition"] == "VERIFY_REQUIRED"
    assert verify["allowed_next_actions"] == ["run_command"]


def validate_progress_state_fields_round_trip() -> None:
    with tempfile.TemporaryDirectory(prefix="action-loop-progress-") as temp:
        root = Path(temp)
        initialize_progress("TASK-1", request_id="REQ-1", goal="test", root=root)
        payload = canonical_progress(
            runtime_state_ref="ALS-123",
            next_phase="DISCOVERY",
            selected_action="query_project",
        )
        record_model_progress(
            "TASK-1", payload, request_id="REQ-1", goal="test", round_id=1, root=root,
        )
        ledger = read_progress("TASK-1", root=root)
        assert ledger is not None
        assert ledger.runtime_state_ref == "ALS-123"
        assert ledger.next_phase == "DISCOVERY"
        assert ledger.selected_action == "query_project"


def validate_runtime_binding_avoids_field_repair() -> None:
    with tempfile.TemporaryDirectory(prefix="action-loop-bind-") as temp:
        loop = WebAgentProtocolLoop(temp, lambda *_args: "")
        loop.action_loop_state = build_action_loop_state()
        progress = canonical_progress()
        action = {"tool": "query_project", "action_id": "Q-1", "project_root": temp, "queries": []}
        diagnostic = loop._normalize_action_loop_decision(progress, [action])
        assert diagnostic is None
        assert progress["runtime_state_ref"] == loop.action_loop_state["state_id"]
        assert progress["next_phase"] == "DISCOVERY"
        assert progress["selected_action"] == "query_project"

        stale = canonical_progress(
            runtime_state_ref="ALS-STALE", next_phase="TERMINAL",
            selected_action="wrong-action",
        )
        diagnostic = loop._normalize_action_loop_decision(stale, [action])
        assert diagnostic is None
        assert stale["runtime_state_ref"] == loop.action_loop_state["state_id"]
        assert stale["next_phase"] == "DISCOVERY"
        assert stale["selected_action"] == "query_project"

        loop.action_loop_state = build_action_loop_state(
            action=action,
            result='{"status":"READY","results":[{"status":"OK","matches":[]}]}',
            execution_status="SUCCEEDED",
        )
        repeated = canonical_progress()
        diagnostic = loop._normalize_action_loop_decision(repeated, [action])
        assert diagnostic and diagnostic["reason"] == "blocked_semantic_action_repeated"


def validate_action_scoped_terminal_evidence() -> None:
    loop = WebAgentProtocolLoop(".", lambda *_args: "")
    read_action = {"tool": "read_file", "action_id": "A-READ", "path": "a.txt"}
    loop.action_ledger["A-READ"] = {
        "action": read_action,
        "result": "bounded content",
        "execution_status": "SUCCEEDED",
        "verification_status": "UNKNOWN",
    }
    loop.action_result_ledger["A-READ"] = {
        "tool": "read_file",
        "execution_status": "SUCCEEDED",
        "verification_status": "UNKNOWN",
        "effective_verification_status": "UNKNOWN",
        "evidence_state": "AVAILABLE",
    }
    assert loop._terminal_evidence_verdict(["A-READ"]) == "PASS"

    write_action = {"tool": "write_file", "action_id": "A-WRITE", "path": "a.txt", "content": "x"}
    loop.action_ledger["A-WRITE"] = {
        "action": write_action,
        "result": "written",
        "execution_status": "SUCCEEDED",
        "verification_status": "UNKNOWN",
    }
    loop.action_result_ledger["A-WRITE"] = {
        "tool": "write_file",
        "execution_status": "SUCCEEDED",
        "verification_status": "UNKNOWN",
        "effective_verification_status": "UNKNOWN",
        "evidence_state": "UNVERIFIED",
    }
    assert loop._terminal_evidence_verdict(["A-WRITE"]) == "UNVERIFIED"

    loop.action_result_ledger["A-WRITE"]["execution_status"] = "FAILED"
    assert loop._terminal_evidence_verdict(["A-WRITE"]) == "FAIL"


def validate_string_progress_wrapper_is_local_normalization() -> None:
    existing = SimpleNamespace(
        base_evaluation="base", total_steps=2, current_step=1,
        steps=[
            {"step": 1, "desc": "read", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "finish", "status": "PENDING"},
        ],
        next_action="query_project", completion_contract={
            "success": ["verified"], "failure": ["failed"],
            "in_progress": ["evidence pending"], "interrupted": ["blocked"],
        },
        decision="CONTINUE", outcome="PENDING",
        matched_condition="evidence pending", evidence_refs=["REQUEST_ACCEPTED"],
    )
    normalized, diagnostic = WebAgentProtocolLoop._normalize_report_progress_envelope(
        {"tool": "report_progress", "action_id": "P-2", "progress": "still collecting evidence"},
        existing,
    )
    assert diagnostic is None
    assert normalized["current_focus"] == "still collecting evidence"
    assert normalized["decision"] == "CONTINUE"
    assert normalized["completion_contract"] == existing.completion_contract


def main() -> int:
    validate_state_classification()
    validate_action_phase_and_lifecycle_contract()
    validate_prerequisite_and_verification_routes()
    validate_progress_state_fields_round_trip()
    validate_runtime_binding_avoids_field_repair()
    validate_action_scoped_terminal_evidence()
    validate_string_progress_wrapper_is_local_normalization()
    print("ACTION_LOOP_RUNTIME_STATE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
