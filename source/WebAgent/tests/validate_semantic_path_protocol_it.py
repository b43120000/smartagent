#!/usr/bin/env python3
"""SPR V1 V9 integration: original path -> preflight ledger -> native read Tool."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def progress():
    return {
        "tool": "report_progress", "action_id": "SPR-PROGRESS",
        "base_evaluation": "Bound the requested path and inspect its metadata.",
        "total_steps": 2, "current_step": 1,
        "steps": [
            {"step": 1, "desc": "Collect directory metadata", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "Report the observation", "status": "PENDING"},
        ],
        "current_focus": "Read-only directory statistics",
        "next_action": "inspect_directory",
        "completion_contract": {
            "success": ["Directory statistics observed"],
            "failure": ["Directory statistics unavailable"],
            "in_progress": ["Directory observation pending"],
            "interrupted": ["Explicit directory cannot be authorized"],
        },
        "decision": "CONTINUE", "outcome": "PENDING",
        "matched_condition": "Directory observation pending",
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "Native inspection evidence is not collected yet.",
        "capability_resolution": {
            "schema": "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1",
            "capabilities": ["directory.inspect_metadata"],
        },
    }


class SPRProtocolIT(unittest.TestCase):
    def test_attached_chinese_and_spaces_reach_real_inspection(self):
        with tempfile.TemporaryDirectory(prefix="spr-v9-") as dirname:
            root = Path(dirname).resolve()
            folder = root / "My Project" / "Camera Test Data"
            folder.mkdir(parents=True)
            (folder / "one.txt").write_text("hello", encoding="utf-8")
            request = (
                "請分析 " + str(folder) + "的資料夾狀況。"
                "我只需要數量與結構，不需要逐一列出檔名。"
                "不允許執行任何寫入、安裝、刪除或修改操作。"
            )
            prompts, events = [], []
            def planner(prompt, _expected, _attachments):
                prompts.append(prompt)
                if len(prompts) == 1:
                    self.assertIn("SMARTAGENT_SEMANTIC_PATH_RESOLUTION_V1", prompt)
                    self.assertIn(str(folder), prompt)
                    return response(
                        progress(),
                        {"tool": "inspect_directory", "action_id": "SPR-INSPECT",
                         "paths": [str(folder)], "recursive": False},
                    )
                raise RuntimeError("SPR_FINISH_AFTER_FIRST_ROUND")
            loop = WebAgentProtocolLoop(
                root, planner, progress_root=root, max_turns=3,
                event_sink=lambda name, **data: events.append((name, data)),
            )
            with patch.dict(os.environ, {"SMARTAGENT_GAR_V1": "0",
                                        "SMARTAGENT_PREREQ_V3_BCD": "0"}):
                with self.assertRaisesRegex(RuntimeError, "SPR_FINISH_AFTER_FIRST_ROUND"):
                    loop.run(
                        request, request_id="RR-SPR-V9-IT",
                        task_id="TASK-SPR-V9-IT", task_epoch="EPOCH-SPR-V9-IT",
                    )
            self.assertEqual(loop.authorized_paths, [str(folder)])
            self.assertIn("SPR-INSPECT", loop.action_result_ledger)
            result = loop.action_result_ledger["SPR-INSPECT"]
            self.assertTrue(result.get("executed"), result)
            self.assertEqual(result.get("execution_status"), "SUCCEEDED", result)
            self.assertTrue(any(name == "semantic_path_resolution_recorded"
                                and data.get("status") == "VERIFIED_UNIQUE"
                                for name, data in events), events)
            index = root / "localdata" / "runtime" / "path_resolution" / "RR-SPR-V9-IT.json"
            doc = json.loads(index.read_text(encoding="utf-8"))
            self.assertEqual(doc["request_id"], "RR-SPR-V9-IT")
            self.assertEqual(doc["task_epoch"], "EPOCH-SPR-V9-IT")
            self.assertEqual(doc["paths"], [str(folder)])

    def test_ambiguous_preflight_never_contacts_planner_or_tools(self):
        with tempfile.TemporaryDirectory(prefix="spr-amb-") as dirname:
            root = Path(dirname).resolve()
            folder = root / "deps"
            folder.mkdir()
            (root / "deps 的資料夾狀況").mkdir()
            calls = []
            loop = WebAgentProtocolLoop(
                root, lambda *args: calls.append(args) or "",
                progress_root=root, max_turns=1,
            )
            from agent_core.semantic_path_resolution import SemanticPathError
            with self.assertRaisesRegex(SemanticPathError, "semantic_path_resolution_ambiguous"):
                loop.run(
                    "請分析 " + str(folder) + " 的資料夾狀況。",
                    request_id="RR-SPR-AMB", task_id="TASK-SPR-AMB",
                    task_epoch="EPOCH-SPR-AMB",
                )
            self.assertFalse(calls)
            self.assertFalse(loop.action_ledger)
            doc = json.loads((root / "localdata" / "runtime" / "path_resolution" /
                              "RR-SPR-AMB.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
