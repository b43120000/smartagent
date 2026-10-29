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
