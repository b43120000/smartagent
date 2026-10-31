"""Offline mainline Hybrid Composer regression: no browser, no network, no user files."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core import composer_operation as composer
from agent_core.web_runtime import WebLLMScraper


class HybridMainlineTests(unittest.TestCase):
    def _writer(self, prompt: str, *, hybrid: bool):
        typed = [""]
        calls = []

        class Keyboard:
            def insert_text(self, value):
                assert "\n" not in value and "\r" not in value
                typed[0] += value
                calls.append(("type", len(value)))

            def press(self, key):
                calls.append(("key", key))
                if key == "Shift+Enter":
                    typed[0] += "\n"
                else:
                    assert key == "Control+End"

        class Scraper:
            _page = SimpleNamespace(keyboard=Keyboard())
            _last_composer_validation = None

            def _composer_matches_prompt(self, expected):
                calls.append(("read", len(expected)))
                return typed[0] == expected

            def _normalize_composer_text(self, value):
                return value.replace("\r\n", "\n")

            def _focus_composer_dom(self):
                return None

            def _check_cancel_requested(self, stage):
                assert stage == "composer_chunk"

            def _log_stage(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "journal.json"
            with patch("agent_core.paths.log_root", return_value=root):
                if hybrid:
                    composer._write_hybrid_prompt(Scraper(), prompt, {}, journal)
                else:
                    composer._write_staged_prompt(Scraper(), prompt, {}, journal)
            saved = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(saved["confirmed_prefix"]["chars"], len(prompt))
            self.assertEqual(saved["confirmed_prefix"]["sha256"],
                             hashlib.sha256(prompt.encode()).hexdigest())
            if hybrid:
                diag = root / "composer-writer" / "hybrid_writer.jsonl"
                row = json.loads(diag.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(row["mode"], "ONE_SHOT_PREFIX512_BULK_TAIL")
                self.assertEqual(row["prefix_chars"], min(512, len(prompt)))
                self.assertEqual(row["status"], "VERIFIED")
                self.assertNotIn("prompt", row)
        self.assertEqual(typed[0], prompt)
        return calls

    def test_hybrid_multiline_fewer_readbacks_without_submits(self):
        prompt = ("Line A " + "Q"*32 + "\n")*85 + "END"
        fast = self._writer(prompt, hybrid=True)
        protected = self._writer(prompt, hybrid=False)
        self.assertLess(sum(x[0] == "read" for x in fast),
                        sum(x[0] == "read" for x in protected))
        self.assertTrue(any(x == ("key", "Shift+Enter") for x in fast))
        self.assertFalse(any(x == ("key", "Enter") for x in fast))

    def test_longline_exact_prefix_and_bounded_tail(self):
        prompt = "S" * 5120
        calls = self._writer(prompt, hybrid=True)
        self.assertIn(("type", 512), calls)
        self.assertLessEqual(max(n for kind,n in calls if kind == "type"), 4096)
        self.assertEqual(sum(n for kind,n in calls if kind == "type"), len(prompt))
        self.assertLessEqual(sum(kind == "read" for kind,n in calls), 6)

    def test_hybrid_crlf_atomic(self):
        # Boundary 512 is inside CRLF. The editor stores one LF; its strict
        # adapter normalizes before comparing the final DOM text.
        prompt = "A" * 511 + "\n" + "B" * 650
        self._writer(prompt, hybrid=True)

    def test_hybrid_tail_corruption_rejected(self):
        typed = [""]
        class Keyboard:
            def insert_text(self, value):
                typed[0] += value[:1]
            def press(self, key):
                if key == "Shift+Enter":
                    typed[0] += "\n"
        class Scraper:
            _page = SimpleNamespace(keyboard=Keyboard())
            def _composer_matches_prompt(self, value):
                return typed[0] == value
            def _normalize_composer_text(self, value):
                return value
            def _focus_composer_dom(self):
                return None
            def _check_cancel_requested(self, stage):
                pass
            def _log_stage(self, *args):
                pass
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_core.composer_operation._await_chunk_readback",
            side_effect=composer.ComposerOperationError("composer_chunk_readback_failed")
        ):
            with self.assertRaises(composer.ComposerOperationError):
                composer._write_hybrid_prompt(
                    Scraper(), "A"*1100, {}, Path(tmp)/"journal.json")

    def test_mainline_dispatch_and_opt_out(self):
        scraper = WebLLMScraper.__new__(WebLLMScraper)
        scraper.service = "chatgpt"
        scraper._isolated_browser = None
        scraper._page = SimpleNamespace(url="https://chatgpt.com/c/test")
        scraper._composer_hybrid_task_enabled = False
        scraper.get_cdp_endpoint = lambda: "http://127.0.0.1:9222"
        scraper._check_cancel_requested = lambda stage: None
        scraper._log_stage = lambda *args: None
        prompt = "P"*1500
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_core.composer_operation.prepare_composer",
            return_value={"operation_id": "TEST"}
        ) as prepare, patch(
            "agent_core.paths.runtime_root", return_value=Path(tmp)
        ), patch.dict(os.environ, {"SMARTAGENT_COMPOSER_HYBRID_V1": "1"}):
            scraper._write_prompt_to_composer(prompt)
            self.assertFalse(prepare.call_args.kwargs["hybrid"])
            scraper._composer_hybrid_task_enabled = True
            for _ in range(2):
                scraper._write_prompt_to_composer(prompt)
                self.assertTrue(prepare.call_args.kwargs["hybrid"])
            scraper._write_prompt_to_composer("short")
            self.assertFalse(prepare.call_args.kwargs["hybrid"])
            scraper._composer_hybrid_task_enabled = False
            scraper._write_prompt_to_composer(prompt)
            self.assertFalse(prepare.call_args.kwargs["hybrid"])
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_core.composer_operation.prepare_composer",
            return_value={"operation_id": "TEST"}
        ) as prepare, patch(
            "agent_core.paths.runtime_root", return_value=Path(tmp)
        ), patch.dict(os.environ, {"SMARTAGENT_COMPOSER_HYBRID_V1": "0"}):
            scraper._composer_hybrid_task_enabled = True
            scraper._write_prompt_to_composer(prompt)
            self.assertFalse(prepare.call_args.kwargs["hybrid"])
        scraper.service = "gemini"
        with patch.object(scraper, "_write_prompt_to_composer_legacy",
                          return_value=False) as old:
            scraper._write_prompt_to_composer(prompt)
            old.assert_called_once()

    def test_launcher_defaults_on_but_preserves_explicit_off(self):
        launcher = (Path(__file__).resolve().parents[3] /
                    "launch_webcopilot.bat").read_text(encoding="utf-8")
        self.assertIn('if not defined SMARTAGENT_COMPOSER_HYBRID_V1 set "SMARTAGENT_COMPOSER_HYBRID_V1=1"', launcher)
        controller = (Path(__file__).resolve().parents[1] /
                      "controller.py").read_text(encoding="utf-8")
        self.assertIn("scraper._composer_hybrid_task_enabled = False", controller)
        self.assertIn("scraper._composer_hybrid_task_enabled = bool(", controller)


if __name__ == "__main__":
    unittest.main(verbosity=2)
