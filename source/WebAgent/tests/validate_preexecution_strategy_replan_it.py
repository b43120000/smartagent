#!/usr/bin/env python3
"""Integration of pre-execution CWD evidence, durable Progress v2 and new tool.

This test executes a real native write under a temporary authorized parent.
No network, installer, external compiler or user's real E: drive is touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.preexecution_replan import (
    analyze_preexecution_rejection, analyze_verified_prerequisite_failure,
    render_verified_prerequisite_replan, repeats_rejected_strategy,
)
from agent_core.security_context import SecurityContext
from agent_core.task_progress import (
    TaskProgressError, initialize_progress, read_progress,
    record_runtime_strategy_revision, validate_model_progress,
)
from agent_core.tools import tool_write_file
from WebAgent.protocol_loop import WebAgentProtocolLoop


class PreexecutionReplanIT(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="preexec-replan-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.target = self.allowed / "feature"
        self.action = {
            "tool": "run_command", "action_id": "ACT-CREATE-1",
            "operation": "MUTATE",
            "working_directory": str(self.target),
            "command": "New-Item -ItemType Directory -Path feature",
            "success_criteria": "Directory exists",
            "verify": [{"action": "file_exists",
                        "path": str(self.target), "expect": True}],
        }
        self.diag = [{
            "reason": "run_command_working_directory_rejected",
            "detail": "command_working_directory_not_directory:" + str(self.target),
        }]
        self.context = SecurityContext.for_workspace(self.allowed)

    def evidence(self, **kwargs):
        return analyze_preexecution_rejection(
            diagnostics=self.diag,
            submitted_actions=[self.action],
            security_context=self.context,
            original_request_paths=[str(self.allowed)],
            request_id="RR-PREEXEC-1", task_epoch="EPOCH-1",
            **kwargs,
        )

    def test_verified_failed_compiler_probe_can_replan_but_cannot_install(self):
        inspect = {
            "tool": "run_command", "action_id": "PROBE-CXX-1",
            "operation": "INSPECT", "command": "Get-Command clang++",
        }
        failed = {
            "executed": True, "result_status": "FAILED",
            "execution_status": "FAILED",
            "effective_verification_status": "FAIL",
            "expectation_status": "",
        }
        receipt = analyze_verified_prerequisite_failure(
            action=inspect, result=failed, evidence_ref="RES-PROBE-CXX-1",
            request_id="RR-PREEXEC-1", task_epoch="EPOCH-1",
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["result_verification"], "FAIL")
        negative_capability = analyze_verified_prerequisite_failure(
            action=inspect, result={
                **failed, "result_status": "SUCCEEDED",
                "execution_status": "SUCCEEDED",
            },
            evidence_ref="RES-UNSUPPORTED-CXX20",
            request_id="RR-PREEXEC-1", task_epoch="EPOCH-1",
        )
        self.assertIsNotNone(negative_capability)
        self.assertEqual(negative_capability["result_status"], "SUCCEEDED")
        self.assertEqual(negative_capability["result_verification"], "FAIL")

        self.assertFalse(receipt["authorization_granted"])
        self.assertTrue(receipt["executed"])
        self.assertIn("explicit installation", str(receipt["routes"]))
        display = render_verified_prerequisite_replan(receipt)
        self.assertIn("RUNTIME_VERIFIED_PREREQUISITE_REPLAN_REQUIRED", display)
        self.assertIn("not by itself prove a toolchain is missing", display.lower())
        self.assertIsNone(analyze_verified_prerequisite_failure(
            action=inspect, result={**failed,
                                    "effective_verification_status": "UNVERIFIED"},
            evidence_ref="RES-UNVERIFIED", request_id="RR-PREEXEC-1",
            task_epoch="EPOCH-1",
        ))
        self.assertIsNone(analyze_verified_prerequisite_failure(
            action={**inspect, "operation": "MUTATE"},
            result=failed, evidence_ref="RES-UNKNOWN-EFFECT",
            request_id="RR-PREEXEC-1", task_epoch="EPOCH-1",
        ))
        self.assertIsNone(analyze_verified_prerequisite_failure(
            action=inspect, result=failed, evidence_ref="RES-UNKNOWN-EFFECT",
            request_id="RR-PREEXEC-1", task_epoch="EPOCH-1",
            effects_pending=True,
        ))
        self.assertIsNone(analyze_verified_prerequisite_failure(
            action=inspect, result={**failed, "expectation_status": "PASS"},
            evidence_ref="RES-EXPECTED-FAIL", request_id="RR-PREEXEC-1",
            task_epoch="EPOCH-1",
        ))

    def test_verified_probe_advances_progress_revision_without_goal_reset(self):
        goal = "Compile C++20 after verified prerequisites"
        original = initialize_progress(
            "TASK-VERIFIED-CXX", request_id="RR-PREEXEC-1",
            goal=goal, root=self.root,
        )
        inspect = {"tool": "run_command", "action_id": "CXX-PROBE",
                   "operation": "INSPECT"}
        evidence = analyze_verified_prerequisite_failure(
            action=inspect,
            result={"executed": True, "result_status": "FAILED",
                    "execution_status": "FAILED",
                    "effective_verification_status": "FAIL"},
            evidence_ref="RES-FAILED-PROBE",
            request_id="RR-PREEXEC-1", task_epoch="EPOCH-1",
        )
        revised, changed = record_runtime_strategy_revision(
            original.task_id, request_id="RR-PREEXEC-1",
            evidence=evidence, round_id=3, root=self.root,
        )
        self.assertTrue(changed)
        self.assertEqual(revised.strategy_state["revision"], 2)
        self.assertEqual(revised.goal, goal)
        self.assertEqual(revised.strategy_state["history"][0]["trigger"],
                         "VERIFIED_FAILED_PREREQUISITE")
        self.assertFalse(revised.strategy_state["history"][0]["authorization_granted"])

    def test_identical_rejected_command_with_new_action_id_is_not_replan(self):
        blocked = {key: self.action.get(key) for key in
                   ("tool", "command", "operation", "working_directory")}
        self.assertTrue(repeats_rejected_strategy(
            {**self.action, "action_id": "ACT-SAME-BUT-NEW-ID"}, blocked,
        ))
        self.assertFalse(repeats_rejected_strategy({
            **self.action, "tool": "write_file",
            "action_id": "ACT-NEW-STRATEGY",
        }, blocked))

    def test_existing_parent_and_absent_target_are_eligible(self):
        receipt = self.evidence()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["target"], str(self.target))
        self.assertEqual(receipt["existing_writable_parent"], str(self.allowed))
        self.assertEqual(receipt["executed"], False)
        self.assertEqual(receipt["authorization_granted"], False)
        self.assertEqual(receipt["result_verification"], "PREFLIGHT_ONLY")
        self.assertEqual([r["tool"] for r in receipt["routes"]],
                         ["write_file", "run_command"])

    def test_replan_v2_keeps_goal_step_and_contract_and_requires_new_action(self):
        goal = "在已授權父目錄內建立 feature/main.cpp"
        initial = initialize_progress(
            "TASK-PREEXEC-1", request_id="RR-PREEXEC-1", goal=goal,
            root=self.root,
        )
        initial.base_evaluation = "Missing output is expected"
        initial.total_steps = 3
        initial.current_step = 1
        initial.steps = [
            {"step": 1, "desc": "Observe", "status": "COMPLETED"},
            {"step": 2, "desc": "Write", "status": "IN_PROGRESS"},
            {"step": 3, "desc": "Verify", "status": "PENDING"},
        ]
        initial.completion_contract = {
            "success": ["Written and verified"],
            "failure": ["Verified write failed"],
            "in_progress": ["Implementation pending"],
            "interrupted": ["No authorized route"],
        }
        # Use the canonical writer to persist the progress fixture.
        from agent_core.task_progress import _atomic_write, get_ledger_path
        _atomic_write(get_ledger_path(initial.task_id, self.root),
                      initial.to_dict())
        old = read_progress(initial.task_id, self.root)
        receipt = self.evidence()
        updated, advanced = record_runtime_strategy_revision(
            initial.task_id, request_id="RR-PREEXEC-1",
            evidence=receipt, round_id=5, root=self.root,
        )
        self.assertTrue(advanced)
        self.assertEqual(updated.strategy_state["revision"], 2)
        self.assertEqual(updated.goal, goal)
        self.assertEqual(updated.current_step, old.current_step)
        self.assertEqual(updated.total_steps, old.total_steps)
        self.assertEqual(updated.completion_contract, old.completion_contract)
        self.assertFalse(updated.strategy_state["history"][0]["completion_verified"])
        repeated, advanced = record_runtime_strategy_revision(
            initial.task_id, request_id="RR-PREEXEC-1",
            evidence=receipt, round_id=6, root=self.root,
        )
        self.assertFalse(advanced)
        self.assertEqual(repeated.strategy_state["revision"], 2)
        # A revised strategy cannot rewind already completed Progress.
        with self.assertRaisesRegex(TaskProgressError,
                                    "cannot move backwards"):
            validate_model_progress({
                "total_steps": 3, "current_step": 0,
                "decision": "CONTINUE", "outcome": "PENDING",
                "current_focus": "Replan without progress rewind",
                "next_action": "write_file",
                "evidence_refs": ["REQUEST_ACCEPTED"],
                "decision_reason": "Check invalid backwards step",
            }, updated)

        written = tool_write_file(
            str(self.target / "main.cpp"),
            '#include <iostream>\nint main(){return 0;}\n',
            security_context=self.context, structured=True,
        )
        self.assertIn("FILE_WRITTEN_VERIFIED", written)
        self.assertTrue((self.target / "main.cpp").is_file())

    def test_no_new_grant_for_outside_target_and_mismatched_request(self):
        bad_action = {**self.action, "working_directory": str(self.root / "foreign")}
        self.assertIsNone(analyze_preexecution_rejection(
            diagnostics=[{
                "reason": self.diag[0]["reason"],
                "detail": "command_working_directory_not_directory:"
                          + str(self.root / "foreign"),
            }],
            submitted_actions=[bad_action],
            security_context=self.context,
            original_request_paths=[str(self.root / "foreign")],
            request_id="RR-FOREIGN", task_epoch="EPOCH-1",
        ))
        self.assertIsNone(analyze_preexecution_rejection(
            diagnostics=self.diag,
            submitted_actions=[self.action],
            security_context=self.context,
            original_request_paths=[str(self.allowed / "elsewhere")],
            request_id="RR-MISMATCH", task_epoch="EPOCH-1",
        ))

    def test_existing_cwd_or_permission_error_is_not_missing_prerequisite(self):
        self.target.mkdir()
        self.assertIsNone(self.evidence())
        self.target.rmdir()
        self.diag[0]["detail"] = "path_outside_authorized_roots:" + str(self.target)
        self.assertIsNone(self.evidence())

    def test_runtime_loop_produces_next_planner_guidance(self):
        loop = WebAgentProtocolLoop(
            self.allowed, lambda *_: "", progress_root=self.root,
        )
        loop.run_id = "RR-PREEXEC-1"
        loop.task_id = "TASK-PREEXEC-LOOP"
        loop.task_epoch = "EPOCH-1"
        loop.turn_id = 5
        loop.authorized_paths = [str(self.allowed)]
        loop.tools.begin_run(loop.run_id, "請建立 feature/main.cpp",
                             loop.authorized_paths)
        initialize_progress(loop.task_id, request_id=loop.run_id,
                            goal="請建立 feature/main.cpp", root=self.root)
        hint = loop._runtime_preexecution_strategy_replan(
            self.diag,
            [{"tool": "report_progress", "action_id": "PG-1"}, self.action,
             {"tool": "turn_commit", "action_count": 2}],
        )
        self.assertIn("RUNTIME_PREEXECUTION_REPLAN_REQUIRED", hint)
        self.assertIn("NATIVE_FILE_CREATION", hint)
        self.assertEqual(read_progress(loop.task_id, self.root).
                         strategy_state["revision"], 2)
        self.assertNotIn("authorization_granted\":true", hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
