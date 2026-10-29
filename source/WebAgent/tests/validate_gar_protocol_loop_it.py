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


class GARProtocolIT(unittest.TestCase):
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
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "1"}):
                with self.assertRaises(RuntimeError):
                    loop.run(
                        "Explain RAII.", request_id="RR-L4-IT",
                        task_id="TASK-L4-IT", task_epoch="EPOCH-L4-IT",
                        source_tag="REMOTEAGENT_WEBCOPILOT_V9",
                    )
            self.assertEqual(len(prepares), 1)
            self.assertGreaterEqual(len(original_calls), 3)
            self.assertEqual(len(fresh_calls), 1)
            self.assertEqual(releases, ["released"])
            self.assertEqual(loop._gar.fresh_sessions, 1)
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
