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
from agent_core.tool_result_policy import structured_outcome
from agent_core.tool_result_contract import attach_runtime_result
from agent_core.goal_strategy_transition import classify_runtime_evidence, build_transition_readiness
from types import SimpleNamespace
import json


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

    def _directory_evidence(self, *, errors=None, authorized=True, exists=False, requested=None):
        path = r"E:\\SmartAgent_PR_BCD_LIVE\\feature"
        requested = path if requested is None else requested
        payload = {"tool": "inspect_directory", "paths": [{
            "path": path, "authorized": authorized, "exists": exists,
            "errors": ["path_not_found"] if errors is None else errors,
            "inaccessible_count": 0,
        }]}
        status, reason, executed, data = structured_outcome("inspect_directory", payload)
        result = attach_runtime_result(json.dumps(payload), status, reason,
                                       tool="inspect_directory", executed=executed,
                                       action_id="act-dir", data=data)
        return ({"tool": "inspect_directory", "action_id": "act-dir",
                 "paths": [requested]}, result, {
                     "tool": "inspect_directory", "result_status": status,
                     "executed": executed, "result_reason": reason,
                     "evidence_status": "ACTIVE", "verification_status": "UNVERIFIED",
                 })

    def test_confirmed_missing_directory_is_completed_observation(self):
        action, result, record = self._directory_evidence()
        self.assertEqual(record["result_status"], "SUCCEEDED")
        self.assertTrue(record["executed"])
        fact = classify_runtime_evidence(record, {"action": action, "result": result})
        self.assertEqual(fact["classification"], "VERIFIED_NEGATIVE_FACT")
        self.assertTrue(fact["proves_missing_prerequisite"])
        self.assertEqual(fact["eligible_transition"], "PREREQUISITE_RESOLUTION")
        self.assertFalse(fact["goal_completion_verified"])
        self.assertFalse(fact["authorizes_execution"])

    def test_denial_io_and_target_mismatch_are_not_absence_proof(self):
        for kwargs in ({"authorized": False, "errors": ["path_not_authorized_by_user_turn"]},
                       {"errors": ["PermissionError: denied"]},
                       {"requested": r"E:\\SmartAgent_PR_BCD_LIVE\\other"}):
            action, result, record = self._directory_evidence(**kwargs)
            fact = classify_runtime_evidence(record, {"action": action, "result": result})
            self.assertFalse(fact["proves_missing_prerequisite"], kwargs)
        action, result, record = self._directory_evidence(requested=r"E:\\SmartAgent_PR_BCD_LIVE\\other")
        self.assertEqual(record["result_status"], "SUCCEEDED")
        self.assertEqual(classify_runtime_evidence(record, {"action": action, "result": result})["classification"],
                         "OUTCOME_UNVERIFIED")

    def test_existing_directory_remains_positive_observation(self):
        action, result, record = self._directory_evidence(exists=True, errors=[])
        fact = classify_runtime_evidence(record, {"action": action, "result": result})
        self.assertEqual(fact["environment_observed_exists"], True)
        self.assertFalse(fact["proves_missing_prerequisite"])

    def test_readiness_carries_directory_fact_without_goal_completion(self):
        action, result, record = self._directory_evidence()
        record.update(request_id="RR-CASE", task_epoch="epoch")
        runtime = SimpleNamespace(
            progress_ledger=SimpleNamespace(
                goal="Create feature folder",
                completion_contract={"success": ["feature folder exists"]}),
            action_result_ledger={"act-dir": record},
            action_ledger={"act-dir": {"action": action, "result": result}},
            condition_result_ledger={},
            run_id="RR-CASE", task_epoch="epoch", task_request="Create feature folder",
        )
        readiness = build_transition_readiness(runtime)
        self.assertEqual(readiness["latest_blocker"]["classification"], "VERIFIED_NEGATIVE_FACT")
        self.assertEqual(readiness["recent_environment_facts"][0]["goal_condition_link"], "UNBOUND")
        self.assertFalse(readiness["recent_environment_facts"][0]["goal_completion_inferred"])
        self.assertFalse(readiness["recent_environment_facts"][0]["authorization_granted"])

    def test_directory_predicate_requires_verified_evidence(self):
        snapshot = {**self.snapshot(), "workspace_root": r"E:\\SmartAgent_PR_BCD_LIVE"}
        paths = [
            {"id": "existing", "kind": "directory_exists", "path": "src"},
            {"id": "missing", "kind": "directory_exists", "path": "feature"},
        ]
        no_fact = reconcile_goal_conditions(snapshot, paths)
        self.assertEqual([row["status"] for row in no_fact["conditions"]],
                         ["VERIFIED", "UNKNOWN"])
        action, result, record = self._directory_evidence()
        record.update(request_id="RR-CASE", task_epoch="epoch")
        observed = reconcile_goal_conditions(
            snapshot, paths,
            action_ledger={"act-dir": {"action": action, "result": result}},
            action_results={"act-dir": record},
            request_id="RR-CASE", task_epoch="epoch",
        )
        self.assertEqual([row["status"] for row in observed["conditions"]],
                         ["VERIFIED", "MISSING"])
        self.assertEqual(observed["conditions"][1]["evidence_ref"], "act-dir")
        self.assertFalse(observed["authorization_granted"])
        self.assertFalse(observed["task_completion_inferred"])
        self.assertFalse(observed["new_scan_performed"])

    def test_stale_or_cross_request_fact_cannot_verify_goal(self):
        snapshot = {**self.snapshot(), "workspace_root": r"E:\\SmartAgent_PR_BCD_LIVE"}
        action, result, record = self._directory_evidence()
        record.update(request_id="RR-OTHER", task_epoch="epoch")
        kwargs = dict(
            action_ledger={"act-dir": {"action": action, "result": result}},
            action_results={"act-dir": record},
            request_id="RR-CASE", task_epoch="epoch")
        row = {"id": "missing", "kind": "directory_exists", "path": "feature"}
        self.assertEqual(reconcile_goal_conditions(snapshot, [row], **kwargs)["conditions"][0]["status"], "UNKNOWN")
        record.update(request_id="RR-CASE", evidence_status="STALE")
        self.assertEqual(reconcile_goal_conditions(snapshot, [row], **kwargs)["conditions"][0]["status"], "UNKNOWN")

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
