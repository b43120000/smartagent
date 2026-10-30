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
        self.assertEqual([name for name, _ in emitted],
                         ["gar_d1_test_armed", "gar_d1_test_fault_injected",
                          "gar_d1_test_disarmed"])
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
