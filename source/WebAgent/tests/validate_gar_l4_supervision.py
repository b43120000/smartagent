"""Offline GAR L4 WebGPT2 advisory -> WebGPT1 resumption tests.

Run from repo root: PYTHONPATH=source python -B source/WebAgent/tests/validate_gar_l4_supervision.py
No browser, network, Telegram, or workspace mutations outside temp fixtures.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.gar_l4_supervision import (
    SCHEMA, parse_review_response, review_prompt,
)
from agent_core.goal_anchored_recovery import FreshContextProof
from WebAgent.fresh_context import FreshPlannerBinding
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_tri_one_5a_protocol_it import answer_only_response


def review_reply(prompt, *, revised="Inspect blocker and try a safe new strategy"):
    found = re.search(r'"nonce":"([0-9a-f]{32})"', prompt)
    assert found, "runtime review challenge must be present"
    return json.dumps({
        "schema": SCHEMA, "nonce": found.group(1),
        "completed_steps": ["1. prerequisite complete", "2. validation complete"],
        "blocked_step": "3. the previous approach failed",
        "failure_analysis": "The current step is stuck on the same protocol blocker",
        "revised_step": revised,
        "remaining_plan": ["4. continue without repeating prior actions"],
        "safety_notes": ["Preserve original permissions and evidence"],
    })


class ReviewContractTests(unittest.TestCase):
    def test_previous_steps_and_blocker_reach_second_planner(self):
        from agent_core.goal_anchored_recovery import RecoveryEngine
        e = RecoveryEngine(request_id="RR", task_id="TASK", task_epoch="E",
                           original_request="6-step camera validation")
        checkpoint = e.snapshot(identity={"request_id": "RR", "task_id": "TASK",
                                          "task_epoch": "E"}, verified_evidence={})
        prompt = review_prompt(
            goal="6-step camera validation",
            progress={"total_steps": 6, "current_step": 3,
                      "steps": [{"desc": "step 1", "status": "DONE"},
                                {"desc": "step 2", "status": "DONE"},
                                {"desc": "step 3", "status": "BLOCKED"}],
                      "current_focus": "step 3 failed"},
            checkpoint=checkpoint, reason="PROTOCOL_REJECTED", nonce="a" * 32,
        )
        self.assertIn("step 2", prompt)
        self.assertIn("step 3 failed", prompt)
        self.assertIn("PROTOCOL_REJECTED", prompt)
        self.assertNotIn("smartagent_tool", prompt)
        advice = parse_review_response(review_reply(prompt), nonce="a" * 32)
        self.assertEqual(advice["completed_steps"][1], "2. validation complete")

    def test_runtime_observed_semantic_stall_can_request_single_review(self):
        from agent_core.goal_anchored_recovery import RecoveryEngine
        e = RecoveryEngine(request_id="RR", task_id="TASK", task_epoch="E",
                           original_request="6-step camera validation")
        def snapshot(pending=None):
            return e.snapshot(
                identity={"request_id": "RR", "task_id": "TASK", "task_epoch": "E"},
                verified_evidence={}, pending_operations=pending,
            )
        denied = e.request_supervisory_review(
            snapshot=snapshot({"unknown": True}),
            reason="semantic_stagnation:repeated_model_decision_without_new_runtime_evidence",
        )
        self.assertEqual((denied.level, denied.status), ("L5", "PAUSED"))
        e = RecoveryEngine(request_id="RR", task_id="TASK", task_epoch="E",
                           original_request="6-step camera validation")
        proposed = e.request_supervisory_review(
            snapshot=snapshot(),
            reason="semantic_stagnation:repeated_model_decision_without_new_runtime_evidence",
        )
        self.assertEqual((proposed.level, proposed.status), ("L4", "PREPARE"))
        self.assertEqual(e.rejected_rounds, 0)
        duplicate = e.request_supervisory_review(
            snapshot=snapshot(),
            reason="semantic_stagnation:repeated_model_decision_without_new_runtime_evidence",
        )
        self.assertEqual(duplicate.status, "PAUSED")

    def test_reject_tool_blocks_wrong_nonce_duplicate_json_and_extra_fields(self):
        original = review_reply('"nonce":"%s"' % ("b" * 32))
        for raw in (
            original.replace("b" * 32, "c" * 32),
            original + '\n\nextra',
            original.replace('"schema":', '"schema":"EXTRA", "schema":', 1),
            original.replace('"safety_notes":', '"extra":123,"safety_notes":', 1),
            original.replace('"revised_step":', '"smartagent_tool":1,"revised_step":', 1),
        ):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError):
                    parse_review_response(raw, nonce="b" * 32)


class SupervisorRuntimeIT(unittest.TestCase):
    @staticmethod
    def adapter(seen, releases, *, invalid=False):
        class Adapter:
            def prepare(self, *, request_id, task_id, task_epoch, owner):
                proof = FreshContextProof(
                    request_id=request_id, task_id=task_id,
                    task_epoch=task_epoch, owner=owner,
                    original_conversation_id="webgpt1",
                    fresh_conversation_id="webgpt2",
                    protocol_session_id="GAR-L4-REVIEW",
                    protocol_identity_verified=True,
                    page_ownership_verified=True,
                    previous_generation_inactive=True,
                )
                def review(prompt):
                    seen.append(prompt)
                    return '{"bad":"review"}' if invalid else review_reply(prompt)
                def not_planner(*args):
                    raise AssertionError("WebGPT2 must never execute V9 tool calls")
                return FreshPlannerBinding(
                    proof=proof, planner=not_planner, review=review,
                    close=lambda: releases.append("released"),
                )
        return Adapter()

    def test_l4_review_returns_to_original_v9_executor(self):
        with tempfile.TemporaryDirectory(prefix="gar-l4-supervisor-") as tmp:
            calls, reviews, releases, events = [], [], [], []
            def webgpt1(prompt, expected, attachments):
                calls.append(prompt)
                return ("invalid V9 protocol" if len(calls) <= 7
                        else answer_only_response(text="Original WebGPT1 accepted."))
            loop = WebAgentProtocolLoop(
                Path(tmp), webgpt1, progress_root=Path(tmp), max_turns=12,
                fresh_context_adapter=self.adapter(reviews, releases),
                event_sink=lambda name, **data: events.append((name, data)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                result = loop.run(
                    "Explain RAII in C++.",
                    request_id="RR-SUPERVISOR", task_id="TASK-SUPERVISOR",
                    task_epoch="EPOCH-SUPERVISOR",
                    source_tag="REMOTEAGENT_WEBCOPILOT_V9",
                )
            self.assertEqual(result, "Original WebGPT1 accepted.")
            self.assertEqual(len(calls), 8)
            self.assertEqual(len(reviews), 1)
            self.assertEqual(releases, ["released"])
            self.assertIn("GAR_L4_WEBGPT2_ADVISORY_NOT_EVIDENCE", calls[-1])
            self.assertIs(loop.planner, webgpt1)
            self.assertEqual(loop._gar.fresh_sessions, 1)
            self.assertTrue(loop._gar.diagnostic()["supervisor_review_returned"])
            self.assertEqual(loop.terminal_outcome, "SUCCESS")
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})
            names = [n for n, _ in events]
            self.assertIn("gar_l4_webgpt1_paused_for_review", names)
            self.assertIn("gar_l4_webgpt2_advice_received", names)
            self.assertIn("gar_l4_webgpt1_resumed", names)
            self.assertNotIn("gar_fresh_planner_activated", names)

    def test_invalid_webgpt2_review_pauses_without_old_replay(self):
        with tempfile.TemporaryDirectory(prefix="gar-l4-review-rejected-") as tmp:
            calls, reviews, releases = [], [], []
            def webgpt1(*args):
                calls.append(1)
                return "invalid V9"
            loop = WebAgentProtocolLoop(
                Path(tmp), webgpt1, progress_root=Path(tmp), max_turns=12,
                fresh_context_adapter=self.adapter(reviews, releases, invalid=True),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "1",
            }):
                with self.assertRaisesRegex(RuntimeError, "GAR_L4_SUPERVISOR_REVIEW_UNVERIFIED"):
                    loop.run("Explain RAII.",
                             request_id="RR-NEG", task_id="TASK-NEG",
                             task_epoch="EPOCH-NEG",
                             source_tag="REMOTEAGENT_WEBCOPILOT_V9")
            self.assertEqual(len(calls), 7)
            self.assertEqual(len(reviews), 1)
            self.assertEqual(releases, ["released"])
            self.assertFalse(loop.terminal_acceptance)
            self.assertIs(loop.planner, webgpt1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
