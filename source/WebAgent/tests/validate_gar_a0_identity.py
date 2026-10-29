"""GAR A0 diagnostic boundary tests; no real browser, Controller restart or task."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_core import source_attestation, task_progress
from WebAgent import protocol_loop
from WebAgent.tests.validate_tri_one_5a_protocol_it import answer_only_response


class GARInProcessA0Tests(unittest.TestCase):
    def test_diagnostic_is_explicitly_opt_in(self):
        self.assertFalse(source_attestation.gar_a0_enabled({}))
        self.assertFalse(source_attestation.gar_a0_enabled(
            {"SMARTAGENT_GAR_A0_DIAGNOSTIC": "0"}
        ))
        self.assertTrue(source_attestation.gar_a0_enabled(
            {"SMARTAGENT_GAR_A0_DIAGNOSTIC": "1"}
        ))

    def test_capture_is_not_a_sibling_controller_attestation(self):
        # A healthy module on disk is not an attested running Controller.
        record = source_attestation.capture_gar_a0(controller_main=None)
        self.assertEqual(record["schema"], "SMARTAGENT_GAR_A0_LOADED_CODE_V1")
        self.assertEqual(record["loaded_code_status"], "NOT_ATTESTED")
        self.assertEqual(record["source_commit_status"], "NOT_ATTESTED")
        self.assertTrue(record["manifest_strict"])
        self.assertTrue(
            record["modules"]["agent_core.task_progress"]
            ["strict_manifest_disk_match"]
        )
        self.assertTrue(
            record["modules"]["WebAgent.protocol_loop"]["match"]
        )

    def test_modified_loaded_function_fails_closed(self):
        original = task_progress.validate_model_progress
        try:
            task_progress.validate_model_progress = lambda *_args, **_kw: None
            record = source_attestation.capture_gar_a0(
                controller_main=None
            )
            row = record["modules"]["agent_core.task_progress"]
            self.assertFalse(row["match"])
            self.assertFalse(
                row["functions"]["validate_model_progress"]["match"]
            )
        finally:
            task_progress.validate_model_progress = original

    def test_missing_module_cannot_be_imported_for_proof(self):
        with patch.dict(sys.modules, {"WebAgent.fresh_context": None}):
            record = source_attestation.capture_gar_a0(
                controller_main=None, require_fresh_context=True,
            )
            self.assertFalse(
                record["modules"]["WebAgent.fresh_context"]["loaded"]
            )
            self.assertEqual(record["loaded_code_status"], "NOT_ATTESTED")

    def test_no_raw_environment_or_absolute_paths_in_capture(self):
        secret = "A0_SECRET_SHOULD_NOT_APPEAR"
        with patch.dict(os.environ, {"A0_PRIVATE_TEST_SECRET": secret}):
            record = source_attestation.capture_gar_a0(
                controller_main=None,
            )
        raw = json.dumps(record)
        self.assertNotIn(secret, raw)
        self.assertNotIn(str(source_attestation.__file__), raw)
        self.assertNotIn('"pid"', raw)
        self.assertNotIn('"source_commit":', raw)

    def test_controller_binding_never_accepts_unrelated_callable(self):
        record = source_attestation.capture_gar_a0(
            controller_main=lambda: None,
        )
        self.assertEqual(record["loaded_code_status"], "NOT_ATTESTED")
        self.assertFalse(
            record["modules"]["WebAgent.controller"]["match"]
        )


    def test_real_loop_request_emits_bound_a0_event_without_fabricated_pass(self):
        events = []
        with tempfile.TemporaryDirectory(prefix="gar-a0-request-") as directory:
            root = Path(directory)
            loop = protocol_loop.WebAgentProtocolLoop(
                root, lambda prompt, expected, attachments: answer_only_response(),
                progress_root=root, max_turns=2,
                event_sink=lambda name, **fields: events.append((name, fields)),
            )
            loop._gar_a0_diagnostic_enabled = True
            loop._gar_a0_instance_digest = "A0-TEST-INSTANCE"
            loop._gar_a0_controller_main = lambda: None
            loop._gar_a0_target_salt = b"test-only-private-salt"
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                loop.run("Explain RAII in C++.")
        samples = [data for name, data in events
                   if name == "gar_a0_request_attestation"]
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample["controller_instance_digest"], "A0-TEST-INSTANCE")
        self.assertEqual(sample["actual_origin"], "WEB_DIRECT")
        self.assertTrue(sample["gar_effective"])
        self.assertTrue(sample["gar_engine_present"])
        self.assertTrue(sample["task_result_effective"])
        self.assertTrue(sample["text_only_effective"])
        self.assertEqual(sample["a0_status"], "NOT_ATTESTED")
        self.assertEqual(
            sample["source_identity"]["loaded_code_status"], "NOT_ATTESTED"
        )
        self.assertEqual(loop.terminal_outcome, "SUCCESS")



    def test_request_observes_configured_fresh_adapter_without_using_it(self):
        events = []
        with tempfile.TemporaryDirectory(prefix="gar-a0-adapter-") as directory:
            root = Path(directory)
            loop = protocol_loop.WebAgentProtocolLoop(
                root, lambda prompt, expected, attachments: answer_only_response(),
                progress_root=root, max_turns=2,
                event_sink=lambda name, **fields: events.append((name, fields)),
                fresh_context_adapter=object(),
            )
            loop._gar_a0_diagnostic_enabled = True
            loop._gar_a0_instance_digest = "A0-TEST-ADAPTER"
            loop._gar_a0_controller_main = lambda: None
            loop._gar_a0_target_salt = b"test-only-private-salt"
            with patch.dict(os.environ, {
                "SMARTAGENT_GAR_V1": "1",
                "TRI_ONE_TASK_RESULT_V1": "1",
                "TRI_ONE_TEXT_ONLY_V1": "1",
            }):
                loop.run("Explain RAII in C++.")
        samples = [data for name, data in events
                   if name == "gar_a0_request_attestation"]
        self.assertEqual(len(samples), 1)
        self.assertTrue(samples[0]["fresh_adapter_present"])
        self.assertEqual(samples[0]["a0_status"], "NOT_ATTESTED")
        self.assertEqual(loop.terminal_outcome, "SUCCESS")



if __name__ == "__main__":
    unittest.main(verbosity=2)
