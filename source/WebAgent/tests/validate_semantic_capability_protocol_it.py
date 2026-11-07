"""SCR V1 V9 round-trip using a fake Planner and real read-only Tools."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from agent_core.semantic_capability_resolution import SCHEMA


def make_progress(caps):
    return {
        "tool": "report_progress", "action_id": "SCR-P1",
        "base_evaluation": "Read the requested directory using Runtime tools.",
        "total_steps": 2, "current_step": 1,
        "steps": [
            {"step": 1, "desc": "Observe directory", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "Assess results", "status": "PENDING"},
        ],
        "current_focus": "Read contents and collect metadata",
        "next_action": "list_directory and inspect_directory",
        "completion_contract": {
            "success": ["Both directory observations succeeded"],
            "failure": ["Directory observation failed"],
            "in_progress": ["Directory inspection pending"],
            "interrupted": ["Scope is blocked"],
        },
        "decision": "CONTINUE", "outcome": "PENDING",
        "matched_condition": "Directory inspection pending",
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "No evidence has been collected yet",
        "capability_resolution": {"schema": SCHEMA, "capabilities": caps},
    }


class SCREndToEnd(unittest.TestCase):
    def case(self, caps, expect_inspect):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            folder = root / "folder"
            folder.mkdir()
            (folder / "file.txt").write_text("ok", encoding="utf-8")
            turns, events = [], []

            def planner(prompt, expected, attachments):
                turns.append(prompt)
                if len(turns) > 1:
                    raise RuntimeError("SCR_END_AFTER_FIRST_ROUND")
                assert SCHEMA in prompt
                return response(
                    make_progress(caps),
                    {"tool": "list_directory", "action_id": "SCR-LIST", "path": str(folder)},
                    {"tool": "inspect_directory", "action_id": "SCR-META",
                     "paths": [str(folder)], "recursive": False},
                )
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=3,
                event_sink=lambda name, **data: events.append((name, data)))
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "0", "SMARTAGENT_PREREQ_V3_BCD": "0",
            }):
                with self.assertRaisesRegex(RuntimeError, "SCR_END_AFTER_FIRST_ROUND"):
                    loop.run(
                        "看看這個路徑有什麼，順便做資料夾統計：" + str(folder),
                        request_id="RR-SCR-IT", task_id="TASK-SCR-IT",
                        task_epoch="EPOCH-SCR-IT",
                        trusted_authorized_paths=[str(folder)],
                    )
            if expect_inspect:
                self.assertIn("SCR-LIST", loop.action_result_ledger,
                              "SCR round events: " + repr(events[-25:]))
                self.assertIn("SCR-META", loop.action_result_ledger)
                self.assertEqual(
                    loop.tools._runtime_semantic_capability_resolution["request_id"],
                    "RR-SCR-IT",
                )
            else:
                # The canonical multi-Action transaction is atomic: a missing
                # explicitly selected capability rejects the whole envelope.
                # It must not execute the first sibling and silently reject
                # only the second; no admitted capability binding is persisted.
                self.assertNotIn("SCR-LIST", loop.action_result_ledger)
                self.assertNotIn("SCR-META", loop.action_result_ledger)
                self.assertIsNone(loop.tools._runtime_semantic_capability_resolution)
                self.assertTrue(any(
                    "semantic_capability_not_selected" in str(data)
                    for _, data in events
                ), events)

    def test_positive(self):
        self.case(["directory.list", "directory.inspect_metadata"], True)

    def test_negative(self):
        self.case(["directory.list"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
