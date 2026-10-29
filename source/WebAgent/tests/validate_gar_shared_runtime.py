"""Offline Goal-Anchored Recovery tests: no browser, model, Telegram or task.

Run (Windows or Linux): PYTHONPATH=source python -B source/WebAgent/tests/validate_gar_shared_runtime.py
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_core.goal_anchored_recovery import (
    RecoveryEngine, classify_failure, enabled, mutation_fingerprint,
)


def make(owner="WEB_COPILOT", goal="Explain C++ RAII."):
    return RecoveryEngine(
        request_id="GAR-RR-1", task_id="GAR-TASK-1",
        task_epoch="GAR-EPOCH-1", original_request=goal, owner=owner,
    )


def snapshot(engine, *, evidence=None, blockers=(), pending=None):
    return engine.snapshot(
        identity={
            "request_id": "GAR-RR-1", "task_id": "GAR-TASK-1",
            "task_epoch": "GAR-EPOCH-1",
        },
        verified_evidence={} if evidence is None else evidence,
        blockers=blockers,
        pending_operations=pending or {},
        progress={"total_steps": 1, "current_step": 0},
    )


class GARSharedRuntimeTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(enabled({}))
        self.assertFalse(enabled({"SMARTAGENT_GAR_V1": "0"}))
        self.assertTrue(enabled({"SMARTAGENT_GAR_V1": "1"}))
        self.assertTrue(enabled({"SMARTAGENT_GAR_V1": "on"}))

    def test_stale_dom_not_new_planner_or_mutation(self):
        engine = make()
        checkpoint = snapshot(engine)
        decision = engine.transport_failure(
            "WebScraperStageError WEB_PROTOCOL_RECOVERY_STALE_READBACK "
            "reason=response_sha_unchanged", checkpoint,
        )
        self.assertEqual((decision.level, decision.status, decision.reason),
                         ("L0", "PAUSED", "STALE_TRANSPORT_READBACK"))
        self.assertFalse(decision.new_session_allowed)
        self.assertFalse(decision.mutation_allowed)
        self.assertEqual(engine.rejected_rounds, 0)
        self.assertEqual(engine.replans, 0)

    def test_transport_cancel_does_not_claim_stale_dom(self):
        engine = make()
        result = engine.transport_failure("KeyboardInterrupt: user cancelled",
                                          snapshot(engine))
        self.assertEqual(result.reason, "FRESHNESS_UNATTESTED")
        self.assertNotEqual(result.reason, "STALE_TRANSPORT_READBACK")

    def test_freshness_classification(self):
        self.assertEqual(classify_failure(
            error="missing progress schema", response_fresh=True,
        ), ("L1", "FRESH_PROTOCOL_REJECTION"))
        self.assertEqual(classify_failure(
            error="WRITER_TIMEOUT", response_fresh=None,
        ), ("L0", "TRANSPORT_UNCERTAIN"))
        self.assertEqual(classify_failure(
            error="anything", response_fresh=False,
        ), ("L0", "STALE_TRANSPORT_READBACK"))

    def test_bounded_l1_l2_l3_l5_with_one_goal(self):
        engine = make()
        got = []
        for _ in range(4):
            step = engine.planner_rejection(
                reason="invalid_progress_payload",
                snapshot=snapshot(engine), response_fresh=True,
                evidence_generation="same-generation",
            )
            got.append((step.level, step.status, step.reason))
            if step.prompt:
                self.assertIn("Explain C++ RAII.", step.prompt)
                self.assertIn("same original", step.prompt.lower())
                self.assertNotIn("PASSWORD=", step.prompt)
            self.assertFalse(step.mutation_allowed)
            self.assertFalse(step.new_session_allowed)
        self.assertEqual([x[0] for x in got], ["L1", "L2", "L3", "L5"])
        self.assertEqual(got[-1][2], "GAR_FRESH_CONTEXT_UNAVAILABLE")
        self.assertEqual(engine.diagnostic()["rejected_rounds"], 4)
        self.assertEqual(engine.diagnostic()["fresh_session_count"], 0)
        exhaustion = engine.planner_rejection(
            reason="invalid_progress_payload", snapshot=snapshot(engine),
            response_fresh=True, evidence_generation="same-generation",
        )
        self.assertEqual(exhaustion.reason, "GAR_SHARED_BUDGET_EXHAUSTED")

    def test_open_unknown_blocks_replan_and_reexecution(self):
        for pending in (
            {"pending_write": {"state": "OPEN"}},
            {"unknown_external_delivery": True},
        ):
            with self.subTest(pending=pending):
                engine = make()
                decision = engine.planner_rejection(
                    reason="missing_progress", snapshot=snapshot(engine, pending=pending),
                    response_fresh=True, evidence_generation="zero",
                )
                self.assertEqual((decision.level, decision.status),
                                 ("L5", "PAUSED"))
                self.assertEqual(decision.reason, "GAR_PENDING_OR_BLOCKED_OPERATION")
                self.assertEqual(engine.rejected_rounds, 0)
        engine = make()
        evidence = {"ACT-7": {"execution_status": "UNKNOWN", "executed": True}}
        step = engine.planner_rejection(
            reason="unknown_side_effect",
            snapshot=snapshot(engine, evidence=evidence), response_fresh=True,
            evidence_generation="g1",
        )
        self.assertEqual(step.reason, "GAR_PENDING_OR_BLOCKED_OPERATION")

    def test_unresolved_blocker_does_not_turn_into_success(self):
        engine = make()
        step = engine.planner_rejection(
            reason="scope_denied",
            snapshot=snapshot(engine, blockers=[
                {"reason": "OPEN_WRITE_TRANSACTION", "satisfied": False}
            ]), response_fresh=True, evidence_generation="g0",
        )
        self.assertEqual(step.status, "PAUSED")
        self.assertEqual(step.reason, "GAR_PENDING_OR_BLOCKED_OPERATION")

    def test_snapshot_identity_and_tampering(self):
        engine = make()
        data = snapshot(engine)
        self.assertTrue(engine.validate_snapshot(data))
        modified = json.loads(json.dumps(data))
        modified["checkpoint"]["verified_evidence"].append({"fake": "PASS"})
        self.assertFalse(engine.validate_snapshot(modified))
        with self.assertRaisesRegex(ValueError, "gar_identity_mismatch"):
            engine.snapshot(
                identity={"request_id": "foreign", "task_id": "GAR-TASK-1",
                          "task_epoch": "GAR-EPOCH-1"},
                verified_evidence={},
            )
        self.assertEqual(engine.planner_rejection(
            reason="invalid_progress_payload",
            snapshot=modified, response_fresh=True,
            evidence_generation="g1",
        ).reason, "GAR_SNAPSHOT_UNVERIFIED")

    def test_negative_fact_is_retained_not_invented(self):
        engine = make()
        data = snapshot(engine, evidence={
            "OBS-1": {"executed": True, "effective_verification_status": "FAIL",
                      "result_status": "SUCCEEDED"}
        })
        self.assertTrue(engine.validate_snapshot(data))
        self.assertEqual(
            data["checkpoint"]["verified_evidence"][0]["verification"], "FAIL"
        )
        self.assertFalse(data["pending_operations"])

    def test_new_evidence_generation_resets_blocker_not_total_budget(self):
        engine = make()
        first = engine.planner_rejection(
            reason="invalid_progress", snapshot=snapshot(engine),
            response_fresh=True, evidence_generation="no-new-evidence",
        )
        second = engine.planner_rejection(
            reason="invalid_progress", snapshot=snapshot(engine),
            response_fresh=True, evidence_generation="verified-new-evidence",
        )
        self.assertEqual((first.level, second.level), ("L1", "L1"))
        self.assertEqual(engine.rejected_rounds, 2)

    def test_same_policy_both_origins(self):
        left = make(owner="TELEGRAM")
        right = make(owner="WEB_COPILOT")
        self.assertEqual(
            left.planner_rejection(
                reason="invalid_progress_payload", snapshot=snapshot(left),
                response_fresh=True, evidence_generation="g",
            ).level,
            right.planner_rejection(
                reason="invalid_progress_payload", snapshot=snapshot(right),
                response_fresh=True, evidence_generation="g",
            ).level,
        )

    def test_persistent_snapshot_redacts_raw_goal(self):
        engine = make(goal="Explain RAII. Authorization: Bearer PRIVATE-SECRET-42")
        data = snapshot(engine, evidence={
            "ACT-PRIVATE-KEY": {"executed": True, "verification_status": "PASS"}
        })
        with tempfile.TemporaryDirectory(prefix="gar-runtime-test-") as directory:
            file = engine.persist_snapshot(data, root=directory)
            self.assertTrue(file.is_file())
            content = file.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE-SECRET-42", content)
            self.assertNotIn("ACT-PRIVATE-KEY", content)
            self.assertNotIn("Explain RAII", content)
            loaded = json.loads(content)
            self.assertTrue(engine.validate_snapshot(loaded))
            self.assertTrue(str(file.resolve()).startswith(
                str(Path(directory).resolve())
            ))

    def test_mutation_fingerprint_blocks_changed_action_id(self):
        a = {"tool": "run_command", "action_id": "ACT-1",
             "command": "echo hello", "operation": "READ_ONLY"}
        b = {"tool": "run_command", "action_id": "ACT-NEW",
             "command": "echo hello", "operation": "READ_ONLY",
             "depends_on": ["other"]}
        c = {**b, "command": "echo different"}
        self.assertEqual(mutation_fingerprint(a), mutation_fingerprint(b))
        self.assertNotEqual(mutation_fingerprint(a), mutation_fingerprint(c))

    def test_cannot_forge_authorization_or_fresh_session(self):
        from agent_core.goal_anchored_recovery import RecoveryDecision
        with self.assertRaisesRegex(ValueError, "gar_cannot_grant"):
            RecoveryDecision("L4", "RETRY", "foo", new_session_allowed=True)
        with self.assertRaisesRegex(ValueError, "gar_cannot_grant"):
            RecoveryDecision("L3", "RETRY", "foo", mutation_allowed=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
