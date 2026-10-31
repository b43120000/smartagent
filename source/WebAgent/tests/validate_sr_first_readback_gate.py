#!/usr/bin/env python3
"""SR First Readback: offline deterministic positive/negative/security tests."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from agent_core.sr_first_readback import (
    SRFirstReadbackCaptured, SRModeConflictError, capture_first_readback,
    classify, enabled, assert_sr_p2_isolated, runtime_mode_attestation,
)
from WebAgent.tests.collect_sr_readback_bundle import bundle
from WebAgent.tests.validate_shared_runtime_sr02_sr03 import (
    cpp_content, json_content, make_wire,
)
from agent_core.web_runtime import WebLLMScraper
from agent_core import web_runtime as runtime_module


class FakeNode:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class FakeTurn:
    def __init__(self, text):
        self.element = FakeNode(text)


class SRFirstReadbackIT(unittest.TestCase):
    def test_opt_in_only(self):
        with patch.dict(os.environ, {
            "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1": "0",
        }):
            self.assertFalse(enabled())
        with patch.dict(os.environ, {
            "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1": "1",
        }):
            self.assertTrue(enabled())

    def test_sr_p2_mutual_exclusion_flag_matrix(self):
        combos = (
            ("0", "0", "NORMAL", False),
            ("1", "0", "P1_FIRST_READBACK", False),
            ("0", "1", "P2", False),
            ("1", "1", "CONFLICT", True),
        )
        for sr_gate, p2, mode, conflicts in combos:
            flags = {
                "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1": sr_gate,
                "SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1": p2,
                "SMARTAGENT_COMPOSER_HYBRID_ONCE_V1": "1",
                "PRIVATE_RUNTIME_SECRET": "DO_NOT_LOG",
            }
            with self.subTest(sr_gate=sr_gate, p2=p2):
                attestation = runtime_mode_attestation(flags)
                self.assertEqual(attestation["mode_status"], mode)
                self.assertEqual(attestation["sr_gate_active"], sr_gate == "1")
                self.assertEqual(attestation["p2_diagnostic_active"], p2 == "1")
                self.assertTrue(attestation["hybrid_once_active"])
                self.assertNotIn("DO_NOT_LOG", json.dumps(attestation))
                if conflicts:
                    with self.assertRaisesRegex(SRModeConflictError, "SR_P2_MODE_CONFLICT"):
                        assert_sr_p2_isolated(flags)
                else:
                    self.assertEqual(assert_sr_p2_isolated(flags), attestation)

    def test_scraper_conflict_blocks_before_composer_or_page_access(self):
        # _page and all other Browser state are intentionally absent.
        scraper = WebLLMScraper.__new__(WebLLMScraper)
        self.assertIs(runtime_module.SRFirstReadbackCaptured, SRFirstReadbackCaptured)
        flags = {
            "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1": "1",
            "SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1": "1",
        }
        with patch.dict(os.environ, flags):
            with self.assertRaisesRegex(SRModeConflictError, "SR_P2_MODE_CONFLICT"):
                scraper.ask("Must never reach the composer")
        self.assertFalse(hasattr(scraper, "_calibrated_file_request"))

    def test_controller_conflict_fails_before_browser_attach(self):
        from WebAgent import controller
        with tempfile.TemporaryDirectory(prefix="sr-p2-conflict-") as temp:
            root = Path(temp)
            with patch.dict(os.environ, {
                "SMARTAGENT_SR_FIRST_READBACK_ONLY_V1": "1",
                "SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1": "1",
            }), patch("WebAgent.controller._resolve_workspace", return_value=root), \\
                    patch("agent_core.protocol_manifest.require_protocol_manifest"), \\
                    patch("WebAgent.controller.install_root", return_value=root), \\
                    patch("WebAgent.controller.log_root", return_value=root / "logs"), \\
                    patch("WebAgent.controller.RuntimeLifecycle.from_environment", return_value=None), \\
                    patch("WebAgent.controller.open_or_attach_browser",
                          side_effect=AssertionError("Browser attached during mode conflict")) as attach:
                outcome = controller.main([
                    "--workspace", str(root),
                    "--webgpt-url", "https://chatgpt.com/c/fixture",
                ])
            self.assertEqual(outcome, 2)
            attach.assert_not_called()
            events = [
                json.loads(row) for row in (root / "logs" / "webagent_runtime.jsonl")
                .read_text(encoding="utf-8").splitlines()
            ]
            attested = [row for row in events if row.get("event") == "controller_mode_attestation"]
            rejected = [row for row in events if row.get("event") == "controller_mode_rejected"]
            self.assertEqual(len(attested), 1)
            self.assertEqual(attested[0]["mode_status"], "CONFLICT")
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["reason"], "SR_P2_MODE_CONFLICT")

    def test_cpp_and_json_capture_without_action(self):
        with tempfile.TemporaryDirectory(prefix="sr-gate-") as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {
                "SMARTAGENT_SR_EVIDENCE_DIR": str(root / "localdata/logs/sr-first-readback"),
            }):
                for turn, raw, marker in (
                    (1, make_wire(cpp_content()), "SR02_END"),
                    (2, make_wire(json_content()), "SR03_END"),
                ):
                    result = capture_first_readback(
                        text=raw, assistant_turn=FakeTurn(raw),
                        response_source={
                            "scope_id": "private-scope",
                            "assistant_id_sha256": "hash-id",
                            "assistant_fingerprint": "fp",
                        },
                        expected={"run_id": "SR-TEST", "turn_id": turn},
                        ui_idle_seconds=5.0, stable_seconds=2.2,
                    )
                    self.assertIsInstance(result, SRFirstReadbackCaptured)
                    data = json.loads((result.capture_dir / "capture.json").read_text(encoding="utf-8"))
                    self.assertEqual(data["schema"], "SMARTAGENT_SR_FIRST_READBACK_V1")
                    self.assertEqual(data["diagnostic"]["verdict"], "STRICT_SINGLE_FENCE")
                    self.assertEqual(data["diagnostic"]["actions_executed"], 0)
                    self.assertEqual(data["diagnostic"]["repair_prompts_sent"], 0)
                    self.assertEqual(data["diagnostic"]["semantic_ack_admission"], "NOT_RUN")
                    self.assertEqual(data["dom_and_extractor_equal"], True)
                    structure = json.loads(
                        (result.capture_dir / "dom_selected_structure.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(structure["status"], "SELECTOR_API_UNAVAILABLE")
                    self.assertEqual(
                        (result.capture_dir / "extracted_response.txt").read_text(encoding="utf-8"),
                        raw,
                    )
                    self.assertIn(marker, raw)
                    self.assertEqual(data["scope_id_sha256"], hashlib.sha256(
                        b"private-scope"
                    ).hexdigest())
                    self.assertNotIn("private-scope", (result.capture_dir / "capture.json").read_text())

    def test_invalid_original_is_not_autofixed(self):
        raw = "Human-readable RAII, without V9 envelope"
        outcome = classify(raw)
        self.assertEqual(outcome["verdict"], "PARSE_FAILED")
        self.assertEqual(outcome["v9_normalizations"], [])
        self.assertEqual(outcome["semantic_ack_admission"], "NOT_RUN")

    def test_v9_normalized_raw_json_stays_distinct(self):
        raw = json.dumps({"tool": "final_response", "content": "hi"})
        outcome = classify(raw)
        self.assertEqual(outcome["verdict"], "NORMALIZED_ONLY")
        self.assertIn("V9_EXCLUSIVE_JSON_CANONICALIZED", outcome["v9_normalizations"])

    def test_no_commit_is_rejected(self):
        self.assertIn(
            classify("just some natural prose")["verdict"],
            {"PARSE_FAILED", "COMMIT_MISSING"},
        )

    def test_runtime_hook_bypasses_protocol_and_no_reply_in_controller(self):
        import inspect
        source = inspect.getsource(WebLLMScraper._wait_for_response_complete)
        first = source.index("sr_first_readback_captured")
        normal = source.index("if protocol_expected:", first)
        self.assertLess(first, normal)
        self.assertIn("raise result", source[first:normal])
        controller = (Path(__file__).resolve().parents[1] / "controller.py").read_text(encoding="utf-8")
        self.assertIn("if sr_first_readback_enabled():", controller)
        self.assertIn("continue\n                from agent_core.task_progress", controller)
        self.assertIn("NO model ACK sent", controller)

    def test_zip_allowlist_does_not_include_secret_files(self):
        with tempfile.TemporaryDirectory(prefix="sr-bundle-") as tmp:
            root = Path(tmp)
            capture = root / "localdata/logs/sr-first-readback/captures/SR-1"
            capture.mkdir(parents=True)
            (capture / "capture.json").write_text('{"schema":"test"}', encoding="utf-8")
            (capture / "extracted_response.txt").write_text("SR02_END", encoding="utf-8")
            (capture / "cookies.txt").write_text("PRIVATE_COOKIE", encoding="utf-8")
            (root / "localdata/logs/sr-first-readback-regression/TEST").mkdir(parents=True)
            (root / "localdata/logs/sr-first-readback-regression/TEST/summary.txt").write_text(
                "passed=9", encoding="utf-8"
            )
            archive = bundle(root)
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                self.assertIn("captures/SR-1/capture.json", names)
                self.assertIn("captures/SR-1/extracted_response.txt", names)
                self.assertIn("regression/TEST/summary.txt", names)
                self.assertFalse(any("cookies" in name for name in names))
                self.assertNotIn("PRIVATE_COOKIE", "\n".join(
                    zf.read(n).decode("utf-8") for n in names
                ))
                self.assertIsNone(zf.testzip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
