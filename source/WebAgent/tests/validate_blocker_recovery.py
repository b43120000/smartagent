"""No browser or production task is started by this regression test."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_core.blocker_recovery import BlockerRecovery
from agent_core.capability_recovery import build_result_evidence_to_action_route
from agent_core.action_loop_state import build_action_loop_state
from WebAgent.protocol_loop import WebAgentProtocolLoop


def run():
    tracker = BlockerRecovery()
    context = {"active": True, "reason": "edit_plan_requires_canonical_replan",
               "missing_fields": ["files_to_modify", "verification_commands"]}
    def read(content="abc", end=3, sha="same"):
        return [{"tool": "query_project", "result": json.dumps({
            "project_root": "root", "snapshot_id": "arbitrary-refresh",
            "results": [{"operation": "read_range", "status": "OK", "path": "a.py",
                         "file_sha256": sha, "start_line": 1, "end_line": end,
                         "content": content}]})}]
    assert tracker.observe(context, read()) == "progress"
    assert tracker.observe(context, read()) == "repair_required"
    assert tracker.observe(context, read()) == "stalled"
    assert tracker.observe(context, read(end=4)) == "progress"
    assert tracker.observe(context, read(sha="changed")) == "progress"
    assert tracker.observe({**context, "missing_fields": ["files_to_modify"]}, read(sha="changed")) == "progress"
    assert tracker.observe({"active": False}, []) == "inactive"
    assert tracker.observe(context, read()) == "progress"
    # Actual source content changes, including previously clipped content, count as progress.
    assert tracker.observe(context, read(content="abcd")) == "progress"
    route = build_result_evidence_to_action_route(
        {"tool": "validate_edit_plan", "workspace": "root", "plan": {}},
        json.dumps({"status": "INVALID_PLAN", "reason": "MISSING_FIELDS",
                    "missing_fields": ["base_snapshot_id", "files_to_modify"]}))
    route = build_result_evidence_to_action_route(
        {"tool": "project_sync", "project_root": "root"},
        json.dumps({"status": "READY", "snapshot_id": "current"}), prior_context=route)
    assert route["canonical_action"]["plan"]["base_snapshot_id"] == "current"
    assert route["missing_fields"] == ["files_to_modify"]
    assert not route["prerequisite_satisfied"]
    assert [item["tool"] for item in route["next_actions"]] == ["query_project"]
    # Failed/empty reads must not release the acquisition gate.
    failed = build_result_evidence_to_action_route(
        {"tool": "query_project"}, json.dumps({"status": "REJECTED"}), prior_context=route)
    assert not failed["prerequisite_satisfied"]
    partial = build_result_evidence_to_action_route(
        {"tool": "query_project"}, json.dumps({"status": "READY", "results": [{
            "operation": "read_range", "status": "OK", "file_sha256": "same",
            "content": "1: BEFORE", "next_cursor": 2}]}), prior_context=route)
    assert not partial["prerequisite_satisfied"]
    source = {"status": "READY", "snapshot_id": "current", "results": [{
        "operation": "read_range", "status": "OK", "file_sha256": "same",
        "content": "1: BEFORE", "truncated": True, "next_cursor": None}]}
    route = build_result_evidence_to_action_route(
        {"tool": "query_project"}, json.dumps(source), prior_context=route)
    assert route["next_actions"] == []
    assert route["prerequisite_satisfied"]
    state = build_action_loop_state(
        pending_recovery=route, action={"tool": "query_project"},
        result=json.dumps(source), execution_status="SUCCEEDED")
    assert state["required_transition"] == "REPLAN_REQUIRED"
    assert state["phase"] == "PLAN"
    assert "validate_edit_plan" in state["allowed_next_actions"]
    assert "apply_edit_plan" in state["blocked_actions"]
    # Exercise the real protocol admission method, not only route construction.
    loop = WebAgentProtocolLoop.__new__(WebAgentProtocolLoop)
    loop.action_loop_state = state
    assert loop._normalize_action_loop_decision({}, [{"tool": "validate_edit_plan"}]) is None
    assert loop._normalize_action_loop_decision({}, [{"tool": "apply_edit_plan"}]) is not None
    assert loop._normalize_action_loop_decision({}, [{"tool": "validate_edit_plan"}, {"tool": "apply_edit_plan"}]) is not None
    assert "validate_edit_plan" in tracker.guidance(route)
    assert '"file_sha256": "same"' in tracker.guidance(route)
    cleared = build_result_evidence_to_action_route(
        {"tool": "validate_edit_plan", "plan": {}},
        json.dumps({"status": "VALID"}), prior_context=route)
    assert not cleared["active"]
    after_valid = build_action_loop_state(pending_recovery=cleared,
        action={"tool": "validate_edit_plan"}, result='{"status":"VALID"}',
        execution_status="SUCCEEDED")
    assert "apply_edit_plan" in after_valid["allowed_next_actions"]
    print("PASS: blocker recovery, repeat detection, new evidence, reset, snapshot binding")


if __name__ == "__main__":
    run()
