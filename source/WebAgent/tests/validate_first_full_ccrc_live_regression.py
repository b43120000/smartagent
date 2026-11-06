#!/usr/bin/env python3
"""Live-shaped first-FULL/CCRC recovery regression (no network, no writes).

Replays the 2026-11-06 six-rejection failure class without assuming that a
Protocol rejection is Action execution.  Both Windows and Linux supported.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.cross_contract_repair import (
    ACTION_RECONSTRUCTION, FIELD_REPAIR, repair_instruction,
)
from agent_core.progress_recovery import repair_diagnostic
from agent_core.task_progress import TaskProgressError, initialize_progress
from agent_core.progress_reply_contract import has_accepted_plan
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def progress(action_id, condition, *, include_action=True):
    return {
        "tool": "report_progress", "action_id": action_id,
        "progress_update": "FULL",
        "base_evaluation": "Planner has not inspected the directory yet.",
        "total_steps": 2, "current_step": 0,
        "steps": [
            {"step": 1, "desc": "Inspect scope", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "Verify result", "status": "PENDING"},
        ],
        "completion_contract": {
            "success": ["Requested directory independently verified"],
            "failure": ["Directory inspection failed"],
            "in_progress": ["Need initial directory inspection",
                            "Need independent content verification"],
            "interrupted": ["Required information inaccessible"],
        },
        "current_focus": "Obtain real directory evidence",
        "next_action": "inspect_directory",
        "decision": "CONTINUE",
        "outcome": "PENDING",
        "matched_condition": condition,
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "Only initial request identity is known.",
    }


class FirstFullCCRCRegression(unittest.TestCase):
    def test_initial_ccrc_field_hint_does_not_advertise_patch(self):
        decision = {
            "mode": FIELD_REPAIR, "field_issues": [
                {"origin": "PROGRESS", "field": "matched_condition",
                 "code": "invalid_progress_payload"}
            ]
        }
        text = repair_instruction(decision, accepted_plan=False)
        self.assertIn("[PROGRESS_ADMISSION_MODE] FULL", text)
        self.assertIn("FIRST FULL IS UNADMITTED", text)
        self.assertIn("Do NOT use PATCH", text)
        self.assertNotIn("use PATCH to inherit accepted", text)
        self.assertIn("[PROGRESS_ADMISSION_MODE] PATCH",
                      repair_instruction(decision, accepted_plan=True))

    def test_first_full_repair_diagnostic_has_actual_condition_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = initialize_progress(
                "TASK-FIRST-FULL", request_id="RR-FIRST-FULL",
                goal="Inspect the project root", root=tmp,
            )
            bad = progress("P-1", "paraphrased not exact")
            error = TaskProgressError(
                "matched_condition is not declared in completion_contract.in_progress",
                field="matched_condition",
            )
            diagnostic = repair_diagnostic(
                bad, ledger, error, {"REQUEST_ACCEPTED"},
            )
            hint = diagnostic["progress_admission_hint"]
            self.assertEqual(hint["mode"], "FULL")
            self.assertFalse(hint["accepted_plan_exists"])
            self.assertFalse(hint["may_inherit_plan_fields"])
            self.assertEqual(
                [row["condition_text"] for row in hint["condition_choices"]],
                bad["completion_contract"]["in_progress"],
            )
            self.assertEqual(hint["condition_class"], "in_progress")
            self.assertIn("base_evaluation", hint["required_fields"])
            self.assertIn("current_step", hint["required_fields"])
            self.assertIn("matched_condition", str(diagnostic["detail"]))

    def test_progress_error_then_full_repair_dispatches_original_held_probe(self):
        with tempfile.TemporaryDirectory(prefix="first-full-live-") as tmp:
            root = Path(tmp)
            turns = []
            events = []
            actions = [
                {"tool": "inspect_directory", "action_id": "LIVE-INSPECT-1",
                 "paths": [str(root)]},
            ]

            def planner(prompt, expected, attachments):
                turns.append(prompt)
                if len(turns) == 1:
                    return response(
                        progress("LIVE-PROGRESS-1", "not an exact condition"),
                        *actions,
                    )
                if len(turns) == 2:
                    self.assertIn("[CROSS_CONTRACT_REPAIR]", prompt)
                    self.assertIn("[PROGRESS_ADMISSION_MODE] FULL", prompt)
                    self.assertIn("matched_condition", prompt)
                    # A format repair may send Progress only: Runtime must
                    # restore the *unexecuted* and structurally valid held
                    # inspection, then re-run every ordinary admission gate.
                    return response(progress(
                        "LIVE-PROGRESS-2", "Need initial directory inspection",
                    ))
                raise RuntimeError("STOP_AFTER_INITIAL_OBSERVATION")

            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root,
                max_turns=5,
                event_sink=lambda name, **data: events.append((name, data)),
            )
            with self.assertRaisesRegex(
                    RuntimeError, "STOP_AFTER_INITIAL_OBSERVATION"):
                loop.run(
                    "Check the existing directory and report its state",
                    request_id="RR-LIVE-FIRST-FULL",
                    task_id="TASK-LIVE-FIRST-FULL",
                    task_epoch="EPOCH-LIVE-FIRST-FULL",
                )
            self.assertEqual(len(turns), 3)
            rejects = [(name, data) for name, data in events
                       if name == "protocol_rejected"]
            self.assertEqual(len(rejects), 1)
            self.assertTrue(any(
                name == "progress_repair_actions_restored" and
                "LIVE-INSPECT-1" in data.get("action_ids", [])
                for name, data in events
            ))
            self.assertTrue(any(
                name == "tool_started" and
                data.get("action_id") == "LIVE-INSPECT-1"
                for name, data in events
            ))
            self.assertTrue(any(
                name == "action_evidence_recorded" and
                data.get("action_id") == "LIVE-INSPECT-1"
                for name, data in events
            ))
            self.assertIn("LIVE-INSPECT-1", loop.action_result_ledger)
            self.assertTrue(loop.action_result_ledger[
                "LIVE-INSPECT-1"]["executed"])
            self.assertTrue(has_accepted_plan(loop.progress_ledger))
            self.assertEqual(loop.progress_ledger.current_step, 0)

    def test_reconstruction_can_release_only_unadmitted_hold(self):
        with tempfile.TemporaryDirectory(prefix="ccrc-hold-") as tmp:
            root = Path(tmp)
            loop = WebAgentProtocolLoop(root, lambda *_: "", progress_root=root)
            loop.run_id = "RR-CCRC-RECON"
            loop.task_id = "TASK-CCRC-RECON"
            loop.task_epoch = "EP-CCRC-RECON"
            loop.turn_id = 2
            old = {"tool": "inspect_directory", "action_id": "OLD-READ",
                   "paths": [str(root)]}
            fresh = {"tool": "list_directory", "action_id": "NEW-READ",
                     "path": str(root)}
            loop.pending_progress_repair_actions = [old]
            loop._ccrc = SimpleNamespace(mode=ACTION_RECONSTRUCTION,
                                         replan_required=False)
            calls = [
                progress("P-RECON", "Need initial directory inspection"),
                fresh,
                {"tool": "turn_commit", "action_count": 2},
            ]
            restored = loop._restore_progress_repair_actions(calls)
            self.assertEqual(restored, calls)
            self.assertEqual(loop.pending_progress_repair_actions, [])
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.action_ledger, {})
            # Unadmitted hold release does not execute either Action.
            self.assertNotIn("OLD-READ", loop.action_result_ledger)
            self.assertNotIn("NEW-READ", loop.action_result_ledger)

    def test_fresh_ids_alone_do_not_replace_held_action(self):
        with tempfile.TemporaryDirectory(prefix="ccrc-hold-id-") as tmp:
            root = Path(tmp)
            loop = WebAgentProtocolLoop(root, lambda *_: "", progress_root=root)
            loop.run_id = "RR-CCRC-ID"
            loop.task_id = "TASK-CCRC-ID"
            loop.task_epoch = "EP-CCRC-ID"
            loop.turn_id = 3
            old = {"tool": "inspect_directory", "action_id": "OLD-READ",
                   "paths": [str(root)]}
            fresh = {**old, "action_id": "NEW-IDENTITY"}
            loop.pending_progress_repair_actions = [old]
            loop._ccrc = SimpleNamespace(mode=ACTION_RECONSTRUCTION,
                                         replan_required=False)
            calls = [
                progress("P-ID", "Need initial directory inspection"),
                fresh, {"tool": "turn_commit", "action_count": 2},
            ]
            restored = loop._restore_progress_repair_actions(calls)
            self.assertEqual(restored[1]["action_id"], "OLD-READ")
            self.assertEqual(loop.pending_progress_repair_actions, [old])


if __name__ == "__main__":
    unittest.main(verbosity=2)
