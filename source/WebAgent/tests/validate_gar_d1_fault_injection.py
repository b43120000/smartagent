"""Offline GAR D1 one-shot injection and true V9 L4 activation tests.

PYTHONPATH=source python -B source/WebAgent/tests/validate_gar_d1_fault_injection.py
No CDP/real browser/Telegram network is used here.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAgent.controller import GARD1LiveFaultInjector
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.fresh_context import FreshPlannerBinding
from agent_core.goal_anchored_recovery import FreshContextProof
from WebAgent.tests.validate_tri_one_5a_protocol_it import answer_only_response


class D1FaultInjectionTest(unittest.TestCase):
    def test_default_off_passes_through_without_injection(self):
        real = []
        events = []
        fault = GARD1LiveFaultInjector(
            lambda prompt, expected, files: real.append(prompt) or "REAL_REPLY",
            lambda name, **kwargs: events.append(name),
        )
        self.assertFalse(fault.arm(fault.TEST_REQUEST, eligible=True))
        self.assertEqual(fault.planner("prompt", {}, []), "REAL_REPLY")
        fault.disarm()
        self.assertEqual(len(real), 1)
        self.assertEqual(events, [])

    def test_only_exact_authorized_single_request_is_injected(self):
        real = []
        emitted = []
        fault = GARD1LiveFaultInjector(
            lambda prompt, expected, files: real.append(prompt) or "REAL",
            lambda name, **kwargs: emitted.append((name, kwargs)),
            enabled=True,
        )
        self.assertFalse(fault.arm("Delete a file.", eligible=True))
        self.assertFalse(fault.arm("Explain RAII in C++.", eligible=False))
        self.assertFalse(fault.arm("explain RAII in C++.", eligible=True))
        self.assertTrue(fault.arm(fault.TEST_REQUEST, eligible=True))
        self.assertEqual(fault.planner("first", {}, []), "GAR_D1_INVALID_V9")
        fault.disarm()
        self.assertFalse(fault.arm(fault.TEST_REQUEST, eligible=True))
        self.assertEqual(fault.planner("after", {}, []), "REAL")
        self.assertEqual(len(real), 1)
        # New D1 rejection telemetry is an expected part of the safety
        # contract. Denied requests do not consume the single-use ticket.
        self.assertEqual(
            [(name, fields.get("reason")) for name, fields in emitted],
            [
                ("gar_d1_test_not_armed", "REQUEST_NOT_EXACT_MATCH"),
                ("gar_d1_test_not_armed", "INPUT_NOT_ELIGIBLE"),
                ("gar_d1_test_not_armed", "REQUEST_NOT_EXACT_MATCH"),
                ("gar_d1_test_armed", None),
                ("gar_d1_test_fault_injected", None),
                ("gar_d1_test_disarmed", None),
                ("gar_d1_test_not_armed", "SINGLE_USE_CONSUMED"),
            ],
        )
        self.assertTrue(all("request" not in details and "prompt" not in details
                            for _, details in emitted))
        self.assertEqual(fault.injected, 1)

    def test_enabled_d1_reports_exact_not_armed_reason_without_prompt(self):
        emitted = []
        real = []
        fault = GARD1LiveFaultInjector(
            lambda *args: real.append(args) or "REAL",
            lambda name, **fields: emitted.append((name, fields)),
            enabled=True,
        )
        self.assertFalse(fault.arm("解釋 RAII", eligible=True))
        self.assertFalse(fault.arm(fault.TEST_REQUEST, eligible=False))
        self.assertEqual(
            [(name, data["reason"]) for name, data in emitted],
            [
                ("gar_d1_test_not_armed", "REQUEST_NOT_EXACT_MATCH"),
                ("gar_d1_test_not_armed", "INPUT_NOT_ELIGIBLE"),
            ],
        )
        self.assertTrue(all("request" not in data and "prompt" not in data
                            for _, data in emitted))
        self.assertEqual(real, [])
        self.assertTrue(fault.arm(fault.TEST_REQUEST, eligible=True))
        self.assertFalse(fault.arm(fault.TEST_REQUEST, eligible=True))
        self.assertEqual(emitted[-1][1]["reason"], "SINGLE_USE_CONSUMED")
        fault.disarm()
        self.assertEqual(real, [])

    def test_never_falls_through_to_real_planner_on_unsafe_injection(self):
        real = []
        fault = GARD1LiveFaultInjector(
            lambda *args: real.append(args) or "REAL",
            lambda name, **kwargs: None, enabled=True,
        )
        self.assertTrue(fault.arm(fault.TEST_REQUEST, eligible=True))
        with self.assertRaisesRegex(RuntimeError, "SCOPE_OR_BUDGET_EXCEEDED"):
            fault.planner("prompt", {}, ["unexpected.txt"])
        for _ in range(fault.MAX_INJECTED_REJECTIONS):
            self.assertEqual(fault.planner("prompt", {}, []), fault.INVALID_V9)
        with self.assertRaisesRegex(RuntimeError, "SCOPE_OR_BUDGET_EXCEEDED"):
            fault.planner("prompt", {}, [])
        self.assertEqual(real, [])
        fault.disarm()

    def test_v2_release_requires_verified_l4_after_all_seven_faults(self):
        observed = []
        real_calls = []
        fault = GARD1LiveFaultInjector(
            lambda *args: real_calls.append(args) or "REAL",
            lambda name, **fields: observed.append((name, fields)),
            enabled=True,
        )
        self.assertTrue(fault.arm(fault.TEST_REQUEST, eligible=True))
        valid = {
            "planner_owner": "ORIGINAL_WEBGPT1",
            "original_ack_preserved": True,
            "supervisor_review_returned": True,
            "level": "L4",
        }
        self.assertFalse(fault.release_after_verified_l4(
            "gar_l4_webgpt1_resumed", valid))
        for _ in range(7):
            self.assertEqual(fault.planner("prompt", {}, []), fault.INVALID_V9)
        self.assertFalse(fault.release_after_verified_l4(
            "gar_l4_webgpt2_advice_received", valid))
        self.assertFalse(fault.release_after_verified_l4(
            "gar_l4_webgpt1_resumed", {**valid, "supervisor_review_returned": False}))
        with self.assertRaisesRegex(RuntimeError, "SCOPE_OR_BUDGET_EXCEEDED"):
            fault.planner("prompt", {}, [])
        self.assertTrue(fault.release_after_verified_l4(
            "gar_l4_webgpt1_resumed", valid))
        self.assertFalse(fault.release_after_verified_l4(
            "gar_l4_webgpt1_resumed", valid))
        self.assertEqual(fault.planner("after", {}, []), "REAL")
        self.assertEqual(len(real_calls), 1)
        self.assertEqual(
            [name for name, _ in observed].count("gar_d1_test_resume_release"), 1)
        fault.disarm()

    def test_d1_v2_supervisory_live_equivalent_returns_to_original_planner(self):
        import json
        import re
        from agent_core.gar_l4_supervision import SCHEMA

        with tempfile.TemporaryDirectory(prefix="gar-d1-v2-") as folder:
            root = Path(folder)
            emitted, real_calls, reviews, released = [], [], [], []

            def real_planner(prompt, expected, files):
                real_calls.append((prompt, expected, files))
                return answer_only_response(text="Original WebGPT1: RAII complete.")

            fault = GARD1LiveFaultInjector(
                real_planner,
                lambda name, **data: emitted.append((name, data)),
                enabled=True,
            )

            class Supervisor:
                def prepare(self, *, request_id, task_id, task_epoch, owner):
                    proof = FreshContextProof(
                        request_id=request_id, task_id=task_id,
                        task_epoch=task_epoch, owner=owner,
                        original_conversation_id="original",
                        fresh_conversation_id="second",
                        protocol_session_id="GAR-D1-V2-BOOTSTRAP",
                        protocol_identity_verified=True,
                        page_ownership_verified=True,
                        previous_generation_inactive=True,
                    )

                    def review(prompt):
                        reviews.append(prompt)
                        found = re.search(r'"nonce":"([0-9a-f]{32})"', prompt)
                        assert found
                        return json.dumps({
                            "schema": SCHEMA,
                            "nonce": found.group(1),
                            "completed_steps": [],
                            "blocked_step": "V9 planner envelope rejected",
                            "failure_analysis": "missing_smartagent_tool_envelope",
                            "revised_step": "Return to original WebGPT1 with valid V9",
                            "remaining_plan": ["Answer the original RAII question"],
                            "safety_notes": ["No executed actions to replay"],
                        })

                    def no_planner(*args):
                        raise AssertionError("WebGPT2 must remain read-only")

                    return FreshPlannerBinding(
                        proof=proof, planner=no_planner, review=review,
                        close=lambda: released.append(True),
                    )

            def trusted_sink(name, **data):
                emitted.append((name, data))
                fault.release_after_verified_l4(name, data)

            loop = WebAgentProtocolLoop(
                root, fault.planner, progress_root=root, max_turns=12,
                fresh_context_adapter=Supervisor(), event_sink=trusted_sink,
            )
            self.assertTrue(fault.arm(fault.TEST_REQUEST, eligible=True))
            try:
                with patch.dict(os.environ, {
                    "SMARTAGENT_GAR_V1": "1",
                    "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "1",
                    "TRI_ONE_TASK_RESULT_V1": "1",
                    "TRI_ONE_TEXT_ONLY_V1": "1",
                }):
                    result = loop.run(
                        fault.TEST_REQUEST, request_id="GAR-D1-V2-RR",
                        task_id="GAR-D1-V2-TASK", task_epoch="GAR-D1-V2-EPOCH",
                        source_tag="",
                    )
            finally:
                fault.disarm()
            names = [name for name, _ in emitted]
            self.assertEqual(result, "Original WebGPT1: RAII complete.")
            self.assertEqual(fault.injected, 7)
            self.assertEqual(len(real_calls), 1)
            self.assertEqual(len(reviews), 1)
            self.assertEqual(released, [True])
            self.assertIn("gar_l4_webgpt1_paused_for_review", names)
            self.assertIn("gar_l4_webgpt2_advice_received", names)
            self.assertIn("gar_l4_webgpt1_resumed", names)
            self.assertIn("gar_d1_test_resume_release", names)
            self.assertEqual(loop.terminal_outcome, "SUCCESS")
            self.assertEqual(loop.terminal_acceptance["admission_status"], "ACCEPTED")
            self.assertTrue(any("GAR_L4_WEBGPT2_ADVISORY_NOT_EVIDENCE"
                                in call[0] for call in real_calls))
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})

    def test_fake_planner_d1_escalates_and_real_v9_terminal_is_admitted(self):
        with tempfile.TemporaryDirectory(prefix="gar-d1-inject-") as folder:
            root = Path(folder)
            emitted = []
            original_real_calls, fresh_calls, releases = [], [], []
            fault = GARD1LiveFaultInjector(
                lambda *args: original_real_calls.append(args) or "REAL",
                lambda name, **data: emitted.append((name, data)),
                enabled=True,
            )

            class Fresh:
                def prepare(self, *, request_id, task_id, task_epoch, owner):
                    proof = FreshContextProof(
                        request_id=request_id, task_id=task_id,
                        task_epoch=task_epoch, owner=owner,
                        original_conversation_id="original",
                        fresh_conversation_id="second",
                        protocol_session_id="GAR-D1-TEST-BOOTSTRAP",
                        protocol_identity_verified=True,
                        page_ownership_verified=True,
                        previous_generation_inactive=True,
                    )
                    def planner(prompt, expected, attachments):
                        fresh_calls.append((prompt, expected, attachments))
                        return answer_only_response(
                            text="RAII ties resource lifetime to object lifetime."
                        )
                    return FreshPlannerBinding(
                        proof=proof, planner=planner,
                        close=lambda: releases.append("released"),
                    )

            loop = WebAgentProtocolLoop(
                root, fault.planner, progress_root=root, max_turns=12,
                fresh_context_adapter=Fresh(),
                event_sink=lambda name, **data: emitted.append((name, data)),
            )
            self.assertTrue(fault.arm(fault.TEST_REQUEST, eligible=True))
            try:
                with patch.dict(os.environ, {
                    "SMARTAGENT_GAR_V1": "1",
                    "TRI_ONE_TASK_RESULT_V1": "1",
                    "TRI_ONE_TEXT_ONLY_V1": "1",
                    "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "0",
                }):
                    result = loop.run(
                        fault.TEST_REQUEST, source_tag="",
                        request_id="GAR-D1-WEB-RR", task_id="GAR-D1-WEB-TASK",
                        task_epoch="GAR-D1-WEB-EPOCH",
                    )
            finally:
                fault.disarm()
            names = [name for name, _ in emitted]
            self.assertEqual(fault.injected, 7)
            self.assertEqual(original_real_calls, [])
            self.assertEqual(len(fresh_calls), 1)
            self.assertEqual(releases, ["released"])
            self.assertEqual(loop._gar.fresh_sessions, 1)
            self.assertEqual(loop._gar.rejected_rounds, 7)
            self.assertEqual(loop._bounded_repair.rejected_turns, 7)
            self.assertIn("gar_local_repair_handoff", names)
            self.assertIn("gar_fresh_planner_activated", names)
            decisions = [data for name, data in emitted
                         if name == "gar_recovery_decision"]
            self.assertTrue(any(d.get("level") == "L2" for d in decisions))
            self.assertTrue(any(d.get("level") == "L3" for d in decisions))
            self.assertTrue(any(d.get("level") == "L4" for d in decisions))
            self.assertEqual(
                loop.terminal_acceptance.get("admission_status"), "ACCEPTED")
            self.assertEqual(
                loop.terminal_acceptance.get("completion_kind"), "ANSWER_ONLY")
            self.assertEqual(result,
                             "RAII ties resource lifetime to object lifetime.")
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})


if __name__ == "__main__":
    unittest.main()
