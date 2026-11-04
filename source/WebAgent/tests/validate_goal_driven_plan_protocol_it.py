#!/usr/bin/env python3
"""Full V9 Planner -> invalid Task Plan -> observation -> fresh valid Plan IT."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_task_plan_schema_recovery import _progress, _response
from agent_core.project_sync import inspect_project_scope
from agent_core.task_plan import TASK_PLAN_SCHEMA


class EvidenceGuidedV9IT(unittest.TestCase):
    def test_live_protocol_uses_observation_before_new_proposal(self):
        with tempfile.TemporaryDirectory(prefix="gdpr-v9-") as temporary:
            project = Path(temporary).resolve()
            snapshot = inspect_project_scope(project)
            base_plan = {
                "schema": TASK_PLAN_SCHEMA,
                "base_snapshot_id": snapshot["snapshot_id"],
                "semantic_map_revision": "",
                "goal": "Create a bounded status artifact",
                "affected_flows": [], "files_to_read": [],
                "edit_plan": {},
                "verification_commands": ["python -c \"print('ok')\""],
                "acceptance_criteria": ["status.txt exists"],
                "rollback_condition": "Verification failed",
                "post_change_semantic": [],
            }
            complete_plan = {
                **base_plan,
                "edit_plan": {
                    "expected_observable_result": "status.txt exists",
                    "files_to_modify": [
                        {"path": "status.txt", "create": True,
                         "mode": "whole_file", "content": "ready\n",
                         "modification_intent": "Create the requested status artifact"},
                    ],
                },
            }
            seen_prompts, events = [], []
            def planner(prompt, _expected, _attachments):
                seen_prompts.append(prompt)
                index = len(seen_prompts)
                if index == 1:
                    return _response(
                        _progress("GDPR-P1", "propose_task_plan"),
                        {"tool": "propose_task_plan", "action_id": "GDPR-INVALID",
                         "workspace": str(project), "plan": base_plan},
                    )
                if index == 2:
                    assert "task_plan_prerequisite_discovery_required" in prompt, prompt[-6000:]
                    return _response(
                        _progress("GDPR-P2", "inspect_project_scope"),
                        {"tool": "inspect_project_scope", "action_id": "GDPR-OBSERVE",
                         "workspace": str(project)},
                    )
                if index == 3:
                    assert "task_plan_evidence_ready" in prompt, prompt[-6000:]
                    return _response(
                        _progress("GDPR-P3", "propose_task_plan"),
                        {"tool": "propose_task_plan", "action_id": "GDPR-CORRECTED",
                         "workspace": str(project), "plan": complete_plan},
                    )
                raise RuntimeError("GDPR_STOP_AFTER_PLAN_FREEZE")

            loop = WebAgentProtocolLoop(
                project, planner, progress_root=project, max_turns=6,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            with self.assertRaisesRegex(RuntimeError, "GDPR_STOP_AFTER_PLAN_FREEZE"):
                loop.run(
                    "In " + str(project) +
                    " prepare a plan for creating status.txt; "
                    "inspect prerequisites before any source file mutation.",
                    request_id="RR-GDPR-FULL-IT",
                    task_id="TASK-GDPR-FULL-IT",
                    task_epoch="EPOCH-GDPR-FULL-IT",
                    trusted_authorized_paths=[str(project)],
                )
            self.assertGreaterEqual(len(seen_prompts), 4)
            self.assertEqual(
                loop.action_result_ledger["GDPR-OBSERVE"]["execution_status"],
                "SUCCEEDED",
            )
            result = json.loads(loop.action_ledger["GDPR-CORRECTED"]["result"])
            self.assertEqual(result["status"], "PLAN_FROZEN", result)
            self.assertFalse((project / "status.txt").exists(),
                             "Freezing plan executed a user-file modification")
            self.assertTrue(any(name == "task_plan_repair_latch_updated"
                                for name, _ in events), events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
