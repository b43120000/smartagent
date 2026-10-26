"""Offline IT10 diagnostic gates; no browser, Telegram API or Runtime Action."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent_core import path_integrity_trace as PIT
from agent_core.composer_operation import _page_snapshot
from agent_core.source_attestation import capture


REQUEST = (
    "NLIT_CASE_ID=IT10\n"
    "NLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n"
    "NLIT_MANUAL_RUN_ID=IT10-STRICT-GATE-TEST\n"
    "E:\\card\\__fixture__\\probe.txt\n"
)


class IT10EvidenceGates(unittest.TestCase):
    def test_local_sender_handoff_is_scoped_and_worker_enables_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            settings = {
                "SMARTAGENT_IT10_PATH_TRACE": "1",
                "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
                "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": "IT10-GATES-OFFLINE",
                "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(private),
            }
            with patch.dict(os.environ, settings):
                PIT.begin(REQUEST, transport="LOCAL_TEST")
                self.assertEqual(PIT.diagnostic_status(REQUEST), "READY")
                handoff = PIT.export_handoff()
                self.assertEqual(handoff["schema"], "IT10_TRACE_HANDOFF_V1")

            task = SimpleNamespace(request=REQUEST, metadata={
                "transport": "LOCAL_TEST", "it10_trace_handoff": handoff})
            with patch.dict(os.environ, {}, clear=True):
                child_env = PIT.worker_env_from_handoff(task)
                self.assertEqual(child_env["SMARTAGENT_IT10_PATH_TRACE"], "1")
                self.assertEqual(PIT.diagnostic_status(REQUEST), "FLAG_DISABLED")
                with patch.dict(os.environ, child_env):
                    PIT.begin(REQUEST, task_id="TASK-OFFLINE", transport="LOCAL_TEST")
                    PIT.record("06_worker_input", REQUEST)
                    self.assertEqual(PIT.diagnostic_status(REQUEST), "READY")
            paths = list(private.glob("it10-path-*.jsonl"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text().splitlines()[0])["stage"], "06_worker_input")

            # Invalid or foreign request does not inherit a diagnostic capability.
            self.assertEqual(PIT.worker_env_from_handoff(SimpleNamespace(
                request="unrelated", metadata=task.metadata)), {})
            self.assertEqual(PIT.worker_env_from_handoff(SimpleNamespace(
                request=REQUEST, metadata={"transport": "TELEGRAM",
                                            "it10_trace_handoff": handoff})), {})
            self.assertEqual(PIT.worker_env_from_handoff(SimpleNamespace(
                request=REQUEST + "tampered", metadata=task.metadata)), {})
            bad = {**handoff, "issued_at": 0}
            self.assertEqual(PIT.worker_env_from_handoff(SimpleNamespace(
                request=REQUEST, metadata={"transport": "LOCAL_TEST",
                                            "it10_trace_handoff": bad})), {})

    def test_gate_reason_only_and_private_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(PIT.diagnostic_status(REQUEST), "FLAG_DISABLED")
            PIT.begin(REQUEST)
            self.assertFalse(PIT.context())
        self.assertEqual(PIT.diagnostic_status("ordinary request"), "NOT_IT10")

    def test_writer_persists_native_strict_result_without_prompt(self):
        validated = SimpleNamespace(
            matched=True, matched_view="semantic_inverse",
            expected_length=35105, observed_length=35105,
            expected_sha256="expected-hash", observed_sha256="observed-hash")
        adapter = SimpleNamespace(
            validate_composer=lambda prompt: validated,
            observation_turns=lambda role: [],
            generation_active=lambda: False)
        scraper = SimpleNamespace(
            _web_ui_adapter=lambda: adapter,
            _normalize_composer_text=lambda value: value,
            _read_composer_text=lambda: "visible",
            _page=SimpleNamespace(url="about:blank"))
        snap = _page_snapshot(scraper, "PRIVATE_PROMPT_CONTENT")
        self.assertTrue(snap["composer_matches"])
        self.assertEqual(snap["composer_validation"]["matched_view"], "semantic_inverse")
        self.assertEqual(snap["composer_validation"]["observed_sha256"], "observed-hash")
        self.assertNotIn("PRIVATE_PROMPT_CONTENT", json.dumps(snap))

    def test_source_attestation_provenance(self):
        result = capture(role="offline_test", manifest_verified=False)
        self.assertEqual(result["schema"], "SMARTAGENT_SOURCE_ATTESTATION_V1")
        self.assertFalse(result["manifest_verified_before_capture"])
        self.assertTrue(result["disk_head_is_not_loaded_source_proof"])
        module = result["modules"]["agent_core.path_integrity_trace"]
        self.assertTrue(module["loaded"])
        self.assertIn("_settings", module["live_code_sha256"])
        self.assertEqual(len(module["live_code_sha256"]["_settings"]), 64)

    def test_tracked_debug_default_is_safe(self):
        root = Path(__file__).resolve().parents[3]
        payload = json.loads((root / "config" / "debug_config.defaults.json").read_text(encoding="utf-8"))
        self.assertIs(payload["composer_verification"]["unsafe_force_pass"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
