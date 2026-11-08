"""V10 Case D: no single inspection, or fabricated VERIFY, can finish a deployment."""
from __future__ import annotations
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from WebAgent.minimal_core_loop import narrow_goal_verified
from WebAgent.minimal_deployment_terminal import evaluate_deployment_goal

TARGET = r"E:\\SmartAgent_GoalDriven_IT_GH_v1"
GOAL = (
    "請重新驗證並完成 Case D 的部署目標。\\n"
    "預期部署根目錄：" + TARGET + "\\n"
    "預期包含：case_a、case_b、case_c、fixture_manifest.json、check_results.py。\\n"
    "以上五個項目必須直接位於指定根目錄。"
)
REQUIRED = ["case_a", "case_b", "case_c", "fixture_manifest.json", "check_results.py"]
ACT1 = "MIN-SOURCE-OBS"
VERIFY = "MIN-VERIFY-OBS"


def fixture():
    runtime = SimpleNamespace(
        _target_path_contract={
            "binding_status": "BOUND", "requested_target": TARGET,
            "required_top_level_entries": REQUIRED,
            "revalidation_requested": True,
        },
        _target_pre_mutation={
            "state": "ABSENT", "resolved_path": TARGET, "entries": {},
            "reason": "TARGET_NOT_FOUND",
        },
        run_id="RR-TEST", task_id="TASK-TEST", task_epoch="EPOCH-TEST",
        action_ledger={}, action_result_ledger={},
        tools=SimpleNamespace(), workspace=TARGET,
    )
    source = {
        "action": {"tool": "inspect_directory", "action_id": ACT1, "paths": [TARGET]},
        "receipt": {"status": "SUCCEEDED", "executed": True, "reason": "DIRECTORY_INSPECTED"},
    }
    action = {
        "tool": "run_command", "operation": "VERIFY", "action_id": VERIFY,
        "working_directory": TARGET, "command": "Get-Location",
        "verifies_action_id": ACT1,
        "verify": [{"action": "file_exists", "path": TARGET}],
    }
    receipt = {
        "status": "SUCCEEDED", "executed": True,
        "data": {"verification_status": "PASS"},
    }
    row = {"action": action, "receipt": receipt}
    runtime.action_ledger[VERIFY] = {"action": action}
    runtime.action_result_ledger[VERIFY] = {
        "request_id": runtime.run_id, "task_epoch": runtime.task_epoch,
        "executed": True, "execution_status": "SUCCEEDED",
        "verification_status": "PASS", "evidence_status": "ACTIVE",
    }
    return runtime, [source, row]


class MinimalDeploymentTerminalTests(unittest.TestCase):
    def test_list_directory_is_never_whole_case_d_proof(self):
        self.assertFalse(narrow_goal_verified(GOAL, [{
            "action": {"tool": "list_directory", "path": TARGET},
            "receipt": {"status": "SUCCEEDED", "executed": True,
                        "data": {"terminal_eligible": True,
                                 "entries": [{"name": "case_a"}]}},
        }]))

    def test_requires_independent_verify_exact_cwd_and_content(self):
        runtime, history = fixture()
        with patch("WebAgent.minimal_deployment_terminal.SecurityContext.from_agent",
                   return_value=object()), patch(
            "WebAgent.minimal_deployment_terminal.verify_target_completion",
            return_value={"status": "PASS"}), patch(
            "WebAgent.minimal_deployment_terminal._pinned_blob_integrity",
            return_value=(True, "PINNED_SOURCE_BYTES_VERIFIED"),
        ):
            proof = evaluate_deployment_goal(runtime, GOAL, history)
            self.assertTrue(proof.verified)
            self.assertEqual(proof.evidence_refs, (VERIFY,))
            self.assertEqual(proof.goal_kind, "DEPLOYMENT_CONTENT_VERIFIED")
            history[-1]["action"]["working_directory"] = r"E:\\Other"
            self.assertFalse(evaluate_deployment_goal(runtime, GOAL, history).verified)

    def test_rejects_missing_verify_postcondition_or_foreign_epoch(self):
        runtime, history = fixture()
        history[-1]["receipt"]["data"]["verification_status"] = "UNVERIFIED"
        self.assertEqual(evaluate_deployment_goal(runtime, GOAL, history).reason,
                         "EXACT_ROOT_INDEPENDENT_VERIFY_REQUIRED")
        history[-1]["receipt"]["data"]["verification_status"] = "PASS"
        runtime.action_result_ledger[VERIFY]["task_epoch"] = "FOREIGN"
        self.assertEqual(evaluate_deployment_goal(runtime, GOAL, history).reason,
                         "VERIFY_LEDGER_IDENTITY_OR_STATUS_INVALID")

    def test_target_structure_or_content_failure_is_terminal_blocker(self):
        runtime, history = fixture()
        with patch("WebAgent.minimal_deployment_terminal.SecurityContext.from_agent",
                   return_value=object()), patch(
            "WebAgent.minimal_deployment_terminal.verify_target_completion",
            return_value={"status": "PASS"}), patch(
            "WebAgent.minimal_deployment_terminal._pinned_blob_integrity",
            return_value=(False, "PINNED_SOURCE_BLOB_MISMATCH"),
        ):
            self.assertEqual(evaluate_deployment_goal(runtime, GOAL, history).reason,
                             "PINNED_SOURCE_BLOB_MISMATCH")


if __name__ == "__main__":
    unittest.main()
