"""GAR offline V9 Runtime wiring smoke: no browser or network.

Run with PYTHONPATH=source:
    python -B source/WebAgent/tests/validate_gar_protocol_loop_it.py
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.task_progress import read_progress
from agent_core.goal_anchored_recovery import FreshContextProof
from WebAgent.fresh_context import FreshPlannerBinding
from WebAgent.tests.validate_tri_one_5a_protocol_it import answer_only_response


class GARProtocolIT(unittest.TestCase):
    def test_unverified_terminal_precondition_is_not_unknown_side_effect(self):
        # Real V9 parser and terminal gate: first response is a valid V9
        # terminal SUCCESS without evidence; with 5A OFF it must be rejected,
        # but GAR must not mistake that *protocol requirement* for a tool
        # executing with UNKNOWN outcome.
        with tempfile.TemporaryDirectory(prefix="gar-terminal-prereq-") as directory:
            root = Path(directory)
            events = []
            replies = []
            def planner(prompt, expected, attachments):
                replies.append(prompt)
                return answer_only_response(text="RAII releases resources.")
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=2,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "0",
                "TRI_ONE_TEXT_ONLY_V1": "0",
            }):
                with self.assertRaises(Exception):
                    loop.run(
                        "Explain RAII.", source_tag="REMOTEAGENT_WEBCOPILOT_V9",
                        request_id="GAR-PRE-RR", task_id="GAR-PRE-TASK",
                        task_epoch="GAR-PRE-EPOCH",
                    )
            decisions = [row for name, row in events
                         if name == "gar_recovery_decision"]
            self.assertGreaterEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["last_reason"],
                             "GAR_LOCAL_REPAIR_ACTIVE")
            self.assertGreaterEqual(loop._gar.rejected_rounds, 1)
            self.assertTrue(loop.pending_verification_requirement)
            self.assertFalse(loop.terminal_acceptance)
            self.assertFalse(loop.action_result_ledger)
            self.assertFalse(loop.action_ledger)
            files = list((root / "localdata" / "runtime" /
                          "gar_snapshots").glob("*.json"))
            self.assertEqual(len(files), 1)
            import json
            checkpoint = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["pending_operations"], [])
            self.assertIn("UNVERIFIED_TERMINAL_NO_EXECUTION",
                          checkpoint["verification_preconditions"])

    def test_request_accepted_only_is_advisory_in_real_web_direct_v9(self):
        # Live C1 reproduction: Progress cites REQUEST_ACCEPTED without tools.
        # V9 must reject SUCCESS, but GAR must not invent an UNKNOWN side effect.
        with tempfile.TemporaryDirectory(prefix="gar-c1-request-accepted-") as directory:
            root = Path(directory)
            events, planner_calls = [], []
            def planner(prompt, expected, attachments):
                planner_calls.append(prompt)
                reply = answer_only_response(text="RAII releases resources.")
                marker = '"evidence_refs":[]'
                assert reply.count(marker) == 1
                return reply.replace(marker, '"evidence_refs":["REQUEST_ACCEPTED"]', 1)
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=2,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                with self.assertRaises(Exception):
                    loop.run(
                        "Explain RAII.", source_tag="",
                        request_id="GAR-C1-WEB-RR", task_id="GAR-C1-WEB-TASK",
                        task_epoch="GAR-C1-WEB-EPOCH",
                    )
            rejected = [row for name, row in events if name == "protocol_rejected"]
            self.assertGreaterEqual(len(rejected), 1)
            # Shared Answer-only now returns an explicit model-repair
            # diagnostic for non-action status refs. It must NEVER convert
            # them into verified Action Evidence or terminal SUCCESS.
            self.assertTrue(any(d.get("reason") == "answer_only_status_markers_not_action_evidence"
                                for d in rejected[0].get("diagnostics", [])))
            candidates = [row for name, row in events
                          if name == "tri_one_answer_only_candidate"]
            self.assertTrue(candidates)
            self.assertFalse(candidates[0]["eligible"])
            self.assertEqual(candidates[0]["reason"],
                             "action_evidence_cannot_be_answer_only")
            classified = [row for name, row in events
                          if name == "gar_pending_classification"]
            # No pending Action verification is manufactured by this
            # fail-closed status-marker repair path.
            self.assertFalse(classified)
            decisions = [row for name, row in events
                         if name == "gar_recovery_decision"]
            self.assertTrue(decisions)
            self.assertEqual((decisions[0]["level"], decisions[0]["last_reason"]),
                             ("L1", "GAR_LOCAL_REPAIR_ACTIVE"))
            self.assertGreaterEqual(loop._gar.rejected_rounds, 1)
            self.assertFalse(loop.pending_verification_requirement)
            self.assertFalse(loop.terminal_acceptance)
            self.assertFalse(loop.action_ledger)
            self.assertFalse(loop.action_result_ledger)
            import json
            checkpoints = list((root / "localdata" / "runtime" /
                               "gar_snapshots").glob("*.json"))
            self.assertEqual(len(checkpoints), 1)
            checkpoint = json.loads(checkpoints[0].read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["pending_operations"], [])
            self.assertEqual(checkpoint["checkpoint"]["verified_evidence"], [])
            self.assertFalse(checkpoint["checkpoint"]["verified_evidence"])
            self.assertGreaterEqual(len(planner_calls), 2)

    def test_narrative_status_marker_is_advisory_in_real_web_direct_v9(self):
        with tempfile.TemporaryDirectory(prefix="gar-d1-marker-") as directory:
            root = Path(directory)
            events = []
            def planner(prompt, expected, attachments):
                reply = answer_only_response(text="RAII releases resources.")
                marker = '"evidence_refs":[]'
                assert reply.count(marker) == 1
                return reply.replace(marker, '"evidence_refs":["RUNTIME_STATUS"]', 1)
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=2,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                with self.assertRaises(Exception):
                    loop.run("Explain RAII.", source_tag="",
                             request_id="GAR-D1-MARKER-RR",
                             task_id="GAR-D1-MARKER-TASK",
                             task_epoch="GAR-D1-MARKER-EPOCH")
            # The strict shared Answer-only contract explicitly rejects a
            # model's RUNTIME_STATUS citation before constructing an Action
            # verification requirement. The rejection is a repair prompt.
            rejections = [data for name, data in events if name == "protocol_rejected"]
            self.assertTrue(rejections)
            self.assertTrue(any(
                d.get("reason") == "answer_only_status_markers_not_action_evidence"
                for d in rejections[0].get("diagnostics", [])
            ))
            decisions = [data for name, data in events
                         if name == "gar_recovery_decision"]
            self.assertTrue(decisions)
            self.assertEqual((decisions[0]["level"], decisions[0]["last_reason"]),
                             ("L1", "GAR_LOCAL_REPAIR_ACTIVE"))
            self.assertFalse(loop.terminal_acceptance)
            self.assertFalse(loop.action_result_ledger)
            self.assertFalse(loop.pending_verification_requirement)

    def test_stale_readback_cannot_reexecute_or_complete(self):
        with tempfile.TemporaryDirectory(prefix="gar-stale-") as directory:
            root = Path(directory)
            calls = []
            events = []
            def planner(prompt, expected, attachments):
                calls.append(dict(expected))
                raise RuntimeError(
                    "WEB_PROTOCOL_RECOVERY_STALE_READBACK "
                    "response_sha_unchanged action_executed=false"
                )
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root,
                event_sink=lambda name, **fields: events.append((name, fields)),
                max_turns=6,
            )
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "1"}):
                with self.assertRaisesRegex(RuntimeError, "STALE_READBACK"):
                    loop.run(
                        "Explain RAII in C++.",
                        request_id="GAR-IT-RR-1", task_id="GAR-IT-TASK-1",
                        task_epoch="GAR-IT-EPOCH-1",
                        source_tag="REMOTEAGENT_WEBCOPILOT_V9",
                    )
            self.assertEqual(len(calls), 1)
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.terminal_acceptance, {})
            self.assertEqual(loop._gar.diagnostic()["last_reason"],
                             "STALE_TRANSPORT_READBACK")
            progress = read_progress("GAR-IT-TASK-1", root=root)
            self.assertEqual(progress.runtime_state, "PAUSED")
            checkpoint = tuple((root / "localdata" / "runtime" /
                                "gar_snapshots").glob("*.json"))
            self.assertEqual(len(checkpoint), 1)
            self.assertTrue(any(name == "gar_transport_integrity"
                                for name, _ in events))

    def test_l4_fresh_planner_can_complete_text_only_goal(self):
        # Explicit backward-compatibility test; new v1 defaults to supervisor.
        with tempfile.TemporaryDirectory(prefix="gar-l4-success-") as directory:
            root = Path(directory)
            old_calls, fresh_calls, releases, events = [], [], [], []
            class FreshAdapter:
                def prepare(self, *, request_id, task_id, task_epoch, owner):
                    proof = FreshContextProof(
                        request_id=request_id, task_id=task_id,
                        task_epoch=task_epoch, owner=owner,
                        original_conversation_id="old-page",
                        fresh_conversation_id="fresh-page",
                        protocol_session_id="GAR-BOOTSTRAP-FRESH",
                        protocol_identity_verified=True,
                        page_ownership_verified=True,
                        previous_generation_inactive=True,
                    )
                    def fresh(prompt, expected, attachments):
                        fresh_calls.append((prompt, expected))
                        return answer_only_response(text="RAII releases resources.")
                    return FreshPlannerBinding(
                        proof=proof, planner=fresh,
                        close=lambda: releases.append("done"),
                    )
            def old(prompt, expected, attachments):
                old_calls.append(prompt)
                return "invalid V9 protocol envelope"
            loop = WebAgentProtocolLoop(
                root, old, progress_root=root, max_turns=12,
                fresh_context_adapter=FreshAdapter(),
                event_sink=lambda name, **data: events.append((name, data)),
            )
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
                "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "0",
            }):
                final = loop.run(
                    "Explain RAII in C++.",
                    request_id="RR-GAR-L4-PASS", task_id="TASK-GAR-L4-PASS",
                    task_epoch="EPOCH-GAR-L4-PASS",
                    source_tag="REMOTEAGENT_WEBCOPILOT_V9",
                )
            self.assertEqual(final, "RAII releases resources.")
            self.assertEqual(len(fresh_calls), 1)
            self.assertEqual(len(old_calls), 7)
            self.assertEqual(releases, ["done"])
            self.assertIs(loop.planner, old)
            observed = [name for name, _ in events]
            self.assertIn("gar_local_repair_handoff", observed)
            self.assertIn("gar_fresh_planner_activated", observed)
            self.assertEqual(loop._gar.rejected_rounds, 7)
            self.assertEqual(loop._bounded_repair.rejected_turns, 7)
            self.assertEqual(loop._gar.fresh_sessions, 1)
            self.assertEqual(loop.terminal_outcome, "SUCCESS")
            self.assertEqual(loop.terminal_acceptance["admission_status"], "ACCEPTED")
            self.assertEqual(loop.terminal_acceptance["completion_kind"], "ANSWER_ONLY")
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})

    def test_l4_new_planner_replaces_old_after_verified_preparation(self):
        with tempfile.TemporaryDirectory(prefix="gar-l4-it-") as directory:
            root = Path(directory)
            original_calls = []
            fresh_calls = []
            prepares = []
            releases = []
            class FakeAdapter:
                def prepare(self, *, request_id, task_id, task_epoch, owner):
                    prepares.append((request_id, task_id, task_epoch, owner))
                    proof = FreshContextProof(
                        request_id=request_id, task_id=task_id,
                        task_epoch=task_epoch, owner=owner,
                        original_conversation_id="old-session",
                        fresh_conversation_id="fresh-session",
                        protocol_session_id="GAR-L4-PROTOCOL",
                        protocol_identity_verified=True,
                        page_ownership_verified=True,
                        previous_generation_inactive=True,
                    )
                    def planner(prompt, expected, attachments):
                        fresh_calls.append(prompt)
                        return "still invalid protocol"
                    return FreshPlannerBinding(
                        proof=proof, planner=planner,
                        close=lambda: releases.append("released"),
                    )
            def old_planner(prompt, expected, attachments):
                original_calls.append(prompt)
                return "invalid smartagent_tool payload"
            loop = WebAgentProtocolLoop(
                root, old_planner, progress_root=root, max_turns=10,
                fresh_context_adapter=FakeAdapter(),
            )
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "1", "SMARTAGENT_GAR_L4_SUPERVISOR_V1": "0"}):
                with self.assertRaises(RuntimeError):
                    loop.run(
                        "Explain RAII.", request_id="RR-L4-IT",
                        task_id="TASK-L4-IT", task_epoch="EPOCH-L4-IT",
                        source_tag="REMOTEAGENT_WEBCOPILOT_V9",
                    )
            self.assertEqual(len(prepares), 1)
            self.assertEqual(len(original_calls), 7)
            self.assertEqual(len(fresh_calls), 3)
            self.assertEqual(releases, ["released"])
            self.assertIs(loop.planner, old_planner)
            self.assertEqual(loop._gar.fresh_sessions, 1)
            self.assertEqual(loop._gar.diagnostic()["fresh_repair_attempts"], 2)
            self.assertTrue(loop._gar.diagnostic()["old_planner_fenced"])
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.terminal_acceptance, {})

    def test_l4_invalid_proof_never_switches_planner(self):
        with tempfile.TemporaryDirectory(prefix="gar-l4-negative-") as directory:
            root = Path(directory)
            fresh_calls = []
            class BadAdapter:
                def prepare(self, **kw):
                    return FreshPlannerBinding(
                        proof=FreshContextProof(
                            request_id=kw["request_id"],
                            task_id=kw["task_id"],
                            task_epoch="wrong-epoch", owner=kw["owner"],
                            original_conversation_id="same",
                            fresh_conversation_id="same",
                            protocol_session_id="BAD",
                            protocol_identity_verified=True,
                            page_ownership_verified=True,
                            previous_generation_inactive=True,
                        ),
                        planner=lambda *_: fresh_calls.append(True),
                        close=lambda: None,
                    )
            loop = WebAgentProtocolLoop(
                root, lambda *_: "invalid protocol",
                progress_root=root, max_turns=10,
                fresh_context_adapter=BadAdapter(),
            )
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "1"}):
                with self.assertRaisesRegex(RuntimeError, "GAR_L4_PROOF_MISMATCH"):
                    loop.run("Explain RAII.", source_tag="REMOTEAGENT_TELEGRAM")
            self.assertEqual(fresh_calls, [])
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.terminal_acceptance, {})

    def test_default_off_does_not_activate_new_gar(self):
        with tempfile.TemporaryDirectory(prefix="gar-default-off-") as directory:
            root = Path(directory)
            loop = WebAgentProtocolLoop(
                root, lambda *_: (_ for _ in ()).throw(
                    RuntimeError("WEB_PROTOCOL_RECOVERY_STALE_READBACK")
                ), progress_root=root, max_turns=1,
            )
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "0"}):
                with self.assertRaises(RuntimeError):
                    loop.run("Explain RAII.")
            self.assertIsNone(loop._gar)
            self.assertFalse((root / "localdata" / "runtime" /
                              "gar_snapshots").exists())

    def test_gar_off_keeps_legacy_repeated_rejection_pause(self):
        with tempfile.TemporaryDirectory(prefix="gar-off-legacy-") as directory:
            root = Path(directory)
            received = []
            def invalid_reply(prompt, expected, attachments):
                received.append(prompt)
                return "invalid smartagent_tool payload"
            loop = WebAgentProtocolLoop(
                root, invalid_reply, progress_root=root, max_turns=8,
            )
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "0"}):
                with self.assertRaisesRegex(RuntimeError, "連續回傳相同"):
                    loop.run("Explain RAII.", source_tag="REMOTEAGENT_WEBCOPILOT_V9")
            self.assertEqual(len(received), 2)
            self.assertIsNone(loop._gar)
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.terminal_acceptance, {})

    def test_duplicate_invalid_fresh_planner_reply_bounded(self):
        with tempfile.TemporaryDirectory(prefix="gar-fresh-") as directory:
            root = Path(directory)
            prompts = []
            def planner(prompt, expected, attachments):
                prompts.append(prompt)
                # Malformed V9 response; no valid action or observable result.
                return "not a smartagent_tool envelope"
            loop = WebAgentProtocolLoop(root, planner, progress_root=root,
                                        max_turns=8)
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "1"}):
                with self.assertRaises(RuntimeError):
                    loop.run("Explain RAII in C++.",
                             source_tag="REMOTEAGENT_WEBCOPILOT_V9")
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop.terminal_acceptance, {})
            self.assertGreaterEqual(loop._gar.rejected_rounds, 2)
            self.assertEqual(loop._gar.fresh_sessions, 0)
            self.assertTrue(any("[GAR_RUNTIME_RECOVERY]" in p
                                for p in prompts[1:]))
            self.assertEqual(loop._gar.diagnostic()["level"], "L5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
