#!/usr/bin/env python3
"""P0-P3 closed-loop grounding tests, including the Case D regression."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.action_loop_state import build_action_loop_state
from agent_core.action_readiness import tool_readiness_contract
from agent_core.goal_gap_ledger import (
    decision_grounding_diagnostics,
    reconstruct_goal_gaps,
)
from agent_core.progress_reply_contract import dynamic_progress_contract
from agent_core.repair_stagnation import RepairStagnation
from agent_core.tool_result_contract import attach_runtime_result, encode_result


ROOT = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
REQ = "RR-CASE-D"
EPOCH = "EPOCH-CASE-D"


def directory_record(action_id: str, *, exists: bool, result_path: str = ROOT):
    action = {"tool": "inspect_directory", "action_id": action_id, "paths": [ROOT]}
    body = {
        "tool": "inspect_directory",
        "paths": [{
            "path": result_path,
            "exists": exists,
            "authorized": True,
            "inaccessible_count": 0,
            "errors": [] if exists else ["path_not_found"],
        }],
    }
    reason = "DIRECTORY_INSPECTED" if exists else "DIRECTORY_ABSENCE_OBSERVED"
    result = attach_runtime_result(
        json.dumps(body), "SUCCEEDED", reason,
        tool="inspect_directory", action_id=action_id, executed=True,
        data={"observation_status": "CONFIRMED", "observed_exists": exists},
    )
    row = {
        "tool": "inspect_directory", "result_status": "SUCCEEDED",
        "result_reason": reason, "execution_status": "SUCCEEDED",
        "executed": True, "effective_verification_status": "UNKNOWN",
        "evidence_status": "ACTIVE",
        "request_id": REQ, "task_epoch": EPOCH,
    }
    return {"action": action, "result": result}, row


def runtime(goal=None):
    return SimpleNamespace(
        run_id=REQ, task_epoch=EPOCH,
        task_request=goal or "請在 " + ROOT + " 部署缺少的測試環境",
        progress_ledger=SimpleNamespace(
            goal=goal or "請在 " + ROOT + " 部署缺少的測試環境",
        ),
        action_ledger={}, action_result_ledger={},
    )


def add_probe(r, action_id="inspect-1", *, exists=False, result_path=ROOT):
    action, row = directory_record(
        action_id, exists=exists, result_path=result_path,
    )
    r.action_ledger[action_id] = action
    r.action_result_ledger[action_id] = row


class PersistentGoalGapTests(unittest.TestCase):
    def test_query_project_preconditions_are_declared_to_planner(self):
        readiness = tool_readiness_contract("query_project")
        self.assertTrue(readiness["existing_project_workspace_required"])
        self.assertIn("project_root", readiness["required_action_fields_when_explicit_scope"])
        self.assertEqual(readiness["directory_precondition"]["field"], "project_root")
        self.assertEqual(readiness["directory_precondition"]["expected"], True)
        self.assertTrue(readiness["directory_precondition"]["not_an_authorization_grant"])


    def test_case_d_negative_fact_persists_after_rejected_query(self):
        r = runtime()
        add_probe(r)
        before = reconstruct_goal_gaps(r)
        self.assertEqual(before["active_count"], 1)
        gap = before["active_gaps"][0]
        self.assertEqual(gap["observed_fact_ref"], "inspect-1")
        self.assertEqual(gap["status"], "UNRESOLVED")
        self.assertIn("CREATE_DIRECTORY_IF_AUTHORIZED", gap["repair_candidates"])
        self.assertFalse(gap["authorization_granted"])
        self.assertFalse(gap["goal_completion_verified"])

        rejected_action = {
            "tool": "query_project", "action_id": "query-1",
            "project_root": ROOT,
            "queries": [{"operation": "search_text", "text": "foo"}],
        }
        r.action_ledger["query-1"] = {
            "action": rejected_action,
            "result": encode_result(
                "REJECTED", "PREFLIGHT_REJECTED",
                tool="query_project", executed=False,
            ),
        }
        r.action_result_ledger["query-1"] = {
            "tool": "query_project", "executed": False,
            "result_status": "REJECTED",
            "execution_status": "REJECTED",
            "request_id": REQ, "task_epoch": EPOCH,
            "effective_verification_status": "UNKNOWN",
            "evidence_status": "ACTIVE",
        }
        after = reconstruct_goal_gaps(r)
        self.assertEqual(after["active_count"], 1)
        self.assertEqual(after["active_gaps"][0]["gap_id"], gap["gap_id"])
        self.assertEqual(after["active_gaps"][0]["observed_fact_ref"], "inspect-1")
        self.assertEqual(after["storage"], "RECONSTRUCTED_FROM_DURABLE_ACTION_LEDGER")

        blocked = decision_grounding_diagnostics([rejected_action], after)
        self.assertEqual(blocked[0]["reason"], "goal_gap_known_missing_workspace")
        self.assertEqual(blocked[0]["evidence_ref"], "inspect-1")

        # A fresh Runtime instance reconstructed from the durable history
        # must still expose the same unmet prerequisite.
        restored = runtime()
        restored.action_ledger = dict(r.action_ledger)
        restored.action_result_ledger = dict(r.action_result_ledger)
        self.assertEqual(reconstruct_goal_gaps(restored)["active_count"], 1)

    def test_authorized_discovery_and_mutation_not_preemptively_blocked(self):
        r = runtime()
        add_probe(r)
        view = reconstruct_goal_gaps(r)
        self.assertEqual(decision_grounding_diagnostics([
            {"tool": "inspect_directory", "paths": ["E:\\"]},
            {"tool": "list_directory", "path": "E:\\"},
            {"tool": "run_command", "operation": "MUTATE",
             "working_directory": "E:\\",
             "command": "New-Item -ItemType Directory -Path 'E:\\target'"},
            {"tool": "query_project", "project_root": r"E:\Elsewhere"},
        ], view), [])
        # Running from an absent cwd is incompatible even if its command
        # could otherwise be allowed.
        self.assertEqual(decision_grounding_diagnostics([
            {"tool": "run_command", "working_directory": ROOT,
             "operation": "INSPECT", "command": "Get-Location"},
        ], view)[0]["reason"], "goal_gap_known_missing_workspace")

    def test_verified_present_observation_resolves_but_failure_does_not(self):
        r = runtime()
        add_probe(r)
        add_probe(r, "inspect-2", exists=True)
        view = reconstruct_goal_gaps(r)
        self.assertEqual(view["active_count"], 0)
        self.assertEqual(view["gaps"][0]["status"], "RESOLVED")
        self.assertEqual(view["gaps"][0]["resolved_by_evidence_ref"], "inspect-2")
        self.assertEqual(decision_grounding_diagnostics([
            {"tool": "query_project", "project_root": ROOT}
        ], view), [])

    def test_mismatched_or_unexecuted_observations_are_not_facts(self):
        r = runtime()
        add_probe(r, result_path=r"E:\Different")
        self.assertEqual(reconstruct_goal_gaps(r)["active_count"], 0)
        r.action_result_ledger["inspect-1"]["executed"] = False
        self.assertEqual(reconstruct_goal_gaps(r)["active_count"], 0)

    def test_read_only_goal_never_suggests_mutation(self):
        r = runtime(goal="請唯讀檢查 " + ROOT + " 是否存在，不得寫入或建立")
        add_probe(r)
        view = reconstruct_goal_gaps(r)
        self.assertEqual(view["active_count"], 0)
        g = view["known_absences"][0]
        self.assertEqual(g["status"], "OBSERVED_ABSENCE")
        self.assertIsNone(g["desired_value"])
        self.assertNotIn("CREATE_DIRECTORY_IF_AUTHORIZED", g["repair_candidates"])

    def test_active_gap_does_not_override_latest_action_lifecycle(self):
        r = runtime()
        add_probe(r)
        view = reconstruct_goal_gaps(r)
        state = build_action_loop_state(
            progress={"decision": "CONTINUE"},
            action={"tool": "query_project", "action_id": "query-1"},
            result=encode_result("REJECTED", "PREFLIGHT_REJECTED",
                                 tool="query_project", executed=False),
            execution_status="REJECTED", verification_status="UNKNOWN",
            goal_gap_view=view,
        )
        self.assertEqual(state["required_transition"], "REPLAN_REQUIRED")
        self.assertEqual(state["goal_gap_state"]["active_count"], 1)
        self.assertIn(view["active_gaps"][0]["gap_id"],
                      state["goal_gap_state"]["unresolved_gap_ids"])


class ProgressAndRepairP3Tests(unittest.TestCase):
    def test_progress_contract_tells_planner_exact_interrupt_values(self):
        contract = dynamic_progress_contract(None)
        self.assertEqual(contract["decision_outcome_values"]["INTERRUPT"],
                         ["FAILED", "UNKNOWN"])
        self.assertEqual(contract["context_request_contract"]["kinds"],
                         ["RUNTIME_READ", "USER_INPUT", "BLOCKED"])
        self.assertTrue(contract["context_request_contract"][
            "blocked_requires_detail_and_runtime_objective_blocker_id"])

    def test_duplicate_fact_does_not_extend_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = RepairStagnation(root=Path(tmp),
                                        task_id="TASK-CASE-D", request_id=REQ,
                                        now=1000)
            self.assertTrue(tracker.accepted_observation(
                action_id="inspect-1",
                fact_key=("directory.exists", ROOT, False), now=1010))
            self.assertEqual(tracker.effective_generation, 1)
            tracker.rejected([{"reason": "invalid_progress_payload"}],
                             total=1, now=1020)
            self.assertFalse(tracker.accepted_observation(
                action_id="inspect-2",
                fact_key=("directory.exists", ROOT, False), now=1030))
            self.assertEqual(tracker.effective_generation, 1)
            self.assertFalse(tracker.accepted_observation(
                action_id="inspect-slash-alias",
                fact_key=("directory.exists", ROOT.replace(chr(92), "/"), False),
                now=1035))
            self.assertEqual(tracker.rejected_turns_since_progress, 1)
            self.assertTrue(tracker.accepted_observation(
                action_id="inspect-3",
                fact_key=("directory.exists", ROOT, True), now=1040))
            self.assertEqual(tracker.effective_generation, 2)
            self.assertFalse(tracker.accepted_observation(
                action_id="inspect-4", fact_key=("file.exists", ROOT, False),
                now=1050))


if __name__ == "__main__":
    unittest.main(verbosity=2)
