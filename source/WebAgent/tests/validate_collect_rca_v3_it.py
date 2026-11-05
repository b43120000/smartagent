#!/usr/bin/env python3
"""Windows-only RCA V3 collector smoke test, isolated from real workspaces."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


class CollectRCAV3IT(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe / PowerShell required")
    def test_collect_summarizes_scr_changes_without_raw_model_text(self):
        repo = Path(__file__).resolve().parents[3]
        collector = repo / "collect.bat"
        self.assertTrue(collector.is_file())
        task, request = "TASK-RCA-V3-SMOKE", "RR-RCA-V3-SMOKE"
        with tempfile.TemporaryDirectory(prefix="smartagent-rca-v3-") as root_name:
            root = Path(root_name)
            shutil.copyfile(collector, root / "collect.bat")
            (root / "source" / "WebAgent").mkdir(parents=True)
            (root / "source" / "WebAgent" / "protocol_loop.py").write_text(
                "# Collector fixture; not a runtime.\n", encoding="utf-8")
            progress = root / "localdata" / "runtime" / "task_progress"
            progress.mkdir(parents=True)
            (progress / f"{task}.json").write_text(json.dumps({
                "task_id": task, "request_id": request,
                "runtime_state": "PAUSED", "current_step": 0, "total_steps": 0,
                "steps": [],
            }), encoding="utf-8")
            logs = root / "localdata" / "logs"
            logs.mkdir(parents=True)
            secret = "NEVER_EXPORT_SECRET_XYZ"
            base = {"task_id": task, "request_id": request}
            def progress_response(scr, aid):
                fence = chr(96) * 3
                return "\n".join([
                    fence + "smartagent_tool",
                    json.dumps({"tool": "report_progress", "action_id": aid,
                                "capability_resolution": scr,
                                "decision": "CONTINUE"}, separators=(",", ":")),
                    json.dumps({"tool": "turn_commit", "action_count": 1}),
                    fence,
                ])
            scr1 = {
                "schema": "WRONG_SCHEMA",
                "capabilities": ["directory.inspect_metadata"],
                "extra_unknown": secret,
            }
            scr2 = {
                "schema": "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1",
                "capabilities": ["directory.inspect_metadata"],
            }
            events = [
                {**base, "event": "planner_response_received", "round": 1,
                 "timestamp": 100.0, "response": progress_response(scr1, "P-1")},
                {**base, "event": "protocol_rejected", "round": 1, "timestamp": 101.0,
                 "diagnostics": [{
                     "reason": "semantic_capability_schema_invalid",
                     "marker": "[WEBAGENT_SEMANTIC_CAPABILITY_REJECTED]",
                     "capability_repair": {
                         "proposal_shape": {
                             "object_type_valid": True, "key_set_exact": False,
                             "schema_matches": False, "capabilities_is_list": True,
                             "unknown_or_invalid_count": 0,
                             "duplicate_count": 0, "extra_key_count": 1,
                         },
                         "previously_selected": [],
                         "required_for_submitted_actions": ["directory.inspect_metadata"],
                     },
                 }]},
                {**base, "event": "planner_context_prepared", "round": 2,
                 "timestamp": 102.0, "context_snapshot_id": "CTX-SAFE2",
                 "context_digest": "a" * 64, "evidence_total": 0,
                 "diagnostic_count": 1, "critical_context_omitted": False},
                {**base, "event": "planner_response_received", "round": 2,
                 "timestamp": 103.0, "response": progress_response(scr2, "P-2")},
                {**base, "event": "planner_context_response_correlated", "round": 2,
                 "timestamp": 104.0, "context_snapshot_id": "CTX-SAFE2",
                 "context_digest": "a" * 64, "planner_response_received": True},
                {**base, "event": "preexecution_strategy_replan_evaluated", "round": 2,
                 "timestamp": 105.0, "status": "NOT_ELIGIBLE",
                 "reason_code": "RUNTIME_ATTESTATION_NOT_AVAILABLE",
                 "authorization_granted": False},
            ]
            (logs / "webagent_runtime.jsonl").write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8")
            command = ["cmd.exe", "/d", "/c", str(root / "collect.bat"), task, request]
            completed = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=90)
            self.assertEqual(completed.returncode, 0,
                             completed.stdout[-3000:] + completed.stderr[-3000:])
            zips = list((root / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
            self.assertEqual(len(zips), 1)
            with zipfile.ZipFile(zips[0]) as archive:
                def load(name):
                    return json.loads(archive.read(name))
                manifest = load("manifest.json")
                self.assertEqual(manifest["schema"], "SMARTAGENT_LIVE_RCA_V3")
                diff = load("planner_contract_diff.json")
                self.assertEqual(len(diff), 2)
                self.assertFalse(diff[0]["schema_valid"])
                self.assertTrue(diff[1]["schema_valid"])
                self.assertTrue(diff[1]["proposal_changed_from_previous"])
                delivery = load("planner_context_delivery.json")
                self.assertTrue(any(row.get("context_digest") == "a" * 64 for row in delivery))
                preflight = load("replan_preflight_summary.json")
                self.assertTrue(any(row["status"] == "NOT_ELIGIBLE" for row in preflight))
                for filename in archive.namelist():
                    data = archive.read(filename).decode("utf-8")
                    self.assertNotIn(secret, data)
                    self.assertNotIn("WRONG_SCHEMA", data)
                    self.assertNotIn("extra_unknown", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
