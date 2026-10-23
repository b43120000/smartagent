"""Runtime Expected Outcome Contract integration and fail-closed regression.

Run: PYTHONPATH=source python source/WebAgent/tests/validate_vpcs_expected_outcome.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.project_access import build_project_capsule
from agent_core.project_sync import load_current_project_snapshot
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop


STALE_CONDITION = "Old pinned reference must report project_snapshot_stale"
CROSS_CONDITION = "Old reference on a new snapshot must report context_ref_snapshot_mismatch"


def query(agent, root: Path, operation: dict, **fields):
    return agent.execute({
        "tool": "query_project",
        "project_root": str(root),
        "queries": [operation],
        **fields,
    })


def unpack(raw):
    payload = json.loads(raw)
    outcome = decode_result(raw)
    assert isinstance(outcome, dict), raw
    return payload, outcome


def require_terminal(action, raw, outcome, should_pass: bool):
    loop = WebAgentProtocolLoop(
        Path(action["project_root"]), lambda *_args, **_kwargs: "",
    )
    loop.run_id = "VPCS-EXPECTATION-RUN"
    loop.task_epoch = "VPCS-EXPECTATION-EPOCH"
    evidence = loop._classify_action_evidence(action, raw)
    assert evidence["expectation_status"] == ("PASS" if should_pass else "FAIL"), evidence
    assert evidence["terminal_eligible"] is should_pass, evidence
    action_id = action["action_id"]
    loop.action_ledger = {action_id: {"action": action, "result": raw, **evidence}}
    loop.action_result_ledger = {action_id: {
        **evidence,
        "request_id": loop.run_id,
        "task_epoch": loop.task_epoch,
        "evidence_status": "ACTIVE",
    }}
    verdict = loop._terminal_evidence_verdict([action_id])
    assert verdict == ("PASS" if should_pass else "FAIL"), (verdict, evidence)
    return verdict


def run():
    with tempfile.TemporaryDirectory(prefix="vpcs-expected-query-") as directory:
        root = Path(directory).resolve()
        file = root / "sample.py"
        file.write_text("def answer():\n    return 42\n", encoding="utf-8")
        ctx = WebAgentToolContext(root, interface_name="remote")
        ctx.begin_run("VPCS-EXPECTED-TEST", "test project " + str(root), [str(root)])
        old = build_project_capsule(root)
        snapshot_a = old["snapshot_id"]

        meta, _ = unpack(query(ctx, root, {
            "operation": "get_file_metadata", "path": "sample.py",
        }, snapshot_id=snapshot_a))
        ref_a = meta["results"][0]["context_ref"]
        file.write_text("def answer():\n    return 99\n", encoding="utf-8")

        expected_stale = {
            "query_index": 1,
            "status": "ERROR",
            "error": "project_snapshot_stale:sample.py",
        }
        action = {
            "tool": "query_project",
            "action_id": "VPCS-STALE",
            "project_root": str(root),
            "queries": [{"operation": "read_context_ref", "context_ref": ref_a}],
            "snapshot_id": snapshot_a,
            "condition_id": STALE_CONDITION,
            "expected_outcome": expected_stale,
        }
        valid, diagnostic = validate_tool_envelope(action)
        assert valid, diagnostic

        raw = ctx.execute(action)
        body, wrapper = unpack(raw)
        assert wrapper["status"] == "SUCCEEDED" and wrapper["executed"], wrapper
        assert body["status"] == "READY" and body["results"][0]["status"] == "ERROR", body
        assert body["results"][0]["error"] == "project_snapshot_stale:sample.py", body
        assert wrapper["data"]["expectation_status"] == "PASS", wrapper
        assert wrapper["data"]["verification_status"] == "PASS", wrapper
        assert wrapper["data"]["observation_status"] == "ERROR", wrapper
        assert wrapper["data"]["observation_error"] == expected_stale["error"], wrapper
        assert body["index_context"]["recovery_action"] == "PINNED_SNAPSHOT_NO_REBUILD", body
        assert load_current_project_snapshot(root)["snapshot_id"] == snapshot_a
        require_terminal(action, raw, wrapper, True)

        # An undeclared negative read preserves legacy FAILED semantics.
        unmarked = {k: v for k, v in action.items() if k not in {"condition_id", "expected_outcome"}}
        unmarked["action_id"] = "VPCS-UNMARKED"
        base_payload, base_result = unpack(ctx.execute(unmarked))
        assert base_payload["results"][0]["error"] == expected_stale["error"], base_payload
        assert base_result["status"] == "FAILED", base_result

        # An incorrect expected error remains failed even though the read ran.
        bad_action = dict(action, action_id="VPCS-WRONG-EXPECTED",
                          expected_outcome={**expected_stale, "error": "context_ref_snapshot_mismatch"})
        bad_raw = ctx.execute(bad_action)
        bad_body, bad_result = unpack(bad_raw)
        assert bad_body["results"][0]["error"] == expected_stale["error"], bad_body
        assert bad_result["status"] == "FAILED", bad_result
        assert bad_result["data"]["expectation_status"] == "FAIL", bad_result
        require_terminal(bad_action, bad_raw, bad_result, False)

        # A post-hoc expectation on the same action id is a different
        # signature, never an allowed exactly-once cache replay.
        from agent_core.action_loop_state import semantic_action_signature
        assert semantic_action_signature(action) != semantic_action_signature(unmarked)

        # Unsupported or underspecified expectations fail before execution.
        for invalid in (
            dict(action, expected_outcome={"query_index": 1, "status": "ERROR", "error": "permission_denied"}),
            dict(action, expected_outcome={"query_index": True, **{k:v for k,v in expected_stale.items() if k != "query_index"}}),
            dict(action, queries=[{"operation": "search_text", "query": "answer"}]),
            {k:v for k,v in action.items() if k != "condition_id"},
            dict(action, expected_outcome={"query_index": 1, "status": "ERROR", "error": "project_snapshot_stale:"}),
        ):
            permitted, _ = validate_tool_envelope(invalid)
            assert not permitted, invalid
            rejected = decode_result(ctx.execute(invalid))
            assert rejected and rejected["status"] == "REJECTED" and not rejected["executed"], rejected

        # Explicit sync is still the only publication point for the new
        # snapshot after a pinned stale read.
        snapshot_b = build_project_capsule(root)["snapshot_id"]
        assert snapshot_b != snapshot_a
        cross = {
            **action,
            "action_id": "VPCS-CROSS",
            "snapshot_id": snapshot_b,
            "condition_id": CROSS_CONDITION,
            "expected_outcome": {
                "query_index": 1,
                "status": "ERROR",
                "error": "context_ref_snapshot_mismatch",
            },
        }
        cross_raw = ctx.execute(cross)
        cross_payload, cross_result = unpack(cross_raw)
        assert cross_payload["results"][0]["error"] == "context_ref_snapshot_mismatch", cross_payload
        assert cross_result["status"] == "SUCCEEDED", cross_result
        require_terminal(cross, cross_raw, cross_result, True)

        # Positive reads are unchanged.
        new_meta, _ = unpack(query(ctx, root, {
            "operation": "get_file_metadata", "path": "sample.py",
        }, snapshot_id=snapshot_b))
        fresh_ref = new_meta["results"][0]["context_ref"]
        fresh_payload, fresh_result = unpack(query(ctx, root, {
            "operation": "read_context_ref", "context_ref": fresh_ref,
        }, snapshot_id=snapshot_b))
        assert fresh_result["status"] == "SUCCEEDED", fresh_result
        assert "return 99" in fresh_payload["results"][0]["content"], fresh_payload
        assert fresh_payload["results"][0]["file_sha256"] == new_meta["results"][0]["file"]["sha256"]

    print("VPCS_EXPECTED_OUTCOME_PASS")


def validate_protocol_terminal():
    """Use real WebAgent admission, execution, ledger and terminal verdict."""
    from WebAgent.tests.validate_progress_terminal_lifecycle import response

    with tempfile.TemporaryDirectory(prefix="vpcs-expect-protocol-") as directory:
        root = Path(directory).resolve()
        source = root / "sample.py"
        source.write_text("def answer():\n    return 42\n", encoding="utf-8")
        capsule = build_project_capsule(root)
        seed = query(WebAgentToolContext(root), root, {
            "operation": "get_file_metadata", "path": "sample.py",
        }, snapshot_id=capsule["snapshot_id"])
        ref = json.loads(seed)["results"][0]["context_ref"]
        source.write_text("def answer():\n    return 99\n", encoding="utf-8")
        turns = []
        contract = {
            "success": [STALE_CONDITION],
            "failure": ["Pinned reference unexpectedly returned source or another error"],
            "in_progress": ["Pinned reference has not been checked"],
            "interrupted": ["Local query runtime unavailable"],
        }
        def planner(prompt, expected, attachments):
            turns.append(prompt)
            if len(turns) == 1:
                return response({
                    "tool": "report_progress", "action_id": "P-VPCS-START",
                    "base_evaluation": "Validate expected stale context without changing source",
                    "total_steps": 1, "current_step": 0.5,
                    "steps": [{"step": 1, "desc": "Check immutable reference", "status": "IN_PROGRESS"}],
                    "current_focus": "Check pinned read", "next_action": "query_project",
                    "completion_contract": contract, "decision": "CONTINUE",
                    "outcome": "PENDING", "matched_condition": contract["in_progress"][0],
                    "evidence_refs": ["REQUEST_ACCEPTED"],
                    "decision_reason": "No observed result yet",
                }, {
                    "tool": "query_project", "action_id": "VPCS-PROTOCOL-NEG",
                    "project_root": str(root),
                    "snapshot_id": capsule["snapshot_id"],
                    "queries": [{"operation": "read_context_ref", "context_ref": ref}],
                    "expected_outcome": {
                        "query_index": 1, "status": "ERROR",
                        "error": "project_snapshot_stale:sample.py",
                    },
                    "condition_id": STALE_CONDITION,
                })
            assert len(turns) == 2, prompt
            assert "VPCS-PROTOCOL-NEG" in loop.action_result_ledger
            assert loop.action_result_ledger["VPCS-PROTOCOL-NEG"]["expectation_status"] == "PASS"
            return response({
                "tool": "report_progress", "action_id": "P-VPCS-END",
                "decision": "COMPLETE", "outcome": "SUCCESS",
                "current_step": 1,
                "steps": [{"step": 1, "desc": "Check immutable reference", "status": "COMPLETED"}],
                "current_focus": "Negative assertion observed",
                "next_action": "final_response",
                "matched_condition": STALE_CONDITION,
                "evidence_refs": ["VPCS-PROTOCOL-NEG"],
                "decision_reason": "Runtime accepted the exact predeclared stale error",
            }, {
                "tool": "final_response", "action_id": "FINAL-VPCS-EXPECTED",
                "content": "VPCS_EXPECTED_TERMINAL_PASS",
            })

        loop = WebAgentProtocolLoop(root, planner, progress_root=root, max_turns=8)
        final = loop.run("Check pinned query negative expectation\n" + str(root),
                         trusted_authorized_paths=[str(root)])
        assert final == "VPCS_EXPECTED_TERMINAL_PASS", final
        assert loop.terminal_outcome == "SUCCESS", loop.terminal_outcome
        assert len(turns) == 2, turns
        assert load_current_project_snapshot(root)["snapshot_id"] == capsule["snapshot_id"]
        print("VPCS_EXPECTED_PROTOCOL_TERMINAL_PASS")


if __name__ == "__main__":
    run()
    validate_protocol_terminal()
