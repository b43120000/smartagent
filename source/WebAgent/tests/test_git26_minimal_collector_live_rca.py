"""Windows end-to-end v10 Collector causal coverage: no raw paths or commands."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[3]
TASK = "TASK-G26-MINIMAL-TEST"
REQUEST = "RR-G26-MINIMAL-TEST"


@unittest.skipUnless(os.name == "nt", "Requires Windows CMD and PowerShell 5.1")
class V10CollectorTests(unittest.TestCase):
    def test_all_v10_rejections_and_context_are_causally_reported(self):
        with tempfile.TemporaryDirectory(prefix="g26-minimal-rca-") as temp:
            root = Path(temp)
            shutil.copyfile(REPO / "collect.bat", root / "collect.bat")
            stub = root / "source" / "WebAgent" / "protocol_loop.py"
            stub.parent.mkdir(parents=True)
            stub.write_text("# safe fixture\\n", encoding="utf-8")
            ledger = root / "localdata" / "runtime" / "task_progress" / (TASK + ".json")
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({
                "task_id": TASK, "request_id": REQUEST,
                "runtime_state": "PAUSED",
                "interruption_reason": "GOAL_ACTION_REPAIR_EXHAUSTED",
            }), encoding="utf-8")
            base = {
                "event": "STATUS", "task_id": TASK, "request_id": REQUEST,
                "task_epoch": "EPOCH-G26", "timestamp": 1794144507.0,
            }
            rows = [{**base, "round": 1, "stage": "minimal_goal_driver_context",
                     "snapshot_source": "CANONICAL_SHARED_BLACKBOARD",
                     "context_sha256": "a" * 64,
                     "next_category": "RESOLVE_REGISTERED_SOURCE_ARTIFACT",
                     "source_requirement": "REQUIRED"}]
            for i, (operation, reason, tool) in enumerate([
                ("READ_ONLY", "run_command_operation_invalid", "run_command"),
                ("READ", "run_command_operation_invalid", "run_command"),
                ("", "scope_rejected:resolved_scope_expands_beyond_explicit_path",
                 "inspect_project_scope"),
            ], start=2):
                rows.append({**base, "round": i, "stage": "minimal_action_proposed",
                             "tool": tool, "operation": operation or "INVALID_OR_MISSING",
                             "operation_valid": False, "proposal_sha256": str(i) * 64,
                             "proposed_fields": ["operation", "command"],
                             "private_path": "SHOULD_NOT_BE_EXPORTED"})
                rows.append({**base, "round": i, "stage": "minimal_goal_action_rejected",
                             "tool": tool, "proposed_operation": operation,
                             "reason_code": reason, "repair_mode": (
                                 "SCHEMA_REPAIR" if i < 4 else "EVIDENCE_ACQUISITION"),
                             "proposal_sha256": str(i) * 64,
                             "goal_context_sha256": "a" * 64,
                             "executed": False})
            log = root / "localdata" / "logs" / "remote_runtime.jsonl"
            log.parent.mkdir(parents=True)
            log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            process = subprocess.run(
                ["cmd.exe", "/d", "/c", str(root / "collect.bat"), TASK, REQUEST],
                cwd=root, capture_output=True, text=True, timeout=120)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            zips = list((root / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
            self.assertEqual(len(zips), 1)
            with zipfile.ZipFile(zips[0]) as z:
                coverage = json.loads(z.read("rca_causal_coverage.json"))
                self.assertEqual(coverage["admission_rejection_count"], 3)
                self.assertEqual(coverage["minimal_admission_rejection_count"], 3)
                self.assertEqual(coverage["minimal_action_proposed_count"], 3)
                self.assertEqual(coverage["minimal_admission_rejection_represented_count"], 3)
                self.assertEqual(coverage["status"], "FULL_AVAILABLE_LOG_SCAN")
                lineage = json.loads(z.read("minimal_core_action_lineage.json"))
                self.assertEqual(sum(x["event"] == "minimal_action_proposed"
                                     for x in lineage), 3)
                self.assertTrue(any(x.get("operation") == "READ_ONLY" for x in lineage))
                rejects = json.loads(z.read("action_admission_rejections.json"))
                self.assertEqual(len(rejects), 3)
                failures = json.loads(z.read("failure_causal_summary.json"))
                self.assertEqual(len(failures), 3)
                scope = json.loads(z.read("scope_preflight_summary.json"))
                self.assertTrue(any("resolved_scope_expands" in
                                    str(x.get("scope_reason_code")) for x in scope))
                delivery = json.loads(z.read("planner_context_delivery.json"))
                self.assertTrue(any(x.get("next_category") ==
                                    "RESOLVE_REGISTERED_SOURCE_ARTIFACT" for x in delivery))
                self.assertNotIn("SHOULD_NOT_BE_EXPORTED",
                                 "\n".join(z.read(name).decode("utf-8")
                                             for name in z.namelist()))


if __name__ == "__main__":
    unittest.main()
