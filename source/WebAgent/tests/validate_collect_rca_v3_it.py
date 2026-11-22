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
                payload = {"tool": "report_progress", "action_id": aid,
                           "decision": "CONTINUE"}
                if scr is not None:
                    payload["capability_resolution"] = scr
                return "\n".join([
                    fence + "smartagent_tool",
                    json.dumps(payload, separators=(",", ":")),
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
                {**base, "event": "planner_response_received", "round": 3,
                 "timestamp": 106.0, "response": progress_response(None, "P-3")},
                {**base, "event": "tool_started", "round": 3, "timestamp": 107.0,
                 "action_id": "A-SCOPE-UNAVAILABLE", "tool": "inspect_project_scope",
                 "scope_allowed": False,
                 "scope_reason_code": "workspace_not_found_or_not_directory",
                 "scope_category": "PROJECT_WORKSPACE_UNAVAILABLE_OR_NOT_DIRECTORY",
                 "scope_path_role": "PROJECT_WORKSPACE",
                 "scope_transition": "EVIDENCE_ACQUISITION",
                 "scope_required_field": ""},
                {**base, "event": "protocol_rejected", "round": 4, "timestamp": 108.0,
                 "diagnostics": [{
                     "reason": "invalid_progress_payload",
                     "field_issues": [{
                         "field": "context_request.detail",
                         "code": "detail_required",
                     }],
                     "context_request_repair": {
                         "schema": "SMARTAGENT_CONTEXT_REQUEST_FIELD_REPAIR_V1",
                         "field_issues": [{
                             "field": "context_request.detail",
                             "code": "detail_required",
                         }],
                         "objective_blocker_ids": ["A-SCOPE-UNAVAILABLE"],
                     },
                 }]},
                {**base, "event": "command_repair_readiness_evaluated", "round": 5,
                 "timestamp": 109.0, "action_id": "D9", "repair_mode": "FIELD_REPAIR",
                 "issue_count": 3, "previous_issue_count": None,
                 "resolved_issue_count": 0, "introduced_issue_count": 0,
                 "converging": False, "issue_fields": [
                     "operation", "verify", "success_criteria",
                 ], "issue_codes": [
                     "operation_not_in_registry",
                     "required_if_non_observation",
                 ], "authorization_granted": False, "executed": False},
                {**base, "event": "protocol_rejected", "round": 5,
                 "timestamp": 109.5, "diagnostics": [{
                     "tool": "run_command", "reason": "inline_script_too_complex",
                     "detail": "field=verify[0].command; hidden=" + secret,
                     "command_repair": {
                         "schema": "SMARTAGENT_COMMAND_REPAIR_READINESS_V1",
                         "action_id": "D9", "mode": "ACTION_RECONSTRUCTION",
                         "issue_count": 1, "field_issues": [
                             {"field": "command", "code": "inline_script_too_complex"},
                         ], "authorization_granted": False,
                     },
                 }]},
                {**base, "event": "command_repair_converged", "round": 6,
                 "timestamp": 110.0, "action_id": "D9",
                 "previous_issue_count": 3, "current_issue_count": 0,
                 "resolved_issue_count": 3, "introduced_issue_count": 0,
                 "full_admission_passed": True, "executed": False},
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
                self.assertEqual(len(diff), 3)
                self.assertFalse(diff[0]["schema_valid"])
                self.assertTrue(diff[1]["schema_valid"])
                self.assertTrue(diff[1]["proposal_changed_from_previous"])
                self.assertEqual(diff[1]["known_capabilities"],
                                 ["directory.inspect_metadata"])
                self.assertFalse(diff[2]["scr_present"])
                self.assertEqual(diff[2]["known_capabilities"], [])
                delivery = load("planner_context_delivery.json")
                self.assertTrue(any(row.get("context_digest") == "a" * 64 for row in delivery))
                preflight = load("replan_preflight_summary.json")
                self.assertTrue(any(row["status"] == "NOT_ELIGIBLE" for row in preflight))
                scope = load("scope_preflight_summary.json")
                self.assertEqual(scope[0]["scope_reason_code"],
                                 "workspace_not_found_or_not_directory")
                self.assertFalse(scope[0]["filesystem_absence_verified"])
                context = load("context_request_repair_summary.json")
                self.assertEqual(context[0]["field_repairs"][0]["issues"][0]["code"],
                                 "detail_required")
                command_events = load("command_repair_summary.json")
                self.assertEqual(len(command_events), 3)
                rejections = [row for row in command_events
                              if row["event"] == "protocol_rejected"]
                self.assertEqual(rejections[0]["command_reject_field"], "verify")
                self.assertEqual(rejections[0]["command_repair_diagnostics"][0]["mode"],
                                 "ACTION_RECONSTRUCTION")
                successes = [row for row in command_events
                             if row["event"] == "command_repair_converged"]
                self.assertEqual(successes[0]["resolved_issue_count"], 3)
                self.assertTrue(successes[0]["full_admission_passed"])
                for filename in archive.namelist():
                    data = archive.read(filename).decode("utf-8")
                    self.assertNotIn(secret, data)
                    self.assertNotIn("WRONG_SCHEMA", data)
                    self.assertNotIn("extra_unknown", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
