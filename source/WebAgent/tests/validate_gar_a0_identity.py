"""GAR A0 diagnostic boundary tests; no real browser, Controller restart or task."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

from agent_core import source_attestation, task_progress
from WebAgent import protocol_loop


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
