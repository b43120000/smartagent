"""Evidence Foundation v1 contract and integration checks.

Run: PYTHONPATH=source python source/WebAgent/tests/validate_runtime_evidence_foundation.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.evidence_foundation import (
    EvidenceSpecError, adapt_project_query, adapt_command_outcome,
    evaluate_predicate, observation, validate_predicate,
)
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext


def verdict(raw):
    payload = json.loads(raw)
    wrapped = decode_result(raw)
    assert wrapped is not None, raw
    return payload, wrapped


def check_terminal(action, raw, passed):
    facts = WebAgentProtocolLoop._classify_action_evidence(action, raw)
    assert facts["verification_status"] == ("PASS" if passed else "FAIL"), facts
    assert facts["terminal_eligible"] is passed, facts
    loop = WebAgentProtocolLoop(Path(action["path"]).parent, lambda *_a: "")
    loop.run_id = "EVIDENCE-INTEGRATION"
    loop.task_epoch = "EVIDENCE-EPOCH"
    action_id = action["action_id"]
    loop.action_ledger[action_id] = {"action":action, "result":raw, **facts}
    loop.action_result_ledger[action_id] = {
        **facts, "request_id":loop.run_id, "task_epoch":loop.task_epoch,
        "evidence_status":"ACTIVE",
    }
    assert loop._terminal_evidence_verdict([action_id]) == ("PASS" if passed else "FAIL")


def run():
    expected_missing = {"field":"file.exists","operator":"equals","expected":False}
    expected_present = {"field":"file.exists","operator":"equals","expected":True}
    assert validate_predicate(expected_missing) == expected_missing
    assert evaluate_predicate(expected_missing, observation("file.exists", False,
        source="filesystem.stat", scope="one"))["verification_status"] == "PASS"
    assert evaluate_predicate(expected_missing, observation("file.exists", True,
        source="filesystem.stat", scope="one"))["verification_status"] == "FAIL"
    unknown = observation("file.exists", source="filesystem.stat", scope="one", status="UNKNOWN")
    assert "value" not in unknown
    assert evaluate_predicate(expected_missing, unknown)["verification_status"] == "UNKNOWN"
    old = observation("file.exists", False, source="filesystem.stat", scope="one")
    old["freshness"] = "STALE"
    assert evaluate_predicate(expected_missing, old)["verification_status"] == "UNKNOWN"
    assert evaluate_predicate(expected_missing, observation("process.exit_code", 0,
        source="run_command", scope="one"))["verification_status"] == "UNKNOWN"

    for spec in (
        {"field":"file.exists","operator":"equals","expected":"false"},
        {"field":"file.exists","operator":"contains","expected":"x"},
        {"field":"file.exists","operator":"eval","expected":False},
        {"field":"process.exit_code","operator":"equals","expected":True},
        {"field":"file.exists","operator":"equals","expected":False,"python":"os.system"},
    ):
        try:
            validate_predicate(spec)
        except EvidenceSpecError:
            pass
        else:
            raise AssertionError(f"Untrusted predicate admitted: {spec}")

    # Adapter results are purely observations and cannot turn stdout or
    # malformed/untrusted tool payloads into confirmed facts.
    assert adapt_project_query({"status":"READY","results":[]}) == []
    assert adapt_project_query({"schema":"PROJECT_ACCESS_QUERY_RESULT_V1",
        "status":"READY","snapshot_id":"S","results":[{"status":"OK"}],
        "response_truncated":True}) == []
    found = adapt_project_query({"schema":"PROJECT_ACCESS_QUERY_RESULT_V1",
        "status":"READY","snapshot_id":"S","project_handle":"H","results":[
            {"operation":"read_context_ref","status":"ERROR","error":"context_ref_snapshot_mismatch"}]})
    assert len(found) == 2 and found[1]["value"] == "ERROR", found
    assert adapt_command_outcome({"tool":"run_command","data":{
        "process_state":"RUNNING","exit_code":2}}) == []
    assert adapt_command_outcome({"tool":"run_command","data":{
        "process_state":"EXITED","exit_code":2}})[0]["value"] == 2
    assert adapt_command_outcome({"tool":"run_command","data":{
        "process_state":"EXITED","exit_code":"2"}}) == []

    with tempfile.TemporaryDirectory(prefix="smartagent-evidence-foundation-") as directory:
        root = Path(directory).resolve()
        ctx = WebAgentToolContext(root)
        ctx.begin_run("EVIDENCE-001", "Inspect authorized project " + str(root), [str(root)])
        ctx.bind_runtime_completion_contract(
            {"success": ["The target file must not exist"]},
            request_id="EVIDENCE-001", task_epoch="EVIDENCE-001",
        )
        missing = root / "not_here.txt"
        base = {
            "tool":"observe_fact", "action_id":"EVIDENCE-MISSING",
            "path":str(missing), "condition_id":"The target file must not exist",
            "predicate":expected_missing,
        }
        legal, diag = validate_tool_envelope(base)
        assert legal, diag
        raw = ctx.execute(base)
        payload, wrapper = verdict(raw)
        assert payload["status"] == "READY" and payload["raw_evidence"]["result"] == "NOT_FOUND", payload
        assert payload["observation"]["value"] is False, payload
        assert wrapper["status"] == "SUCCEEDED" and wrapper["data"]["verification_status"] == "PASS"
        assert wrapper["data"]["expectation_status"] == "PASS", wrapper
        check_terminal(base, raw, True)

        false_claim = {**base, "action_id":"EVIDENCE-WRONG",
                       "predicate":expected_present}
        raw = ctx.execute(false_claim)
        payload, wrapper = verdict(raw)
        assert payload["observation"]["value"] is False
        assert wrapper["status"] == "SUCCEEDED"
        assert wrapper["data"]["verification_status"] == "FAIL"
        check_terminal(false_claim, raw, False)

        # Without a predicate, evidence remains a fact (not an invented PASS).
        informational = {"tool":"observe_fact", "action_id":"EVIDENCE-INFO", "path":str(missing)}
        payload, wrapper = verdict(ctx.execute(informational))
        assert wrapper["status"] == "SUCCEEDED"
        assert wrapper["data"]["verification_status"] == "UNVERIFIED"
        assert payload["observation"]["value"] is False

        present = root / "actual.txt"
        present.write_text("hello", encoding="utf-8")
        present_claim = {**base, "action_id":"EVIDENCE-PRESENT", "path":str(present),
                         "predicate":expected_present}
        payload, wrapper = verdict(ctx.execute(present_claim))
        assert wrapper["status"] == "SUCCEEDED"
        assert payload["observation"]["value"] is True
        assert wrapper["data"]["verification_status"] == "PASS"

        # A parent that does not exist is UNKNOWN rather than proving absence.
        nested = {**base,"action_id":"EVIDENCE-NESTED", "path":str(root/"missing_parent"/"file.txt")}
        nested_result, nested_wrapper = verdict(ctx.execute(nested))
        assert nested_result["observation"]["observation_status"] == "UNKNOWN"
        assert nested_wrapper["status"] == "UNKNOWN", nested_wrapper
        assert nested_wrapper["data"]["verification_status"] == "UNKNOWN"

        # Scope denial is not proof that a file is absent.
        forbidden = {**base, "action_id":"EVIDENCE-FORBIDDEN",
                     "path":str(root.parent/"outside.txt")}
        denied = decode_result(ctx.execute(forbidden))
        assert denied and denied["status"] == "REJECTED", denied
        assert denied["executed"] is False

        # Invalid or late assertion changes are rejected before filesystem probes.
        for invalid in (
            {**base, "action_id":"EVIDENCE-BAD1", "predicate":{"field":"file.exists","operator":"equals","expected":"false"}},
            {k:v for k,v in base.items() if k!="condition_id"},
            {**base, "action_id":"EVIDENCE-BAD3", "path":"relative.txt"},
        ):
            if invalid["path"] == "relative.txt":
                # Absolute-path admission occurs during Runtime scope preflight.
                assert decode_result(ctx.execute(invalid))["status"] == "REJECTED"
            else:
                admitted, _ = validate_tool_envelope(invalid)
                assert not admitted
                assert decode_result(ctx.execute(invalid))["status"] == "REJECTED"

        from WebAgent.protocol_loop import action_signature
        assert action_signature(base) != action_signature(informational)

    print("RUNTIME_EVIDENCE_FOUNDATION_PASS")


if __name__ == "__main__":
    run()
