#!/usr/bin/env python3
"""Case D RCA: an invalid first FULL must not trap the initial observation.

Never touches the user's E: drive or uses a browser. The exact first-FULL,
Action-Hold, CCRC handoff and admission loop is exercised in a scoped temp root.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.cross_contract_repair import (
    ACTION_RECONSTRUCTION,
    CrossContractRepairCoordinator,
    FIELD_REPAIR,
    repair_instruction,
)
from agent_core.initial_progress_normalization import (
    materialize_unadmitted_first_full,
    remember_unadmitted_first_full,
)
from agent_core.progress_reply_contract import materialize_progress
from agent_core.task_progress import (
    TaskProgressError, initialize_progress, read_progress, validate_model_progress,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def initial_progress():
    return {
        "tool": "report_progress", "action_id": "P-CASE-D-1",
        "progress_update": "FULL",
        "base_evaluation": "Inspect target before any deployment or mutation",
        "total_steps": 2, "current_step": 0,
        "steps": [
            {"step": 1, "desc": "Inspect target", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "Verify original goal", "status": "PENDING"},
        ],
        "current_focus": "Get independent directory evidence",
        "next_action": "list_directory",
        "completion_contract": {
            "success": ["Fixture complete"],
            "failure": ["Fixture failed"],
            "in_progress": [
                "Awaiting target observation",
                "Awaiting source verification",
            ],
            "interrupted": ["Required prerequisite cannot be established"],
        },
        "decision": "CONTINUE", "outcome": "PENDING",
        "matched_condition": "BAD_NOT_DECLARED",
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "No Runtime directory observation yet",
    }


class FirstFullDraftTests(unittest.TestCase):
    def test_initial_draft_reuses_only_planner_plan_fields(self):
        full = initial_progress()
        draft = remember_unadmitted_first_full(
            {}, full, request_id="R", task_epoch="E", round_id=1,
        )
        self.assertEqual(draft["schema"],
                         "SMARTAGENT_UNADMITTED_FIRST_FULL_DRAFT_V1")
        self.assertEqual(draft["admitted"], False)
        self.assertEqual(draft["authorization_granted"], False)
        self.assertNotIn("matched_condition", draft["fields"])
        self.assertNotIn("evidence_refs", draft["fields"])
        self.assertNotIn("next_action", draft["fields"])
        repaired = {
            "tool": "report_progress", "action_id": "P-CASE-D-2",
            "progress_update": "FULL", "decision": "CONTINUE",
            "outcome": "PENDING",
            "matched_condition": "Awaiting target observation",
            "current_focus": "Perform read",
            "next_action": "list_directory",
            "decision_reason": "Inspect before any mutation",
            "evidence_refs": ["REQUEST_ACCEPTED"],
        }
        merged, fields = materialize_unadmitted_first_full(
            repaired, draft, request_id="R", task_epoch="E",
            field_repair_active=True,
        )
        self.assertEqual(set(fields), set(draft["fields"]))
        self.assertEqual(merged["matched_condition"], "Awaiting target observation")
        self.assertEqual(merged["decision_reason"], repaired["decision_reason"])
        self.assertEqual(merged["evidence_refs"], repaired["evidence_refs"])
        self.assertEqual(validate_model_progress(merged)["current_step"], 0)

    def test_patch_and_cross_request_draft_cannot_inherit(self):
        full = initial_progress()
        draft = remember_unadmitted_first_full(
            {}, full, request_id="R", task_epoch="E", round_id=1,
        )
        for patch in (
            {"tool": "report_progress", "decision": "CONTINUE",
             "progress_update": "PATCH"},
            {"tool": "report_progress", "decision": "COMPLETE",
             "progress_update": "FULL"},
        ):
            merged, used = materialize_unadmitted_first_full(
                patch, draft, request_id="R", task_epoch="E",
                field_repair_active=True,
            )
            self.assertFalse(used)
            self.assertNotIn("steps", merged)
        merged, used = materialize_unadmitted_first_full(
            {"tool": "report_progress", "decision": "CONTINUE",
             "progress_update": "FULL"},
            draft, request_id="DIFFERENT", task_epoch="E",
            field_repair_active=True,
        )
        self.assertFalse(used)
        self.assertNotIn("steps", merged)

    def test_first_full_contract_advertised_when_not_admitted(self):
        ccrc = CrossContractRepairCoordinator(
            request_id="R", task_epoch="E", goal="Verify fixture")
        original = initial_progress()
        proposal = [original, {"tool": "list_directory", "action_id": "ACT-D"}]
        diagnostic = [{"reason": "invalid_progress_payload",
                       "field_issues": [{"field": "matched_condition"}]}]
        first = ccrc.observe(proposal, diagnostic)
        self.assertEqual(first["mode"], FIELD_REPAIR)
        note = repair_instruction(first, accepted_plan=False)
        self.assertIn("progress_update=FULL", note)
        self.assertIn("never PATCH", note)
        self.assertNotIn("use a minimal PATCH", note)
        self.assertIn("Progress", note)


class FirstActionClosedLoop(unittest.TestCase):
    def test_bad_first_condition_repaired_then_first_read_executes(self):
        with tempfile.TemporaryDirectory(prefix="case-d-progress-repair-") as tmp:
            root = Path(tmp)
            turns = []
            events = []
            def planner(prompt, expected, attachments):
                turns.append(prompt)
                if len(turns) == 1:
                    return response(
                        initial_progress(),
                        {"tool": "list_directory", "action_id": "ACT-READ-1",
                         "path": str(root)},
                    )
                if len(turns) == 2:
                    self.assertIn("FULL", prompt)
                    return response({
                        "tool": "report_progress", "action_id": "P-CASE-D-2",
                        "progress_update": "FULL", "decision": "CONTINUE",
                        "outcome": "PENDING",
                        "matched_condition": "Awaiting target observation",
                        "current_focus": "Inspect target",
                        "next_action": "list_directory",
                        "decision_reason": "Repair prior condition, preserve planned read",
                        "evidence_refs": ["REQUEST_ACCEPTED"],
                    })
                raise RuntimeError("STOP_AFTER_FIRST_REAL_READ")

            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=8,
                event_sink=lambda event, **kw: events.append((event, kw)),
            )
            with self.assertRaisesRegex(
                RuntimeError, "STOP_AFTER_FIRST_REAL_READ"
            ):
                loop.run(
                    "請先唯讀檢查 Case D 目錄，不做寫入",
                    request_id="RR-CASE-D-P0",
                    task_id="TASK-CASE-D-P0",
                    task_epoch="EPOCH-CASE-D-P0",
                )
            self.assertEqual(len(turns), 3)
            self.assertTrue(any(x[0] == "unadmitted_first_full_draft_preserved"
                                for x in events))
            self.assertTrue(any(x[0] == "unadmitted_first_full_draft_materialized"
                                for x in events))
            self.assertTrue(any(x[0] == "tool_started"
                                and x[1].get("action_id") == "ACT-READ-1"
                                for x in events))
            accepted = read_progress(loop.task_id, root)
            self.assertIsNotNone(accepted)
            self.assertGreater(accepted.total_steps, 0)
            self.assertEqual(accepted.completion_contract["in_progress"][0],
                             "Awaiting target observation")

    def test_reconstruction_releases_original_action_hold(self):
        from WebAgent.tests.validate_cross_contract_repair_it import (
            FakeLoop, progress, issues, command,
        )
        host = FakeLoop()
        held = command("D-HELD")
        host.pending_progress_repair_actions = [held]
        calls = [progress(), held, {"tool": "turn_commit", "action_count": 2}]
        for _ in range(3):
            result = WebAgentProtocolLoop._ccrc_rejected(host, calls, issues())
        self.assertEqual(result["mode"], ACTION_RECONSTRUCTION)
        self.assertEqual(host.pending_progress_repair_actions, [])
        self.assertTrue(any(
            e == "ccrc_unadmitted_progress_hold_released"
            and d.get("release_mode") == ACTION_RECONSTRUCTION
            for e,d in host.events
        ))
        self.assertFalse(any(e == "tool_started" for e,d in host.events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
