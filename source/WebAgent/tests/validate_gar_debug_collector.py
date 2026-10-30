"""Offline tests for one-click GAR L4 debug data collection. No browser required."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from WebAgent.tools.collect_gar_l4_debug import (
    collect, event_for_export, review_info, scrub, select_request,
)


class GARCollectorTests(unittest.TestCase):
    def test_sanitize_raw_prompt_url_credentials_and_keep_review_error_marker(self):
        event = {
            "event": "browser_response_completed",
            "timestamp": "2026-10-30T21:00:00+0800",
            "pid": 111,
            "stage": "GAR L4 WebGPT2 read-only recovery review",
            "response": json.dumps({
                "schema": "SMARTAGENT_GAR_L4_REVIEW_V1",
                "nonce": "a" * 32,
                "completed_steps": [],
                "blocked_step": "V9 formatting",
                "failure_analysis": "missing_smartagent_tool_envelope",
                "revised_step": "Continue original step",
                "remaining_plan": [],
                "safety_notes": [],
            }),
            "request": "SECRET PRIVATE USER REQUEST",
            "browser_url": "https://chatgpt.com/c/confidential",
            "token": "secret",
        }
        sanitized = event_for_export(event)
        serialized = json.dumps(sanitized)
        self.assertNotIn("SECRET PRIVATE", serialized)
        self.assertNotIn("chatgpt.com", serialized)
        self.assertNotIn('"token"', serialized)
        self.assertNotIn('"nonce"', serialized)
        self.assertIn("missing_smartagent_tool_envelope", serialized)
        self.assertEqual(sanitized["review_metadata"]["json_valid"], True)
        self.assertIn("[REDACTED_SECRET]", scrub("password: secret1"))
        self.assertIn("[REDACTED_URL]", scrub("https://chatgpt.com/c/private"))
        self.assertEqual(review_info("not-json")["json_valid"], False)
        failure = event_for_export({
            "event": "browser_response_failed",
            "error": ("[WEB_COMPOSER_UNCONFIRMED] reason="
                      "composer_helper_failed:composer_chunk_readback_failed"
                      " https://chatgpt.com/c/private"),
        })
        self.assertEqual(failure["transport_error_codes"],
                         ["WEB_COMPOSER_UNCONFIRMED"])
        self.assertEqual(failure["composer_reason_code"],
                         "composer_helper_failed:composer_chunk_readback_failed")
        self.assertNotIn("error", failure)

    def test_select_request_isolation_and_full_d1_event_signature(self):
        pid = 44
        events = [
            {"event": "gar_d1_test_armed", "pid": pid, "timestamp": "T0"},
            {"event": "gar_d1_test_fault_injected", "pid": pid, "sequence": 1},
            {"event": "gar_local_repair_handoff", "pid": pid,
             "request_id": "WA-ONE", "round": 5},
            {"event": "gar_l4_webgpt1_paused_for_review", "pid": pid,
             "request_id": "WA-ONE", "round": 7},
            {"event": "gar_l4_webgpt2_advice_received", "pid": pid,
             "request_id": "WA-ONE", "round": 7},
            {"event": "gar_l4_webgpt1_resumed", "pid": pid,
             "request_id": "WA-ONE", "planner_owner": "ORIGINAL_WEBGPT1"},
            {"event": "gar_d1_test_resume_release", "pid": pid},
            {"event": "protocol_loop_completed", "pid": pid, "request_id": "WA-ONE",
             "task_outcome": "SUCCESS"},
            {"event": "protocol_loop_completed", "pid": pid, "request_id": "WA-TWO",
             "task_outcome": "SUCCESS"},
        ]
        req, selected = select_request(events, "WA-ONE")
        self.assertEqual(req, "WA-ONE")
        names = [e["event"] for e in selected]
        self.assertIn("gar_d1_test_armed", names)
        self.assertIn("gar_d1_test_resume_release", names)
        self.assertEqual(names.count("protocol_loop_completed"), 1)
        self.assertEqual(select_request(events)[0], "WA-TWO")

    def test_creates_uploadable_folder_and_zip_without_raw_prompt(self):
        with tempfile.TemporaryDirectory(prefix="gar-collector-ut-") as tmp:
            root = Path(tmp)
            logdir = root / "localdata" / "logs"
            logdir.mkdir(parents=True)
            events = [
                {"event": "gar_d1_test_armed", "pid": 19, "timestamp": "T1",
                 "request": "raw private user prompt"},
                {"event": "gar_recovery_decision", "pid": 19,
                 "request_id": "WA-TEST", "level": "L4", "timestamp": "T2"},
                {"event": "gar_l4_webgpt1_paused_for_review", "pid": 19,
                 "request_id": "WA-TEST", "timestamp": "T3"},
                {"event": "gar_l4_webgpt2_advice_received", "pid": 19,
                 "request_id": "WA-TEST", "timestamp": "T4"},
                {"event": "gar_l4_webgpt1_resumed", "pid": 19,
                 "request_id": "WA-TEST", "timestamp": "T5"},
                {"event": "gar_d1_test_resume_release", "pid": 19,
                 "timestamp": "T6"},
                {"event": "protocol_loop_completed", "pid": 19,
                 "request_id": "WA-TEST", "task_outcome": "SUCCESS",
                 "final_content": "PRIVATE MODEL ANSWER", "timestamp": "T7"},
            ]
            (logdir / "webagent_runtime.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
            journals = root / "localdata" / "runtime" / "composer_operations"
            journals.mkdir(parents=True)
            (journals / "private_page_hash.json").write_text(json.dumps({
                "owner_pid": 19, "state": "QUARANTINED",
                "url": "https://chatgpt.com/c/private",
                "prompt": "SECRET_COMPOSER_PROMPT",
                "prompt_chars": 11719,
                "pending_prefix": {"chars": 4096},
                "reason": "writer_exception_or_cancelled",
                "readback_failure": {
                    "chunk_start": 0, "chunk_end": 4096,
                    "chunk_chars": 4096, "expected_prefix_chars": 4096,
                    "observed_length": 1024,
                    "expected_prefix_sha256": "a" * 64,
                    "observed_sha256": "b" * 64,
                    "ignored_secret": "SECRET_OTHER",
                },
            }), encoding="utf-8")
            outdir = collect(root, request_id="WA-TEST",
                             now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
            self.assertTrue(outdir.is_dir())
            zip_path = outdir.with_suffix(".zip")
            self.assertTrue(zip_path.is_file())
            report = json.loads((outdir / "acceptance_report.json").read_text("utf-8"))
            self.assertEqual(report["selected_request_id"], "WA-TEST")
            self.assertFalse(report["event_checks"]["l4_full_e2e"])
            # Handoff was not present: never elevate ordinary SUCCESS to full D1.
            bundle = (outdir / "runtime_events_sanitized.jsonl").read_text("utf-8")
            self.assertNotIn("PRIVATE MODEL ANSWER", bundle)
            self.assertNotIn("raw private user prompt", bundle)
            journal_export = (outdir / "composer_journals_redacted.json").read_text("utf-8")
            self.assertIn("QUARANTINED", journal_export)
            self.assertIn("4096", journal_export)
            self.assertIn('"observed_length": 1024', journal_export)
            self.assertNotIn("private", journal_export)
            self.assertNotIn("SECRET_COMPOSER_PROMPT", journal_export)
            self.assertNotIn("SECRET_OTHER", journal_export)
            self.assertEqual(report["composer_journal_count"], 1)
            with zipfile.ZipFile(zip_path) as zf:
                self.assertIn(outdir.name + "/acceptance_report.json", zf.namelist())
                self.assertIn(outdir.name + "/composer_journals_redacted.json", zf.namelist())
                self.assertNotIn("webagent_runtime.jsonl", zf.namelist())


if __name__ == "__main__":
    unittest.main(verbosity=2)
