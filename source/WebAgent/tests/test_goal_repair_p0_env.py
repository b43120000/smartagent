"""Offline regressions for P0 Repair + Project Sync reconciliation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.bounded_repair import BoundedRepairBudget
from agent_core.cross_contract_repair import CrossContractRepairCoordinator
from agent_core.progress_recovery import progress_repair_hold_safe
from agent_core.goal_environment_reconciliation import reconcile_goal_conditions


class RepairAndEnvironmentRegression(unittest.TestCase):
    def test_ccrc_reconstruction_then_field_repair(self):
        c = CrossContractRepairCoordinator(
            request_id="RR-CASE", task_epoch="EPOCH", goal="Deploy and verify")
        original = [{"tool": "run_command", "action_id": "act1"}]
        first = c.observe(original, [{
            "reason": "run_command_operation_mismatch",
            "command_repair_diagnostics": [{
                "mode": "ACTION_RECONSTRUCTION",
                "field_issues": [{"field": "operation", "code": "operation_command_mismatch"}],
            }],
        }])
        self.assertEqual(first["mode"], "ACTION_RECONSTRUCTION")
        second = c.observe(original, [{
            "reason": "run_command_verification_contract_required",
            "command_repair_diagnostics": [{
                "mode": "FIELD_REPAIR",
                "field_issues": [{"field": "verify", "code": "verify_object_or_object_list_required"}],
            }],
        }])
        self.assertEqual(second["mode"], "FIELD_REPAIR")
        self.assertFalse(second["converging"])

    def test_bounded_convergence_credit_only_for_strict_reduction(self):
        b = BoundedRepairBudget(max_without_evidence=3, max_total=8)
        d = [{"reason": "invalid_progress_payload"}]
        base = {"mode": "FIELD_REPAIR", "transaction_seq": 1,
                "convergence_generation": 1, "resolved_issue_count": 1,
                "introduced_issue_count": 0, "converging": True}
        b.observe(d, evidence_generation=1)
        credited = b.observe(d, evidence_generation=1, contract_repair=base)
        self.assertEqual(credited["contract_repair_credit_used"], 1)
        duplicate = b.observe(d, evidence_generation=1, contract_repair=base)
        self.assertEqual(duplicate["contract_repair_credit_used"], 1)
        self.assertEqual(duplicate["status"], "REPAIR_ALLOWED")
        stop = b.observe(d, evidence_generation=1, contract_repair={
            **base, "convergence_generation": 2, "introduced_issue_count": 1})
        self.assertEqual(stop["status"], "EXHAUSTED")
        self.assertEqual(stop["rejected_turns_total"], 4)

    def test_hard_cap_never_reset_by_contract_credit(self):
        b = BoundedRepairBudget(max_without_evidence=8, max_total=2)
        d = [{"reason": "invalid_progress_payload"}]
        b.observe(d, evidence_generation=0)
        result = b.observe(d, evidence_generation=0, contract_repair={
            "mode": "FIELD_REPAIR", "transaction_seq": 1,
            "convergence_generation": 1, "resolved_issue_count": 2,
            "introduced_issue_count": 0, "converging": True})
        self.assertEqual(result["status"], "EXHAUSTED")

    def test_pending_action_never_replays_executed_id(self):
        actions = [{"tool": "run_command", "action_id": "act1"}]
        self.assertTrue(progress_repair_hold_safe(actions))
        self.assertFalse(progress_repair_hold_safe(actions, {"act1"}))
        self.assertFalse(progress_repair_hold_safe(actions * 2))
        self.assertFalse(progress_repair_hold_safe([
            {"tool": "final_response", "action_id": "a"}]))

    def snapshot(self):
        return {"schema": "PROJECT_SNAPSHOT_V1", "snapshot_id": "a" * 64,
                "git_head": "b" * 40, "files": [
                    {"path": "src/main.cpp", "sha256": "c" * 64},
                ]}

    def test_index_only_reconciliation_no_rescan(self):
        result = reconcile_goal_conditions(self.snapshot(), [
            {"id": "exists", "kind": "file_exists", "path": "src/main.cpp"},
            {"id": "hash", "kind": "file_sha256", "path": "src/main.cpp",
             "expected_sha256": "c" * 64},
            {"id": "absent", "kind": "file_exists", "path": "src/missing.cpp"},
            {"id": "build", "kind": "file_exists", "path": "build/app"},
            {"id": "git", "kind": "git_head", "expected_sha": "b" * 40},
            {"id": "dep", "kind": "dependency_candidate", "distribution": "cmake"},
        ], environment_index={"dependencies": {"cmake": {"status": "VERIFIED"}}})
        statuses = [row["status"] for row in result["conditions"]]
        self.assertEqual(statuses, ["VERIFIED", "VERIFIED", "MISSING",
                                    "UNKNOWN", "VERIFIED", "UNKNOWN"])
        self.assertFalse(result["task_completion_inferred"])
        self.assertFalse(result["authorization_granted"])
        self.assertFalse(result["dependency_live_verified"])

    def test_reject_path_traversal_and_duplicate(self):
        with self.assertRaises(ValueError):
            reconcile_goal_conditions(self.snapshot(), [
                {"id": "p", "kind": "file_exists", "path": "../secret"}])
        with self.assertRaises(ValueError):
            reconcile_goal_conditions(self.snapshot(), [
                {"id": "p", "kind": "file_exists", "path": "x"},
                {"id": "p", "kind": "file_exists", "path": "y"}])


if __name__ == "__main__":
    unittest.main()
