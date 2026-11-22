"""Git37 Windows collector: task-bound Executor Admission failures must not vanish."""
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
TASK = "TASK-G37-EXACT-CASE-D"
REQUEST = "RR-G37-EXACT-CASE-D"
EPOCH = "EPOCH-G37-CASE-D"


@unittest.skipUnless(os.name == "nt", "Requires Windows CMD and PowerShell 5.1")
class ExecutorAdmissionCollectorTests(unittest.TestCase):
    def _collect(self, *, expected_count=5, request_override=None):
        with tempfile.TemporaryDirectory(prefix="g37-executor-rca-") as tmp:
            root = Path(tmp)
            shutil.copyfile(REPO / "collect.bat", root / "collect.bat")
            source = root / "source" / "WebAgent" / "protocol_loop.py"
            source.parent.mkdir(parents=True)
            source.write_text("# safe test fixture\n", encoding="utf-8")
            task_progress = root / "localdata" / "runtime" / "task_progress" / (TASK + ".json")
            task_progress.parent.mkdir(parents=True)
            task_progress.write_text(json.dumps({
                "task_id": TASK, "request_id": REQUEST,
                "task_epoch": EPOCH, "runtime_state": "PAUSED",
                "interruption_reason": "admission_repair_exhausted",
            }), encoding="utf-8")
            log = root / "localdata" / "logs" / "remote_runtime.jsonl"
            log.parent.mkdir(parents=True)
            log.write_text(json.dumps({
                "event": "STATUS", "stage": "task_paused",
                "task_id": TASK, "request_id": REQUEST,
                "task_epoch": EPOCH, "timestamp": 1794190000.0,
            }) + "\n", encoding="utf-8")
            events = []
            for i in range(5):
                if i == 0:
                    code = "run_command_operation_invalid"
                    fields = [{
                        "field": "operation", "reason": "invalid_enum",
                        "expected_type": "enum", "actual_type": "str",
                        "actual_value": "GIT_CLONE",
                        "allowed_values": ["GIT_INSPECT", "GIT_MUTATE"],
                    }]
                    stage = "SCHEMA_VALIDATION"
                else:
                    code = "wrong_field_type"
                    fields = [{
                        "field": "verify", "reason": "wrong_type",
                        "expected_type": "list | dict", "actual_type": "str",
                    }]
                    stage = "SCHEMA_VALIDATION"
                events.append({
                    # The final row exercises historical Git37 compatibility.
                    "kind": "TOOL_RESULT" if i == 4 else "ADMISSION_REJECTED",
                    "round": i + 1,
                    "toolName": "run_command", "arguments": {
                        "command": "SECRET_DO_NOT_EXPORT_G37",
                    }, "arguments_sha256": ("%d" % (i + 1)) * 64,
                    "runtimeEvidence": {
                        "status": "REJECTED", "executed": False,
                        "reason_code": code, "evidence_ref": "",
                    },
                    "admission_diagnostic": {
                        "schema": "SMARTAGENT_ACTION_ADMISSION_DIAGNOSTIC_V1",
                        "stage": stage, "tool": "run_command",
                        "repair_class": (
                            "MODEL_SCHEMA_REPAIR" if i else "MODEL_ACTION_REASSESSMENT"
                        ),
                        "reason_code": code, "field_issues": fields,
                        "executed": False, "authorization_granted": False,
                    },
                    "admission_repair_attempt": i,
                })
            board = root / "localdata" / "runtime" / "executor_run_blackboard" / (TASK + ".json")
            board.parent.mkdir(parents=True)
            board.write_text(json.dumps({
                "schema": "SMARTAGENT_EXECUTOR_RUN_BLACKBOARD_V1",
                "task_id": TASK,
                "request_id": request_override or REQUEST,
                "task_epoch": EPOCH, "events": events,
                "terminal": {"admission_rejection_count": expected_count},
            }), encoding="utf-8")
            process = subprocess.run(
                ["cmd.exe", "/d", "/c", str(root / "collect.bat"), TASK, REQUEST],
                cwd=root, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            results = list((root / "localdata" / "diagnostics" / "gar_live_rca").glob("*.zip"))
            self.assertEqual(len(results), 1)
            with zipfile.ZipFile(results[0]) as z:
                return {name: z.read(name).decode("utf-8") for name in z.namelist()
                        if name.endswith(".json")}

    def test_all_five_executor_rejections_exported_with_typed_fields(self):
        data = self._collect()
        summary = json.loads(data["executor_admission_summary.json"])
        assert summary["status"] == "MATCHED", summary
        assert summary["observed_rejection_count"] == 5
        assert summary["terminal_rejection_count"] == 5
        assert summary["exported_rejection_count"] == 5
        rejects = json.loads(data["action_admission_rejections.json"])
        assert len(rejects) == 5
        assert all(row["executed"] is False for row in rejects)
        assert all(row["authorization_granted"] is False for row in rejects)
        assert rejects[0]["admission_stage"] == "SCHEMA_VALIDATION"
        assert rejects[0]["field_issues"][0]["field"] == "operation"
        assert rejects[0]["field_issues"][0]["actual_value"] == "GIT_CLONE"
        assert rejects[0]["field_issues"][0]["allowed_values"] == [
            "GIT_INSPECT", "GIT_MUTATE"
        ]
        assert rejects[1]["field_issues"][0]["field"] == "verify"
        assert rejects[1]["field_issues"][0]["expected_type"] == "list | dict"
        assert rejects[1]["field_issues"][0]["actual_type"] == "str"
        coverage = json.loads(data["rca_causal_coverage.json"])
        assert coverage["executor_admission_rejection_count"] == 5
        assert coverage["admission_rejection_count"] == 5
        assert "EXECUTOR_ADMISSION_REJECTION_COVERAGE_INCOMPLETE" not in coverage["issues"]
        assert "SECRET_DO_NOT_EXPORT_G37" not in "\n".join(data.values())

    def test_mismatched_blackboard_terminal_count_marks_rca_partial(self):
        data = self._collect(expected_count=6)
        coverage = json.loads(data["rca_causal_coverage.json"])
        assert coverage["status"] == "PARTIAL"
        assert "EXECUTOR_ADMISSION_REJECTION_COVERAGE_INCOMPLETE" in coverage["issues"]

    def test_foreign_request_blackboard_is_not_merged(self):
        data = self._collect(request_override="RR-FOREIGN-G37")
        summary = json.loads(data["executor_admission_summary.json"])
        assert summary["status"] == "INVALID"
        assert json.loads(data["action_admission_rejections.json"]) == []
        coverage = json.loads(data["rca_causal_coverage.json"])
        assert coverage["status"] == "PARTIAL"
        assert "EXECUTOR_BLACKBOARD_UNVERIFIED" in coverage["issues"]


if __name__ == "__main__":
    unittest.main()
