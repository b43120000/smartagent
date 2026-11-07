"""Execute the REAL Windows collect.bat against bounded synthetic Log events.

No credentials, local sensitive path exports or GitHub network calls.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TASK = "TASK-G27-TEST"
REQUEST = "RR-G27-TEST"
ACTION = "G27-FAILED-GIT"


@unittest.skipUnless(os.name == "nt", "Windows cmd.exe and PowerShell required")
class CollectorCausalCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="g27-collector-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copyfile(REPO / "collect.bat", self.root / "collect.bat")
        stub = self.root / "source" / "WebAgent" / "protocol_loop.py"
        stub.parent.mkdir(parents=True)
        stub.write_text("# Offline test fixture, no Runtime invocation\n", encoding="utf-8")
        ledger = self.root / "localdata" / "runtime" / "task_progress" / (TASK + ".json")
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            "task_id": TASK, "request_id": REQUEST,
            "runtime_state": "PAUSED",
            "interruption_reason": "bounded_repair:repair_stagnation_soft_deadline",
            "current_step": 2, "total_steps": 5,
            "steps": [{"status": "COMPLETED"},
                      {"status": "IN_PROGRESS"}],
        }), encoding="utf-8")

    def _collect(self, include_failure=True, older_log_noise=False):
        base = {
            "timestamp": 1794040191.0,
            "event": "STATUS", "component": "telegram_webagent_worker",
            "task_id": TASK, "request_id": REQUEST, "task_epoch": "G27-EPOCH",
        }
        rows = [
            {**base, "stage": "planner_context_prepared", "round": 3,
             "context_snapshot_id": "CTX-G27-EXACT",
             "context_digest": "a" * 64,
             "failure_causal_receipt_count": 1,
             "last_failure_category": "DNS_RESOLUTION_FAILED",
             "planner_failure_action_ids": [ACTION],
             "prerequisite_transition_stage": "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED"},
            {**base, "stage": "planner_context_response_correlated", "round": 3,
             "context_snapshot_id": "CTX-G27-EXACT",
             "context_digest": "a" * 64,
             "planner_response_received": True,
             "failure_causal_receipt_count": 1,
             "last_failure_category": "DNS_RESOLUTION_FAILED",
             "planner_failure_action_ids": [ACTION]},
            {**base, "stage": "action_resolution_recorded", "round": 2,
             "action_id": ACTION, "tool": "run_command",
             "original_command_sha256": "b" * 64,
             "dispatched_command_sha256": "b" * 64,
             "disposition": "PASS_UNCHANGED", "reason_codes": [],
             "changed_fields": [], "dispatched": True, "executed": True,
             "evidence_kind": "ACTUAL_TOOL_RESULT"},
            {**base, "stage": "prerequisite_transition_prepared",
             "operation_stage": "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED",
             "progress_transition": "PLANNER_ASSESS_ACTUAL_PROBE_RESULT",
             "access_state": "UNKNOWN", "scope_preflight_status": "NOT_CHECKED",
             "probe_evidence_present": True,
             "latest_probe_failure_category": "DNS_RESOLUTION_FAILED",
             "latest_probe_failure_ref": ACTION,
             "next_failure_evaluation": "CHECK_DNS_WITH_SCOPED_OBSERVATION"},
            {**base, "stage": "action_evidence_recorded", "action_id": ACTION,
             "tool": "run_command", "result_status": "FAILED",
             "result_reason": "COMMAND_EXITED", "executed": True},
            {**base, "stage": "protocol_rejected", "round": 3,
             "diagnostics": [{
                 "reason": "run_command_working_directory_rejected",
                 "tool": "run_command", "action_id": "G27-DNS",
                 "scope_reason_code": "path_independent_command_cwd_outside_bound_workspace",
                 "scope_category": "COMMAND",
                 "scope_path_role": "WORKING_DIRECTORY",
                 "scope_required_field": "working_directory",
                 "scope_reason_code": "path_independent_command_cwd_outside_bound_workspace",
                 "unified_contract": {
                     "schema": "SMARTAGENT_UNIFIED_CONTRACT_TRANSACTION_V1",
                     "issues": [{
                         "contract": "ACTION", "tool": "run_command",
                         "reason_code": "run_command_working_directory_rejected",
                         "action_id": "G27-DNS", "decision": "REJECT_REPLAN",
                         "unexpected_keys": ["working_directory"],
                         "missing_keys": [], "field_issues": ["working_directory"]
                     }]
                 }
             }]},
            {**base, "stage": "unified_contract_transaction_resolved",
             "round": 3, "phase": "REJECTED",
             "contract_names": ["ACTION"],
             "decision_codes": ["REJECT_REPLAN"],
             "reason_codes": ["run_command_working_directory_rejected"],
             "failure_count": 1,
             "last_failure_category": "DNS_RESOLUTION_FAILED"},
            {**base, "stage": "planner_response_received", "round": 3,
             "response": json.dumps({
                 "tool": "report_progress", "action_id": "G27-PROGRESS",
                 "decision": "CONTINUE",
             })},
        ]
        # This event mentions the selected Request but belongs to a DIFFERENT
        # Task. String-match-only collection would silently join its evidence.
        rows.append({
            **base, "stage": "tool_failure_causal_receipt_recorded",
            "task_id": "TASK-OTHER-G27", "action_id": "FOREIGN-RESULT",
            "round": 8, "tool": "run_command",
            "result_status": "FAILED", "result_reason": "COMMAND_EXITED",
            "failure_category": "AUTHENTICATION_REJECTED",
        })
        if include_failure:
            rows.append({
                **base, "stage": "tool_failure_causal_receipt_recorded",
                "round": 2, "action_id": ACTION, "tool": "run_command",
                "operation": "GIT_INSPECT", "result_status": "FAILED",
                "result_reason": "COMMAND_EXITED", "executed": True,
                "exit_code": 128, "timed_out": False,
                "failure_category": "DNS_RESOLUTION_FAILED",
                "classification_verified": True,
                "stderr_present": True, "stderr_sha256": "a" * 64,
                "blocking_evidence_ref": ACTION,
                "next_evaluation": "CHECK_DNS_WITH_SCOPED_OBSERVATION",
                "raw_result": "SECRET_TOOL_OUTPUT_MUST_NOT_EXPORT",
            })
        logs = self.root / "localdata" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "remote_runtime.jsonl").open("w", encoding="utf-8") as output:
            for item in rows:
                output.write(json.dumps(item) + "\n")
            if older_log_noise:
                # The previous 25,000-line tail silently lost these early
                # Task events; full-file streaming must retain them.
                noise = json.dumps({
                    "event": "STATUS", "stage": "OTHER_TASK_HEARTBEAT",
                    "task_id": "TASK-UNRELATED",
                    "request_id": "RR-UNRELATED",
                }) + "\n"
                for _ in range(25010):
                    output.write(noise)
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(self.root / "collect.bat"), TASK, REQUEST],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        zips = list((self.root / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(zips[0]) as z:
            names = set(z.namelist())
            contents = {name: z.read(name).decode("utf-8") for name in names}
        return contents

    def test_full_causal_chain_with_schema_and_secret_scrub(self):
        files = self._collect()
        self.assertIn("prerequisite_transition_summary.json", files)
        self.assertIn("failure_causal_summary.json", files)
        self.assertIn("rca_causal_coverage.json", files)
        transition = json.loads(files["prerequisite_transition_summary.json"])[0]
        self.assertEqual(transition["transition_stage"],
                         "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED")
        self.assertEqual(transition["latest_probe_failure_category"],
                         "DNS_RESOLUTION_FAILED")
        failure = json.loads(files["failure_causal_summary.json"])[0]
        self.assertEqual(failure["exit_code"], 128)
        self.assertEqual(failure["next_evaluation"],
                         "CHECK_DNS_WITH_SCOPED_OBSERVATION")
        self.assertTrue(failure["classification_verified"])
        self.assertNotIn("FOREIGN-RESULT", "\\n".join(files.values()))
        admission = json.loads(files["action_admission_rejections.json"])[0]
        self.assertEqual(admission["unified_contract_issue_diffs"][0]["tool"],
                         "run_command")
        scope = json.loads(files["scope_preflight_summary.json"])
        self.assertTrue(any(item.get("admission_only") for item in scope))
        admission_scope = next(item for item in scope if item.get("admission_only"))
        self.assertEqual(admission_scope["scope_reason_code"],
                         "path_independent_command_cwd_outside_bound_workspace")
        self.assertEqual(admission_scope["action_id"], "G27-DNS")
        delivery = json.loads(files["planner_context_delivery.json"])
        self.assertEqual(delivery[0]["failure_causal_receipt_count"], 1)
        self.assertEqual(delivery[0]["planner_failure_action_ids"], [ACTION])
        self.assertEqual(delivery[0]["prerequisite_transition_stage"],
                         "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED")
        action_trace = json.loads(files["action_causal_trace_summary.json"])
        self.assertTrue(any(row["event"] == "action_resolution_recorded"
                            and row["original_command_sha256"] == "b" * 64
                            for row in action_trace))
        ledger = json.loads(files["task_progress_summary.json"])
        self.assertEqual(ledger["interruption_reason"],
                         "bounded_repair:repair_stagnation_soft_deadline")
        diff = json.loads(files["planner_contract_diff.json"])[0]
        self.assertEqual(diff["schema_status"], "NOT_SUBMITTED")
        self.assertIsNone(diff["schema_valid"])
        coverage = json.loads(files["rca_causal_coverage.json"])
        self.assertEqual(coverage["status"], "FULL_AVAILABLE_LOG_SCAN", coverage)
        self.assertFalse(coverage["log_window_bounded"])
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["causal_coverage_status"], coverage["status"])
        self.assertNotIn("SECRET_TOOL_OUTPUT_MUST_NOT_EXPORT",
                         "\n".join(files.values()))

    def test_full_scan_retains_early_failure_beyond_previous_25000_line_window(self):
        files = self._collect(older_log_noise=True)
        transition = json.loads(files["prerequisite_transition_summary.json"])
        self.assertEqual(transition[0]["transition_stage"],
                         "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED")
        coverage = json.loads(files["rca_causal_coverage.json"])
        self.assertEqual(coverage["status"], "FULL_AVAILABLE_LOG_SCAN", coverage)
        manifest = json.loads(files["manifest.json"])
        source = next(row for row in manifest["log_sources"]
                      if row["source"] == "remote_runtime")
        self.assertEqual(source["scan_mode"], "FULL_FILE_STREAM")
        self.assertTrue(source["scan_guarantees_unlimited_history"])

    def test_missing_failure_receipt_is_reported_partial_not_hidden(self):
        files = self._collect(include_failure=False)
        coverage = json.loads(files["rca_causal_coverage.json"])
        self.assertEqual(coverage["status"], "PARTIAL")
        self.assertIn("ACTION_FAILURE_CAUSAL_RECEIPT_MISSING",
                      coverage["issues"])
        self.assertIn(ACTION, coverage["missing_failure_action_ids"])
        self.assertEqual(
            json.loads(files["manifest.json"])["causal_coverage_status"],
            "PARTIAL")


if __name__ == "__main__":
    unittest.main()
