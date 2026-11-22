#!/usr/bin/env python3
"""P0-P3 action provenance, negative fact, dynamic fields and decision gate."""
from __future__ import annotations

import json
import unittest

from agent_core.action_loop_state import (
    build_action_loop_state,
    classify_evidence_state,
)
from agent_core.pr_bcd_observation_evidence import observation_receipt
from agent_core.progress_reply_contract import dynamic_progress_contract
from agent_core.runtime_fact_ledger import (
    build_action_provenance,
    decision_action_consistency,
    planner_projection,
)
from agent_core.tool_result_contract import attach_runtime_result, encode_result

REQUEST = "RR-TEST-A"
EPOCH = "EPOCH-TEST-A"
ROOT = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
ACTION = {"tool": "inspect_directory", "action_id": "inspect-1", "paths": [ROOT]}


def make_trace(original=ACTION, canonical=ACTION, dispatched=ACTION,
               result=None, request=REQUEST, epoch=EPOCH):
    if result is None and dispatched is not None:
        result = encode_result(
            "SUCCEEDED", "DIRECTORY_ABSENCE_OBSERVED",
            tool="inspect_directory", executed=True,
        )
    return build_action_provenance(
        original, canonical, dispatched, result,
        request_id=request, task_epoch=epoch, round_id=1,
    )


class FactLedgerP0Test(unittest.TestCase):
    def test_absent_directory_is_completed_negative_fact_not_goal_pass(self):
        trace = make_trace()
        self.assertEqual(trace["original_action"], ACTION)
        self.assertEqual(trace["canonical_action"], ACTION)
        self.assertEqual(trace["dispatched_action"], ACTION)
        self.assertEqual(trace["actual_result"]["status"], "SUCCEEDED")
        self.assertTrue(trace["actual_result"]["executed"])
        self.assertEqual(trace["fact"]["predicate"], "directory.exists")
        self.assertIs(trace["fact"]["value"], False)
        self.assertIs(trace["fact"]["goal_verified"], False)
        visible = planner_projection(trace, request_id=REQUEST, task_epoch=EPOCH)
        self.assertEqual(visible["fact"]["value"], False)
        self.assertIsNone(planner_projection(trace, request_id="OTHER", task_epoch=EPOCH))
        self.assertIsNone(planner_projection(trace, request_id=REQUEST, task_epoch="OTHER"))

    def test_failed_directory_list_is_not_automatically_a_fact(self):
        action = {"tool": "list_directory", "action_id": "list-1", "path": ROOT}
        result = encode_result(
            "FAILED", "DIRECTORY_NOT_FOUND",
            tool="list_directory", executed=False,
        )
        trace = make_trace(action, action, action, result)
        self.assertEqual(trace["actual_result"]["reason"], "DIRECTORY_NOT_FOUND")
        self.assertIs(trace["actual_result"]["executed"], False)
        self.assertIsNone(trace["fact"])

    def test_canonicalization_and_dispatch_remain_separate(self):
        original = {"tool": "inspect_directory", "action_id": "inspect-1",
                    "paths": [ROOT], "timeout": "30"}
        canonical = {**original, "timeout": 30}
        trace = make_trace(original, canonical, canonical)
        self.assertEqual(trace["original_action"]["timeout"], "30")
        self.assertEqual(trace["canonical_action"]["timeout"], 30)
        self.assertEqual(trace["dispatched_action"]["timeout"], 30)
        original["timeout"] = "99"
        self.assertEqual(trace["original_action"]["timeout"], "30")

    def test_no_execution_has_null_result_not_fabricated_unknown(self):
        trace = make_trace(dispatched=None, result=None)
        self.assertEqual(trace["admission_status"], "NOT_DISPATCHED")
        self.assertIsNone(trace["dispatched_action"])
        self.assertIsNone(trace["actual_result"])
        self.assertIsNone(trace["fact"])
        with self.assertRaises(ValueError):
            make_trace(dispatched=None, result="fake")

    def test_identity_mismatch_blocks_provenance(self):
        bad = dict(ACTION, action_id="unrelated")
        with self.assertRaises(ValueError):
            make_trace(original=bad)

    def test_command_and_query_contents_do_not_leak_to_planner_snapshot(self):
        secret = "set-variable TOKEN 'secret-should-not-appear'"
        a = {"tool": "run_command", "action_id": "cmd-1",
             "command": secret, "working_directory": ROOT,
             "queries": [{"operation": "search_text", "query": "hidden"}]}
        trace = make_trace(a, a, a, encode_result(
            "SUCCEEDED", "COMMAND_EXITED", executed=True, tool="run_command",
        ))
        public = planner_projection(trace, request_id=REQUEST, task_epoch=EPOCH)
        serialized = json.dumps(public)
        self.assertNotIn("secret-should-not-appear", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertTrue(public["dispatched_action"]["command_redacted"])
        self.assertEqual(public["dispatched_action"]["query_count"], 1)

    def test_result_identity_mismatch_never_produces_native_fact(self):
        forged = attach_runtime_result(
            '{"paths":[{"exists":false}]}',
            "SUCCEEDED", "DIRECTORY_ABSENCE_OBSERVED",
            tool="inspect_directory", executed=True,
            action_id="other-action",
        )
        trace = make_trace(result=forged)
        self.assertEqual(trace["actual_result"]["status"], "UNKNOWN")
        self.assertEqual(trace["actual_result"]["reason"], "RESULT_IDENTITY_MISMATCH")
        self.assertIsNone(trace["fact"])

    def test_native_observation_payload_visible_with_result_reason(self):
        result = attach_runtime_result(
            json.dumps({"paths": [{"path": ROOT, "exists": False, "authorized": True}]}),
            "SUCCEEDED", "DIRECTORY_ABSENCE_OBSERVED",
            tool="inspect_directory", executed=True,
        )
        visible = planner_projection(
            make_trace(result=result), request_id=REQUEST, task_epoch=EPOCH,
        )
        observed = visible["actual_result"]["observed_data"]
        self.assertEqual(observed["paths"][0]["path"], ROOT)
        self.assertIs(observed["paths"][0]["exists"], False)
        self.assertIs(visible["fact"]["value"], False)

    def test_bounded_safe_exact_command_visible(self):
        action = {"tool": "run_command", "action_id": "cmd-1",
                  "operation": "INSPECT", "command": "git status --short",
                  "working_directory": ROOT}
        trace = make_trace(action, action, action, encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
            data={"exit_code": 0, "process_state": "EXITED"},
        ))
        visible = planner_projection(trace, request_id=REQUEST, task_epoch=EPOCH)
        self.assertEqual(visible["dispatched_action"]["command"], "git status --short")
        self.assertEqual(visible["actual_result"]["observed_data"]["exit_code"], 0)

    def test_native_present_directory_is_a_fact_only_when_bool_observed(self):
        result = attach_runtime_result(
            json.dumps({"paths": [{"path": ROOT, "exists": True}]}),
            "SUCCEEDED", "DIRECTORY_INSPECTED",
            tool="inspect_directory", executed=True,
        )
        trace = make_trace(result=result)
        self.assertEqual(trace["fact"]["value"], True)
        self.assertIs(trace["fact"]["goal_verified"], False)


class NegativeObservationP1Test(unittest.TestCase):
    def test_negative_inspection_is_not_accepted_as_positive_proof(self):
        result = encode_result(
            "SUCCEEDED", "DIRECTORY_ABSENCE_OBSERVED",
            executed=True, tool="inspect_directory",
        )
        receipt = observation_receipt(
            ACTION, result,
            {"execution_status": "SUCCEEDED", "result_status": "SUCCEEDED",
             "executed": True, "evidence_status": "ACTIVE"},
        )
        self.assertIs(receipt["admitted"], False)
        self.assertEqual(receipt["reason"], "OBSERVED_DIRECTORY_ABSENT")

    def test_absence_requires_replan_without_claiming_terminal_success(self):
        result = encode_result(
            "SUCCEEDED", "DIRECTORY_ABSENCE_OBSERVED",
            executed=True, tool="inspect_directory",
        )
        state, _ = classify_evidence_state(
            ACTION, result, "SUCCEEDED", "UNKNOWN",
        )
        self.assertEqual(state, "OBSERVED_NEGATIVE")
        loop = build_action_loop_state(
            progress={"decision": "CONTINUE"}, action=ACTION,
            result=result, execution_status="SUCCEEDED",
            verification_status="UNKNOWN",
        )
        self.assertEqual(loop["required_transition"], "REPLAN_REQUIRED")
        self.assertNotEqual(loop["required_transition"], "TERMINAL")


class DynamicContractP2Test(unittest.TestCase):
    def test_initial_full_and_inherited_patch(self):
        full = dynamic_progress_contract(None, runtime_transition="CONTINUE_PLAN")
        self.assertEqual(full["mode"], "FULL")
        self.assertIn("completion_contract", full["required"])

        class Accepted:
            total_steps = 2
            steps = [{"step": 1, "status": "IN_PROGRESS"}]
            completion_contract = {"success": ["verified"]}
            base_evaluation = "observed source"
        patch = dynamic_progress_contract(Accepted(), runtime_transition="WAIT_RUNTIME",
                                          action_history_count=1)
        self.assertEqual(patch["mode"], "PATCH")
        self.assertIn("completion_contract", patch["inherited_if_omitted"])
        self.assertIn("evidence_refs", patch["required"])
        self.assertTrue(patch["runtime_wait_allowed"])
        self.assertTrue(patch["full_runtime_admission_required"])

    def test_continue_requires_action_unless_runtime_wait(self):
        self.assertEqual(decision_action_consistency(
            "CONTINUE", [], runtime_transition="REPLAN_REQUIRED"), "ACTION_REQUIRED")
        self.assertEqual(decision_action_consistency(
            "CONTINUE", [], runtime_transition="WAIT_RUNTIME"), "RUNTIME_WAIT")
        self.assertEqual(decision_action_consistency(
            "CONTINUE", [ACTION], runtime_transition="CONTINUE_PLAN"), "ACTION_PRESENT")
        self.assertEqual(decision_action_consistency(
            "COMPLETE", [], runtime_transition="CONTINUE_PLAN"), "NOT_CONTINUE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
