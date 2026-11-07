"""Git27: durable, typed failure cause across Planner/Prerequisite/Progress.

Fully offline. No remote repository, no mutation, no privileged operations.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_core.failure_causal_receipt import (
    SCHEMA, build_failure_receipt, recent_failure_receipts,
)
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.tool_result_contract import attach_runtime_result
from agent_core.prerequisite_transition_gate import build_prerequisite_transition
from agent_core.unified_contract_transaction import build_transaction

REQ = "RR-G27-CASE-D"
EPOCH = "EPOCH-G27"
ACT = "G27-GIT-READ"
REGISTERED = {"artifact_references": [{
    "kind": "REMOTE_ARTIFACT_REFERENCE",
    "repository": "b43120000/test",
    "candidate_id": "E1-CASE-D",
}]}
PREREQ = {
    "source_requirement": "REQUIRED",
    "next_category": "RESOLVE_REGISTERED_SOURCE_ARTIFACT",
}
REMOTE = {
    "authorization_state": "UNKNOWN", "git_executable_status": "AVAILABLE",
}


def simulated_git_result(stderr, exit_code=128):
    # Trusted typed Tool Result is attached by Runtime, not authored by Planner.
    return attach_runtime_result(
        "[COMMAND_RESULT]\noperation: GIT_INSPECT\n"
        "command: git ls-remote https://github.com/b43120000/test.git HEAD\n"
        "exit_code: 128\nstderr:\n" + stderr +
        "\n[OPERATION_RESULT] {}\nVERIFICATION_STATUS: FAIL",
        "FAILED", "COMMAND_EXITED", tool="run_command", executed=True,
        data={"exit_code": exit_code, "timed_out": False,
              "process_state": "EXITED"},
    )


def ledgers(stderr, *, request_id=REQ, task_epoch=EPOCH):
    result = simulated_git_result(stderr)
    actions = {ACT: {"action": {
        "tool": "run_command", "operation": "GIT_INSPECT",
        "command": "git ls-remote https://github.com/b43120000/test.git HEAD",
        "action_id": ACT,
    }, "result": result}}
    evidence = {ACT: {
        "tool": "run_command", "result_status": "FAILED",
        "execution_status": "FAILED", "request_id": request_id,
        "task_epoch": task_epoch, "evidence_status": "ACTIVE",
    }}
    return actions, evidence


class CausalReceiptTests(unittest.TestCase):
    def test_typed_dns_failure_is_bound_and_persistent(self):
        secret = "CREDENTIAL_SHOULD_NOT_BE_EXPORTED"
        actions, evidence = ledgers(
            "fatal: unable to access ... Could not resolve host: github.com\n"
            + secret)
        receipt = build_failure_receipt(ACT, action_ledger=actions,
            result_ledger=evidence, request_id=REQ, task_epoch=EPOCH)
        self.assertEqual(receipt["schema"], SCHEMA)
        self.assertEqual(receipt["failure"]["category"], "DNS_RESOLUTION_FAILED")
        self.assertTrue(receipt["failure"]["classification_verified"])
        self.assertTrue(receipt["failure"]["stderr_present"])
        self.assertEqual(receipt["execution"]["exit_code"], 128)
        self.assertEqual(receipt["replan"]["next_evaluation"],
                         "CHECK_DNS_WITH_SCOPED_OBSERVATION")
        self.assertNotIn(secret, json.dumps(receipt))
        self.assertNotIn("github.com", json.dumps(receipt))
        self.assertFalse(receipt["authorization_granted"])
        self.assertFalse(receipt["action_dispatched_by_receipt"])
        self.assertEqual(len(recent_failure_receipts(
            action_ledger=actions, result_ledger=evidence,
            request_id=REQ, task_epoch=EPOCH)), 1)

        trans = build_prerequisite_transition(
            artifact_view=REGISTERED, prerequisite_view=PREREQ,
            remote_view=REMOTE, request_id=REQ, task_epoch=EPOCH,
            action_ledger=actions, result_ledger=evidence,
            bound_workspace="/safe/workspace",
        )
        self.assertEqual(trans["stage"], "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED")
        self.assertEqual(trans["latest_probe_failure_category"],
                         "DNS_RESOLUTION_FAILED")
        self.assertEqual(trans["latest_probe_failure"]["action_id"], ACT)
        self.assertEqual(trans["next_failure_evaluation"],
                         "CHECK_DNS_WITH_SCOPED_OBSERVATION")
        self.assertFalse(trans["authorization_granted"])
        shared = build_transaction(
            request_id=REQ, task_epoch=EPOCH, original_goal="Case D",
            round_id=3, phase="REJECTED",
            prerequisite_transition=trans, failure_receipts=[receipt])
        self.assertEqual(shared["failure_causal_receipts"][0]["failure_category"],
                         "DNS_RESOLUTION_FAILED")

    def test_unclassifiable_error_never_guesses_authorization(self):
        actions, evidence = ledgers("fatal: an unexpected remote error")
        receipt = build_failure_receipt(ACT, action_ledger=actions,
            result_ledger=evidence, request_id=REQ, task_epoch=EPOCH)
        self.assertEqual(receipt["failure"]["category"], "UNCLASSIFIED_FAILURE")
        self.assertFalse(receipt["failure"]["classification_verified"])
        self.assertEqual(receipt["replan"]["next_evaluation"],
                         "INSPECT_ACTUAL_GIT_FAILURE_EVIDENCE")

    def test_source_code_and_command_mention_do_not_classify_stderr(self):
        actions, evidence = ledgers("error: unspecified")
        actions[ACT]["action"]["command"] += " Could not resolve host"
        receipt = build_failure_receipt(ACT, action_ledger=actions,
            result_ledger=evidence, request_id=REQ, task_epoch=EPOCH)
        self.assertEqual(receipt["failure"]["category"], "UNCLASSIFIED_FAILURE")

    def test_repo_not_found_is_ambiguous_not_verified_denial(self):
        actions, evidence = ledgers("remote: Repository not found.")
        receipt = build_failure_receipt(ACT, action_ledger=actions,
            result_ledger=evidence, request_id=REQ, task_epoch=EPOCH)
        self.assertEqual(receipt["failure"]["category"],
                         "REPOSITORY_NOT_FOUND_OR_INACCESSIBLE")
        self.assertFalse(receipt["failure"]["classification_verified"])
        self.assertFalse(receipt["authorization_granted"])

    def test_wrong_epoch_or_missing_typed_result_is_not_evidence(self):
        actions, evidence = ledgers("fatal: Could not resolve host")
        self.assertIsNone(build_failure_receipt(
            ACT, action_ledger=actions, result_ledger=evidence,
            request_id=REQ, task_epoch="DIFFERENT"))
        actions[ACT]["result"] = "[PLANNER_REPLY] FAILED"
        self.assertIsNone(build_failure_receipt(
            ACT, action_ledger=actions, result_ledger=evidence,
            request_id=REQ, task_epoch=EPOCH))

    def test_remote_logger_preserves_safe_enum_only(self):
        payload = RemoteRuntimeLog._redact({
            "authorization_state": "UNKNOWN",
            "authorization_reason_code": "ACCESS_NOT_YET_EVALUATED",
            "authorization_granted": False,
            "authorization_evidence_present": False,
            "authorization": "Bearer secret",
            "authorization_evidence_ref": "SECRET_DO_NOT_LEAK",
            "authorization_state_unsafe": "AUTH_TOKEN",
            "token": "SECRET_DO_NOT_LEAK",
        })
        self.assertEqual(payload["authorization_state"], "UNKNOWN")
        self.assertEqual(payload["authorization_reason_code"],
                         "ACCESS_NOT_YET_EVALUATED")
        self.assertIs(payload["authorization_granted"], False)
        self.assertEqual(payload["authorization"], "***REDACTED***")
        self.assertEqual(payload["authorization_evidence_ref"], "***REDACTED***")
        self.assertEqual(payload["token"], "***REDACTED***")
        self.assertEqual(RemoteRuntimeLog._redact({
            "authorization_state": "Bearer secret",
        })["authorization_state"], "***REDACTED***")

    def test_real_loop_progress_fails_before_commit_without_new_action(self):
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        from WebAgent.tests.validate_flexible_progress_admission import initial
        from agent_core.task_progress import initialize_progress, record_model_progress
        with tempfile.TemporaryDirectory(prefix="git27-precommit-") as temp:
            root = Path(temp).resolve()
            loop = WebAgentProtocolLoop(root, lambda *args: "", progress_root=root)
            loop.run_id = loop.task_id = REQ
            loop.task_epoch = EPOCH
            loop.intent_digest = "f" * 64
            loop.progress_ledger = initialize_progress(
                loop.task_id, request_id=REQ,
                goal="Inspect registered Git source", root=root)
            loop.progress_ledger = record_model_progress(
                loop.task_id, initial(), request_id=REQ,
                goal="Inspect registered Git source", round_id=1, root=root)
            actions, evidence = ledgers("fatal: Could not resolve host: github.com")
            loop.action_ledger.update(actions)
            loop.action_result_ledger.update(evidence)
            progress = dict(initial())
            progress["next_action"] = ""
            proposed = [progress, {"tool": "turn_commit", "action_count": 1}]
            _, diagnostics = loop._accept_v8_ack(proposed, {})
            self.assertIn("failed_prerequisite_continue_requires_action",
                          [row.get("reason") for row in diagnostics], diagnostics)
            self.assertEqual(loop.progress_ledger.current_step, 0.5)
            self.assertEqual(loop.action_result_ledger[ACT]["result_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
