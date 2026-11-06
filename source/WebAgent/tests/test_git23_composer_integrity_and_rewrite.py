"""Git23 offline: strict composer text, bounded repair and rewrite causality."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core import composer_operation as co
from agent_core.action_resolution_trace import executed_trace, rejection_traces
from agent_core.web_ui.contracts import ComposerSnapshot
from agent_core.web_ui.providers.chatgpt.composer import validate_composer_prompt


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, amount):
        self.now += amount


class FakeComposer:
    """Fake text writer; all repair decisions still run production Python."""
    def __init__(self, *, lost_last=False, corrupt_head=False):
        self.text = ""
        self.lost_last = lost_last
        self.corrupt_head = corrupt_head
        self.input_calls = 0
        self.clear_calls = 0
        self.stage_events = []
        self.loc = SimpleNamespace(
            evaluate=lambda script, *args: None,
            is_visible=lambda: True,
            is_enabled=lambda: True,
            is_editable=lambda: True,
        )
        outer = self

        class Keyboard:
            @staticmethod
            def press(key):
                if key == "Shift+Enter":
                    outer.text += "\n"
                else:
                    assert key == "Control+End"

            @staticmethod
            def insert_text(value):
                outer.input_calls += 1
                if outer.input_calls == 1 and outer.lost_last:
                    value = value[:-1]
                if outer.input_calls == 1 and outer.corrupt_head:
                    value = "!" + value[1:]
                outer.text += value

        self._page = SimpleNamespace(keyboard=Keyboard())

    def _composer_matches_prompt(self, expected):
        return self.text == expected

    def _read_composer_text_candidates(self):
        return [self.text]

    def _focus_composer_dom(self):
        return self.loc

    def _composer_locator(self):
        return self.loc

    def _normalize_composer_text(self, value):
        return value

    def _check_cancel_requested(self, stage):
        assert stage == "composer_chunk"

    def _log_stage(self, stage, detail):
        self.stage_events.append((stage, detail))


def fake_snapshot(scraper, _prompt):
    return {
        "url": "https://chatgpt.com/c/owned",
        "user_count": 1,
        "assistant_count": 1,
        "last_turn_status": "LAST_ASSISTANT",
        "user_anchors": [["u", "same-user"]],
        "assistant_anchors": [["a", "same-assistant"]],
        "generation_active": False,
        "composer_empty": scraper.text == "",
    }


def fake_atomic_clear(scraper, primary):
    if scraper.text != primary:
        return False
    scraper.clear_calls += 1
    scraper.text = ""
    return True


class ComposerIntegrityTests(unittest.TestCase):
    def test_whitespace_and_linebreak_differences_are_not_equal(self):
        def match(expected, actual):
            return validate_composer_prompt(
                expected, ComposerSnapshot(actual, actual)).matched
        self.assertTrue(match("a\nb", "a\r\nb"))
        self.assertTrue(match("a b", "a\u00a0b"))
        self.assertFalse(match("a\n\nb", "a\nb"))
        self.assertFalse(match("a  b", "a b"))
        self.assertFalse(match("a\tb", "a b"))
        self.assertFalse(match("  a", "a"))
        self.assertFalse(match("a\n", "a"))
        self.assertTrue(match("中文https://example.com", "中文 https://example.com"))

    def test_readback_reports_stable_character_diff_without_text(self):
        clock = Clock()
        scraper = FakeComposer()
        scraper.text = "BEGIN  lost"
        with self.assertRaises(co.ComposerOperationError) as raised:
            co._await_chunk_readback(
                scraper, "BEGIN  \nlost", timeout_sec=1,
                clock=clock.monotonic, sleep=clock.sleep,
                confirmed_chars=5,
            )
        data = raised.exception.evidence
        self.assertTrue(data["stable_readback"])
        self.assertEqual(data["first_diff_index"], 7)
        self.assertTrue(data["confirmed_prefix_intact"])
        self.assertEqual(data["observed_length"], len(scraper.text))
        self.assertNotIn("BEGIN", str(data))
        self.assertEqual(
            data["observed_sha256"],
            hashlib.sha256(scraper.text.encode()).hexdigest(),
        )

    def test_bounded_reset_and_smaller_batches_after_truncated_tail(self):
        prompt = "OWNED-BEGIN-" + "abcde" * 895
        scraper = FakeComposer(lost_last=True)
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "state.json"
            record = {"state": "IN_FLIGHT", "baseline": fake_snapshot(scraper, "")}
            with patch.object(co.time, "monotonic", clock.monotonic), \
                 patch.object(co.time, "sleep", clock.sleep), \
                 patch.object(co, "_page_snapshot", fake_snapshot), \
                 patch.object(co, "_atomic_clear_owned", fake_atomic_clear), \
                 patch.object(co.PIT, "record", return_value=None):
                co._write_staged_prompt(scraper, prompt, record, journal)
            self.assertEqual(scraper.text, prompt)
            self.assertEqual(scraper.clear_calls, 1)
            self.assertEqual(record["composer_repair"]["state"], "REWRITTEN_AND_VERIFIED")
            self.assertEqual(record["composer_repair"]["max_full_restarts"], 1)
            self.assertTrue(record["composer_repair_attempted"])
            self.assertEqual(record["confirmed_prefix"]["chars"], len(prompt))
            self.assertGreater(scraper.input_calls, 10)
            saved = journal.read_text(encoding="utf-8")
            self.assertNotIn("OWNED-BEGIN-", saved)
            self.assertIn("first_diff_index", saved)

    def test_changed_first_chars_are_not_owned_and_not_cleared(self):
        prompt = "OWNED-BEGIN-" + "x" * 2048
        scraper = FakeComposer(corrupt_head=True)
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "state.json"
            record = {"state": "IN_FLIGHT", "baseline": fake_snapshot(scraper, "")}
            with patch.object(co.time, "monotonic", clock.monotonic), \
                 patch.object(co.time, "sleep", clock.sleep), \
                 patch.object(co, "_page_snapshot", fake_snapshot), \
                 patch.object(co, "_atomic_clear_owned", fake_atomic_clear), \
                 patch.object(co.PIT, "record", return_value=None):
                with self.assertRaises(co.ComposerOperationError):
                    co._write_staged_prompt(scraper, prompt, record, journal)
            self.assertEqual(scraper.clear_calls, 0)
            self.assertEqual(scraper.input_calls, 1)
            self.assertNotIn("composer_repair_attempted", record)


class TailAndRetryLimitTests(unittest.TestCase):
    def test_native_input_can_replace_latest_batch_without_reset(self):
        prompt = "A" * 4096 + "B" * 501
        scraper = FakeComposer()
        clock = Clock()
        selection = {"start": None}
        raw_inserts = []

        def input_text(value):
            raw_inserts.append(value)
            if len(raw_inserts) == 2:
                value = value[:-1]
            if selection["start"] is not None:
                scraper.text = scraper.text[:selection["start"]] + value
                selection["start"] = None
            else:
                scraper.text += value

        def evaluate(script, args=None):
            if isinstance(args, list) and len(args) == 2:
                primary, prefix = args
                if scraper.text != primary or not scraper.text.startswith(prefix):
                    return False
                selection["start"] = len(prefix)
                return True
            return None

        scraper._page.keyboard.insert_text = input_text
        scraper.loc.evaluate = evaluate
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "tail.json"
            record = {"state": "IN_FLIGHT", "baseline": fake_snapshot(scraper, "")}
            with patch.object(co.time, "monotonic", clock.monotonic), \
                 patch.object(co.time, "sleep", clock.sleep), \
                 patch.object(co, "_page_snapshot", fake_snapshot), \
                 patch.object(co, "_atomic_clear_owned", fake_atomic_clear), \
                 patch.object(co.PIT, "record", return_value=None):
                co._write_staged_prompt(scraper, prompt, record, journal)
            self.assertEqual(scraper.text, prompt)
            self.assertEqual(scraper.clear_calls, 0)
            self.assertEqual(len(raw_inserts), 3)
            self.assertEqual(record["composer_repair"]["state"], "VERIFIED")
            self.assertEqual(
                record["composer_repair"]["mode"], "NATIVE_INPUT_LATEST_BATCH"
            )

    def test_one_full_reset_is_maximum_even_if_retry_truncates(self):
        prompt = "OWNED-BEGIN-" + "R" * 4000
        scraper = FakeComposer()
        clock = Clock()

        def always_truncate(value):
            scraper.input_calls += 1
            scraper.text += value[:-1]

        scraper._page.keyboard.insert_text = always_truncate
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "once.json"
            record = {"state": "IN_FLIGHT", "baseline": fake_snapshot(scraper, "")}
            with patch.object(co.time, "monotonic", clock.monotonic), \
                 patch.object(co.time, "sleep", clock.sleep), \
                 patch.object(co, "_page_snapshot", fake_snapshot), \
                 patch.object(co, "_atomic_clear_owned", fake_atomic_clear), \
                 patch.object(co.PIT, "record", return_value=None):
                with self.assertRaises(co.ComposerOperationError):
                    co._write_staged_prompt(scraper, prompt, record, journal)
            self.assertEqual(scraper.clear_calls, 1)
            self.assertTrue(record["composer_repair_attempted"])
            self.assertEqual(record["composer_repair"]["state"], "RESET_CONFIRMED")
            self.assertIn("readback_failure", record)


class CanonicalRewriteTests(unittest.TestCase):
    def _trace(self, original, canonical, info=None):
        return executed_trace({
            "request_id": "R1", "task_epoch": "E1", "round": 9,
            "action_id": "A1", "original_action": original,
            "canonical_action": canonical,
            "dispatched_action": canonical,
            "actual_result": {"status": "SUCCEEDED", "executed": True},
        }, rewrite_info=info, request_id="R1", task_epoch="E1")

    def test_scalar_path_to_paths_is_rewrite_with_rule(self):
        original = {"action_id": "A1", "tool": "inspect_directory", "path": "E:\\\\Work"}
        canonical = {"action_id": "A1", "tool": "inspect_directory", "paths": ["E:\\\\Work"]}
        trace = self._trace(original, canonical)
        resolved = trace["runtime_resolved"]
        self.assertEqual(resolved["disposition"], "REWRITTEN_AND_DISPATCHED")
        self.assertEqual(resolved["rewrite_reason_code"], "CANONICAL_FORM_NORMALIZATION")
        self.assertIn("SINGULAR_PATH_TO_PATHS_LIST", resolved["rewrite_rule_ids"])
        self.assertEqual(resolved["planner_canonical_form"]["paths"], ["E:\\\\Work"])
        self.assertTrue(trace["execution"]["executed"])

    def test_unknown_normalization_is_explicitly_unclassified(self):
        original = {"action_id": "A1", "tool": "run_command", "operation": None}
        canonical = {"action_id": "A1", "tool": "run_command", "operation": "GIT_INSPECT"}
        resolved = self._trace(original, canonical)["runtime_resolved"]
        self.assertEqual(resolved["rewrite_reason_code"], "CANONICAL_FORM_UNCLASSIFIED")
        self.assertEqual(resolved["changed_fields"], ["operation"])
        self.assertEqual(resolved["rewrite_rules"][0]["to_type"], "str")

    def test_rejected_candidate_never_claims_execution(self):
        original = {"action_id": "A1", "tool": "inspect_directory", "path": "X"}
        candidate = {"action_id": "A1", "tool": "inspect_directory", "paths": ["X"]}
        trace = rejection_traces(
            [candidate], [{"reason": "blocked"}],
            original_actions={"A1": original}, rewrite_notes={},
            request_id="R1", task_epoch="E1", round_id=9,
        )[0]
        self.assertEqual(
            trace["runtime_resolved"]["disposition"], "REWRITE_CANDIDATE_NOT_ADMITTED"
        )
        self.assertEqual(trace["execution"]["status"], "NOT_EXECUTED")
        self.assertFalse(trace["runtime_resolved"]["rewrite_applied"])
        self.assertTrue(trace["runtime_resolved"]["rewrite_attempted"])


if __name__ == "__main__":
    unittest.main()
