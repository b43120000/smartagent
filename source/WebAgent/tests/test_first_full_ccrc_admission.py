#!/usr/bin/env python3
"""RCA TASK-2EE266DD61357DC0D77D: bootstrap FULL and CCRC recovery.

Verify the first rejected Progress never creates phantom Action evidence,
a Progress-only repair reattaches the *same* held read actions, and the
observation actually executes after independent FULL/Scope admission.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os

from agent_core.cross_contract_repair import (
    ACTION_RECONSTRUCTION, FIELD_REPAIR, CrossContractRepairCoordinator,
    repair_instruction,
)
from agent_core.progress_reply_contract import (
    dynamic_progress_contract, materialize_progress,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response

CONTRACT = {
    "success": ["Independent directory observation and content proof obtained"],
    "failure": ["The observation tool failed"],
    "in_progress": [
        "Inspecting deployed root and fixture contents",
        "Locating the authorized deployment source",
    ],
    "interrupted": ["A real blocking prerequisite is unresolved"],
}


def full(action_id: str, *, correct: bool) -> dict:
    return {
        "tool": "report_progress", "action_id": action_id,
        "progress_update": "FULL",
        "base_evaluation": "Inspect existing deployment before any mutation.",
        "total_steps": 2, "current_step": 0,
        "steps": [
            {"step": 1, "desc": "Inspect current deployment", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "Verify final deployment", "status": "PENDING"},
        ],
        "current_focus": "Inspect root and source",
        "next_action": "list_directory and inspect_directory",
        "completion_contract": CONTRACT,
        "decision": "CONTINUE", "outcome": "PENDING",
        "matched_condition": (
            "Inspecting deployed root and fixture contents"
            if correct else "An invented condition is already satisfied"
        ),
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "Runtime evidence is not yet present.",
    }


class BootstrapContractTests(unittest.TestCase):
    def test_unsupported_first_patch_never_inherits_a_fictional_plan(self):
        self.assertEqual(dynamic_progress_contract(None)["mode"], "FULL")
        with self.assertRaisesRegex(ValueError, "first_progress_requires_FULL"):
            materialize_progress({
                "tool": "report_progress",
                "progress_update": "PATCH", "decision": "CONTINUE",
            }, None)

    def test_ccrc_first_full_guidance_and_exact_condition_repair(self):
        coordinator = CrossContractRepairCoordinator(
            request_id="RR-RCA", task_epoch="EPOCH-RCA",
            goal="Inspect deployment, then repair with authorization only",
        )
        proposal = [full("PROG-1", correct=False),
                    {"tool": "inspect_directory", "action_id": "OBS-1",
                     "paths": [r"E:\Missing"]}]
        diagnostic = [{
            "reason": "invalid_progress_payload",
            "field_issues": [{"field": "matched_condition"}],
        }]
        decision = coordinator.observe(proposal, diagnostic,
                                       declared_tools=["inspect_directory"])
        self.assertEqual(decision["mode"], FIELD_REPAIR)
        decision.update(
            progress_mode="FULL", accepted_plan=False,
            held_unadmitted_action_ids=["OBS-1"],
        )
        guidance = repair_instruction(decision)
        self.assertIn("progress_update=FULL", guidance)
        self.assertIn("No existing plan fields can be inherited", guidance)
        self.assertIn("matched_condition_id", guidance)
        self.assertIn("OBS-1", guidance)
        self.assertNotIn("use PATCH to inherit", guidance)

    def test_read_only_hold_released_at_action_reconstruction(self):
        from WebAgent.tests.validate_cross_contract_repair_it import FakeLoop
        host = FakeLoop()
        host.progress_ledger = None
        held = {"tool": "inspect_directory", "action_id": "RCA-READ",
                "paths": [r"E:\Unknown"]}
        host.pending_progress_repair_actions = [held]
        calls = [full("P-1", correct=False), held,
                 {"tool": "turn_commit", "action_count": 2}]
        diagnostic = [{
            "reason": "invalid_progress_payload",
            "field_issues": [{"field": "matched_condition"}],
        }]
        observed = []
        for _ in range(3):
            observed.append(WebAgentProtocolLoop._ccrc_rejected(
                host, calls, [dict(row) for row in diagnostic]))
        self.assertEqual([x["mode"] for x in observed],
                         [FIELD_REPAIR, FIELD_REPAIR, ACTION_RECONSTRUCTION])
        self.assertEqual(observed[0]["progress_mode"], "FULL")
        self.assertEqual(host.pending_progress_repair_actions, [])
        self.assertFalse(host.deferred_action_repairs)
        self.assertTrue(any(name == "ccrc_unadmitted_progress_hold_released"
                            for name, _ in host.events))
        self.assertFalse(any(name == "tool_started"
                             for name, _ in host.events))


class RealAdmissionLifecycleTests(unittest.TestCase):
    def test_repaired_full_progress_reaches_real_read_only_actions(self):
        with tempfile.TemporaryDirectory(prefix="rca-first-full-") as tmp:
            root = Path(tmp)
            absent = root / "case_d_not_deployed"
            events = []
            prompts = []
            action1 = {
                "tool": "list_directory", "action_id": "RCA-LIST-1",
                "path": str(root),
            }
            action2 = {
                "tool": "inspect_directory", "action_id": "RCA-INSPECT-1",
                "paths": [str(absent)],
            }

            def planner(prompt, _expected, _attachments):
                prompts.append(prompt)
                if len(prompts) == 1:
                    return response(
                        full("RCA-PROGRESS-1", correct=False),
                        action1, action2,
                    )
                if len(prompts) == 2:
                    # Planner supplies ONLY corrected FULL; original actions
                    # are held by Runtime and must be reattached without
                    # changing IDs or widening the originally authorized scope.
                    return response(full("RCA-PROGRESS-2", correct=True))
                raise RuntimeError("RCA_STOP_AFTER_FIRST_EXECUTED_EVIDENCE")

            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=6,
                event_sink=lambda name, **data: events.append((name, data)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "0",
                "SMARTAGENT_PREREQ_V3_BCD": "0",
                "SMARTAGENT_PREREQ_V3_PR_A": "0",
            }):
                with self.assertRaisesRegex(
                    RuntimeError, "RCA_STOP_AFTER_FIRST_EXECUTED_EVIDENCE",
                ):
                    loop.run(
                        f"Inspect {absent} in read-only mode before deployment.",
                        request_id="RR-RCA-FIRST-FULL", task_id="TASK-RCA-FIRST-FULL",
                        task_epoch="EPOCH-RCA-FIRST-FULL",
                    )

            rejected = [row for name, row in events if name == "protocol_rejected"]
            self.assertGreaterEqual(len(rejected), 1)
            self.assertEqual(
                rejected[0]["diagnostics"][0]["reason"],
                "invalid_progress_payload",
            )
            self.assertTrue(
                any(name == "progress_repair_actions_preserved"
                    for name, _ in events),
                "Valid read-only actions must remain unexecuted while Progress is invalid",
            )
            self.assertTrue(
                any(name == "progress_repair_actions_restored"
                    for name, _ in events),
                "Progress-only repaired FULL must restore the original actions",
            )
            started = {
                data.get("action_id")
                for name, data in events if name == "tool_started"
            }
            self.assertIn("RCA-LIST-1", started)
            self.assertIn("RCA-INSPECT-1", started)
            self.assertIn("RCA-LIST-1", loop.action_result_ledger)
            self.assertIn("RCA-INSPECT-1", loop.action_result_ledger)
            self.assertFalse(absent.exists())
            self.assertIn("FULL", prompts[1])
            self.assertTrue(
                any(name == "cross_contract_repair_decision"
                    and row.get("progress_mode") == "FULL"
                    for name, row in events)
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
