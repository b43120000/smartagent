"""GAR empty-evidence Progress/terminal boundary regression; offline only.

Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_gar_terminal_empty_evidence_boundary.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.task_progress import (
    TaskProgressError, record_model_progress, validate_model_progress,
)


def terminal_progress() -> dict:
    return {
        "tool": "report_progress",
        "action_id": "P-ANSWER-BOUNDARY",
        "base_evaluation": "User requests a conceptual explanation only.",
        "total_steps": 1,
        "current_step": 1,
        "steps": [{"step": 1, "desc": "Explain the concept",
                   "status": "COMPLETED"}],
        "current_focus": "Explained the concept",
        "completion_contract": {
            "success": ["Concept explained"],
            "failure": ["Could not explain"],
            "in_progress": ["Preparing explanation"],
            "interrupted": ["Explanation blocked"],
        },
        "decision": "COMPLETE",
        "outcome": "SUCCESS",
        "matched_condition": "Concept explained",
        "evidence_refs": [],
        "decision_reason": "No tool was used for this conceptual response.",
    }


class EmptyTerminalEvidenceBoundary(unittest.TestCase):
    def test_default_progress_validator_still_requires_evidence(self):
        with self.assertRaisesRegex(TaskProgressError, "evidence_refs"):
            validate_model_progress(terminal_progress())
        with self.assertRaisesRegex(TaskProgressError, "evidence_refs"):
            validate_model_progress({**terminal_progress(),
                                     "evidence_refs": None},
                                    allow_empty_terminal_candidate=True)

    def test_explicit_terminal_candidate_is_not_verification(self):
        progress = validate_model_progress(
            terminal_progress(), allow_empty_terminal_candidate=True,
        )
        self.assertEqual(progress["evidence_refs"], [])
        self.assertEqual(progress["decision"], "COMPLETE")
        self.assertEqual(progress["outcome"], "SUCCESS")
        for changed in (
            {"outcome": "FAILED", "matched_condition": "Could not explain"},
            {"outcome": "PARTIAL", "matched_condition": "Could not explain"},
            {"decision": "CONTINUE", "outcome": "PENDING",
             "current_step": 0, "next_action": "Gather evidence",
             "steps": [{"step": 1, "desc": "Explain the concept",
                        "status": "IN_PROGRESS"}],
             "matched_condition": "Preparing explanation"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(TaskProgressError, "evidence_refs"):
                    validate_model_progress(
                        {**terminal_progress(), **changed},
                        allow_empty_terminal_candidate=True,
                    )

    def test_only_explicitly_admitted_terminal_can_be_persisted(self):
        with tempfile.TemporaryDirectory(prefix="gar-boundary-") as directory:
            root = Path(directory)
            with self.assertRaisesRegex(TaskProgressError, "evidence_refs"):
                record_model_progress(
                    "TASK-GAR-BOUNDARY", terminal_progress(),
                    request_id="RR-GAR-BOUNDARY", goal="Explain RAII.",
                    round_id=1, root=root,
                )
            ledger = record_model_progress(
                "TASK-GAR-BOUNDARY", terminal_progress(),
                request_id="RR-GAR-BOUNDARY", goal="Explain RAII.",
                round_id=1, root=root,
                allow_empty_terminal_candidate=True,
            )
            self.assertEqual(ledger.evidence_refs, [])
            self.assertEqual(ledger.outcome, "SUCCESS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
