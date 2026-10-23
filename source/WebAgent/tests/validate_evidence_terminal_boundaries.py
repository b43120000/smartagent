"""Focused regression checks for completed UNKNOWN fact and terminal boundaries.

Run: PYTHONPATH=source python source/WebAgent/tests/validate_evidence_terminal_boundaries.py
Full Telegram / Web UI end-to-end acceptance must still be checked on Windows.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_core.action_loop_state import (
    action_lifecycle, build_action_loop_state, classify_evidence_state,
)
from agent_core.context_acquisition import context_capabilities, context_interrupt_allowed
from agent_core.tool_result_contract import (
    completed_unknown_fact_observation, decode_result,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext


MISSING_CLAIM = {"field": "file.exists", "operator": "equals", "expected": False}


def check():
    with tempfile.TemporaryDirectory(prefix="smartagent-evidence-terminal-") as tmp:
        root = Path(tmp).resolve()
        tools = WebAgentToolContext(root)
        tools.begin_run("BOUNDARY-REQUEST", "Verify " + str(root), [str(root)])

        # The parent is missing, so stat is completed but file existence
        # cannot be confirmed. The canonical tool must not coerce to False.
        missing_parent = root / "__missing_parent__" / "probe.txt"
        assert not missing_parent.parent.exists()
        action = {"tool": "observe_fact", "action_id": "BOUNDARY-UNKNOWN",
                  "path": str(missing_parent), "condition_id": "predeclared",
                  "predicate": MISSING_CLAIM}
        raw = tools.execute(action)
        outcome = decode_result(raw)
        assert outcome and outcome["status"] == "UNKNOWN", outcome
        assert outcome["executed"] is True, outcome
        assert outcome["data"]["execution_finality"] == "COMPLETED"
        assert outcome["data"]["verification_status"] == "UNKNOWN"
        assert outcome["data"]["terminal_eligible"] is False
        assert completed_unknown_fact_observation(outcome, action["action_id"])

        result = json.loads(raw)
        fact = result["observation"]
        assert fact["observation_status"] == "UNKNOWN" and "value" not in fact
        assert result["raw_evidence"]["result"] == "UNKNOWN"
        assert result["evaluation"]["comparison"] == "NOT_EVALUATED"
        assert result["evaluation"]["verification_status"] == "UNKNOWN"

        evidence_state, _ = classify_evidence_state(
            action, raw, "UNKNOWN", "UNKNOWN", "UNKNOWN")
        assert evidence_state == "OBSERVATION_UNKNOWN", evidence_state
        assert action_lifecycle(action, "UNKNOWN", "UNKNOWN",
                                evidence_state, "UNKNOWN") == "OBSERVATION_UNKNOWN"
        state = build_action_loop_state(
            action=action, result=raw, execution_status="UNKNOWN",
            verification_status="UNKNOWN", expectation_status="UNKNOWN",
            action_records=[{"action": action, "result": raw,
                             "execution_status": "UNKNOWN",
                             "verification_status": "UNKNOWN",
                             "expectation_status": "UNKNOWN"}],
            available_tools=["observe_fact", "final_response"])
        assert state["execution_finality"] == "COMPLETED", state
        assert state["required_transition"] == "REPLAN_REQUIRED", state
        assert state["blockers"][0]["reason"] == "FACT_OBSERVATION_INDETERMINATE"
        assert state["action_states"][0]["execution_finality"] == "COMPLETED"
        assert len(state["blockers"]) == 1, state

        records = {action["action_id"]: {
            "tool": "observe_fact", "execution_status": "UNKNOWN",
            "result": raw, "evidence_status": "ACTIVE"}}
        caps = context_capabilities([str(root)], ["observe_fact", "list_directory"], records)
        blocked = {"kind": "BLOCKED", "detail": "One canonical probe permitted.",
                   "blocking_action_id": action["action_id"]}
        assert context_interrupt_allowed(blocked, caps), caps
        assert not context_interrupt_allowed({**blocked, "blocking_action_id": "UNRELATED"}, caps)
        records[action["action_id"]]["evidence_status"] = "STALE"
        assert not context_interrupt_allowed(blocked, context_capabilities(
            [str(root)], ["observe_fact"], records))

        # Untrusted or incomplete UNKNOWN outcomes are not completed-fact
        # receipts and must retain the older execution-pending safeguards.
        forged = dict(outcome)
        forged["tool"] = "run_command"
        assert not completed_unknown_fact_observation(forged, action["action_id"])
        forged = {**outcome, "data": {**outcome["data"],
                                    "observations": [{**fact, "value": False}]}}
        assert not completed_unknown_fact_observation(forged, action["action_id"])
        unfinished = {"tool": "run_command", "action_id": "STILL-RUNNING"}
        unknown_state, _ = classify_evidence_state(
            unfinished, "", "UNKNOWN", "UNKNOWN", "")
        assert unknown_state == "MISSING"
        pending = build_action_loop_state(action=unfinished, result="",
            execution_status="RUNNING", verification_status="UNKNOWN")
        assert pending["required_transition"] == "WAIT_RUNTIME", pending

        # Confirmed predicate FAIL stays FAIL even with SUCCEEDED execution.
        present = root / "present.txt"
        present.write_text("exists", encoding="utf-8")
        negative = {**action, "action_id": "BOUNDARY-FAIL", "path": str(present)}
        failed_raw = tools.execute(negative)
        failed = decode_result(failed_raw)
        assert failed and failed["status"] == "SUCCEEDED"
        assert failed["data"]["verification_status"] == "FAIL"
        assert failed["data"]["terminal_eligible"] is False
        verdict = WebAgentProtocolLoop._classify_action_evidence(negative, failed_raw)
        assert verdict["execution_status"] == "SUCCEEDED"
        assert verdict["verification_status"] == "FAIL"
        assert verdict["terminal_eligible"] is False

        loop = WebAgentProtocolLoop(root, lambda *_a: "")
        loop.run_id = "BOUNDARY-REQUEST"
        loop.task_id = "BOUNDARY-TASK"
        loop.task_epoch = "BOUNDARY-EPOCH"
        loop.turn_id = 2
        loop.accepted_ack_ids = ["ACK-CONFIRMED"]
        loop.action_ledger[negative["action_id"]] = {
            "action": negative, "result": failed_raw, **verdict}
        loop.action_result_ledger[negative["action_id"]] = {
            **verdict, "condition_id": "predeclared",
            "request_id": loop.run_id, "task_epoch": loop.task_epoch,
            "evidence_status": "ACTIVE"}
        assert loop._terminal_evidence_verdict([negative["action_id"]]) == "FAIL"
        loop.progress_ledger = SimpleNamespace(
            decision="COMPLETE", outcome="FAILED", matched_condition="failure",
            matched_condition_id="COND-FAILURE-TEST",
            evidence_refs=[negative["action_id"]])
        receipt = loop._accepted_terminal_receipt(runtime_state="COMPLETED")
        assert receipt["admission_status"] == "ACCEPTED"
        assert receipt["task_outcome"] == "FAILED"
        assert receipt["evidence_verdict"] == "FAIL"
        assert receipt["matched_condition_id"] == "COND-FAILURE-TEST"
        assert loop.terminal_acceptance["receipt_id"] == receipt["receipt_id"]

        # Scope rejection is never a successful negative observation.
        forbidden = {**action, "action_id": "BOUNDARY-REJECTED",
                     "path": str(root.parent / "outside.txt")}
        rejected = decode_result(tools.execute(forbidden))
        assert rejected["status"] == "REJECTED" and not rejected["executed"]

    print("EVIDENCE_TERMINAL_BOUNDARIES_PASS")


if __name__ == "__main__":
    check()
