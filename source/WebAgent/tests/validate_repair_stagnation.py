"""Regression for evidence-backed repair stall timers and remote status.

Soft deadline is a continuous stall, not total task age. Immutable global
rejection counts and GAR safeguards are independently owned by Runtime.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.bounded_repair import BoundedRepairBudget
from agent_core.repair_stagnation import (
    RepairStagnation, read_observation, format_status
)


class RepairStallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repair-stall-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tracker = RepairStagnation(
            root=self.root, task_id="TASK-REPAIR-STALLED1",
            request_id="RR-REPAIR1", now=1000)
        self.budget = BoundedRepairBudget()

    def reject(self, when, reason="invalid_progress_payload", gen=0):
        diag=[{"reason":reason}]
        cap=self.budget.observe(diag,evidence_generation=gen)
        self.tracker.rejected(diag,total=cap["rejected_turns_total"],now=when)
        return cap

    def test_elapsed_request_not_equivalent_to_stall(self):
        # 18-minute SDK discovery with three unrelated protocol errors;
        # accepted new evidence breaks the stall between errors.
        self.tracker.accepted_progress(1,now=1001)
        self.reject(1020,"invalid_progress_payload")
        self.assertTrue(self.tracker.accepted_evidence(
            action_id="sdk-opencv",executed=True,verification_status="FAIL",now=1250))
        self.reject(1400,"missing_context_requires_runtime_read",gen=1)
        self.assertTrue(self.tracker.accepted_progress(2,now=1600))
        self.reject(1900,"terminal_success_verification_missing",gen=1)
        self.assertFalse(self.tracker.stalled(now=2140))
        self.assertEqual(self.tracker.rejected_turns_total,3)
        self.assertEqual(self.tracker.rejected_turns_since_progress,1)
        self.assertEqual(self.tracker.last_accepted_progress_at,1600)
        self.assertEqual(self.tracker.last_effective_progress_at,1600)

    def test_genuine_no_progress_stagnation_pauses_after_three_rejects(self):
        for i in range(3):
            self.reject(1005+i*100,"bad_field_"+str(i))
        self.assertFalse(self.tracker.stalled(now=1604))
        self.assertTrue(self.tracker.stalled(now=1605))
        self.assertEqual(self.tracker.rejected_turns_since_progress,3)
        self.assertEqual(self.tracker.last_rejection_reason,"bad_field_2")

    def test_nominal_progress_without_new_step_is_not_effective(self):
        self.tracker.accepted_progress(3,now=1002)
        self.reject(1010)
        self.assertFalse(self.tracker.accepted_progress(3,now=1020))
        self.reject(1040)
        self.reject(1060)
        self.assertTrue(self.tracker.stalled(now=1610))
        self.assertEqual(self.tracker.last_accepted_progress_at,1020)
        self.assertIsNone(self.tracker.last_effective_progress_at)

    def test_unverified_and_duplicate_evidence_cannot_reset(self):
        self.reject(1010)
        self.assertFalse(self.tracker.accepted_evidence(
            action_id="NEW",executed=False,verification_status="PASS",now=1400))
        self.assertFalse(self.tracker.accepted_evidence(
            action_id="NEW",executed=True,verification_status="UNKNOWN",now=1410))
        self.assertTrue(self.tracker.accepted_evidence(
            action_id="NEW",executed=True,verification_status="PASS",now=1420))
        self.reject(1430,gen=1)
        self.assertFalse(self.tracker.accepted_evidence(
            action_id="NEW",executed=True,verification_status="PASS",now=1450))
        self.reject(1460,gen=1)
        self.reject(1470,gen=1)
        self.assertFalse(self.tracker.stalled(now=2000))
        self.assertTrue(self.tracker.stalled(now=2030))

    def test_cumulative_hard_budget_not_reset(self):
        self.reject(1010)
        self.reject(1020)
        self.tracker.accepted_progress(0,now=1025)
        self.tracker.accepted_progress(1,now=1030)
        self.reject(1040,gen=1)
        self.assertEqual(self.budget.rejected_turns,3)
        self.assertEqual(self.tracker.rejected_turns_total,3)
        self.assertEqual(self.tracker.rejected_turns_since_progress,1)

    def test_persistent_remote_status_is_scoped_and_sanitized(self):
        self.tracker.accepted_progress(1,now=1000)
        self.reject(1010,"invalid_progress_payload")
        self.tracker.level("L2")
        doc=read_observation(self.root,"TASK-REPAIR-STALLED1","RR-REPAIR1")
        self.assertEqual(doc["gar_level"],"L2")
        self.assertIsNone(read_observation(self.root,"TASK-REPAIR-STALLED1","RR-OTHER"))
        report=format_status(doc,now=1130)
        for term in ("Rejected Turns: 1","Repair 停滯時間: 120 / 600 秒",
                     "GAR Level: L2","invalid_progress_payload",
                     "最後接受 Progress","最後有效 Evidence","最後 Rejection"):
            self.assertIn(term,report)
        self.assertNotIn("C:\\", report)

    def test_untrusted_reason_redacted_not_echoed(self):
        self.reject(1020,"DROP TABLE; token=SECRET")
        self.assertEqual(self.tracker.last_rejection_reason,"REDACTED_REASON")
        text=format_status(self.tracker.snapshot(),now=1100)
        self.assertNotIn("SECRET",text)

    def test_no_history_does_not_force_pause(self):
        self.assertFalse(self.tracker.stalled(now=999999))
        self.assertEqual(self.tracker.rejected_turns_total,0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
