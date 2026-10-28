"""PR10/2A isolated OPEN Writer -> Protocol terminal guard bridge.

A real task-scoped chunked writer stages bytes in TemporaryDirectory; a
minimal typed-result bridge is inserted into a real WebAgentProtocolLoop action
ledger to exercise its actual OPEN_WRITE_TRANSACTION and terminal-admission
guards. The bridge is NOT evidence of a production Worker dispatch pipeline.

Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_tri_one_2a_open_terminal.py
No real Worker, network, browser, user Workspace, installed Task/Writer/Outbox.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.task_progress import initialize_progress
from agent_core.tool_result_contract import decode_result, encode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from validate_it19_task_scoped_runtime_writer import begin, agent_for


class OpenWriterTerminalGuard(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tri-one-2a-open-terminal-")
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve() / "isolated-workspace"
        self.workspace.mkdir()
        self.destination = self.workspace / "never-committed.txt"
        self.agent = agent_for(self.workspace, "A")
        self.write_id, self.staged_bytes = begin(
            self.agent, self.destination, "TRI-ONE-A"
        )
        self.manager = self.agent._chunked_write_manager
        self.assertEqual(self.manager._load_manifest(self.write_id)["state"], "OPEN")
        self.assertFalse(self.destination.exists())

        self.loop = WebAgentProtocolLoop(
            self.workspace, lambda *_: "", progress_root=self.workspace,
        )
        self.loop.run_id = "RR-A"
        self.loop.task_id = "TASK-A"
        self.loop.task_epoch = "EPOCH-1"
        self.loop.turn_id = 2
        self.loop.task_request = "Only commit this isolated staged file before reporting SUCCESS"
        self.loop.protocol_state = "ACTIVE"
        self.loop.progress_ledger = initialize_progress(
            "TASK-A", request_id="RR-A",
            goal=self.loop.task_request, root=self.workspace,
        )
        self.loop.authorized_paths = [str(self.workspace)]
        self.loop.tools._authorized_local_paths = [str(self.workspace)]

        # The typed result is a controlled bridge from the genuine OPEN
        # manifest to the protocol evidence ledger. This is not a fake COMMIT.
        result = encode_result(
            "SUCCEEDED", "STAGING_STILL_OPEN", tool="write_file_chunk",
            executed=True, data={"transaction_state": "OPEN",
                                 "write_id": self.write_id},
        )
        self.assertEqual(decode_result(result)["data"]["transaction_state"], "OPEN")
        action = {
            "tool": "write_file_chunk", "action_id": "A-OPEN-CHUNK",
            "write_id": self.write_id,
        }
        self.loop.action_ledger[action["action_id"]] = {
            "action": action, "result": result, "execution_status": "SUCCEEDED",
            "verification_status": "UNVERIFIED",
        }
        self.loop.action_result_ledger[action["action_id"]] = {
            "tool": "write_file_chunk", "execution_status": "SUCCEEDED",
            "verification_status": "UNVERIFIED", "terminal_eligible": False,
            "request_id": "RR-A", "task_epoch": "EPOCH-1",
        }
        self.state = self.loop._refresh_action_loop_state()

    def test_open_manifest_is_runtime_blocker(self):
        blockers = self.state.get("blockers", [])
        matching = [
            x for x in blockers
            if x.get("reason") == "OPEN_WRITE_TRANSACTION"
            and x.get("blocking_action_id") == "A-OPEN-CHUNK"
            and x.get("satisfied") is False
        ]
        self.assertEqual(len(matching), 1)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.manager._load_manifest(self.write_id)["state"], "OPEN")

    def test_protocol_terminal_success_is_rejected_while_open(self):
        success = "isolated destination committed and verified"
        progress = {
            "tool": "report_progress", "action_id": "P-FALSE-COMPLETE",
            "base_evaluation": "isolated write remains OPEN",
            "total_steps": 1, "current_step": 1,
            "steps": [{"step": 1, "desc": "commit and verify",
                       "status": "COMPLETED"}],
            "current_focus": "report completion",
            "next_action": "final_response",
            "completion_contract": {
                "success": [success], "failure": ["write rejected"],
                "in_progress": ["write still OPEN"],
                "interrupted": ["writer state unknown"],
            },
            "decision": "COMPLETE", "outcome": "SUCCESS",
            "matched_condition": success,
            "evidence_refs": ["A-OPEN-CHUNK"],
            "decision_reason": "untrusted premature completion",
        }
        action = {"tool": "final_response",
                  "action_id": "F-FALSE-COMPLETE",
                  "content": "Untrue success"}
        accepted, errors = self.loop._accept_ack(
            [progress, action, {"tool": "turn_commit", "action_count": 2}],
            {"ack_web_ack_id": ""},
        )
        self.assertFalse(accepted)
        self.assertTrue(errors)
        self.assertIn(
            "active_action_not_complete",
            {str(x.get("reason") or "") for x in errors},
            errors,
        )
        self.assertFalse(self.loop.terminal_acceptance)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.manager._load_manifest(self.write_id)["state"], "OPEN")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OpenWriterTerminalGuard)
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
