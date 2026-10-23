"""Offline unit tests for the opt-in private NLIT trace collector.

No production Planner, browser, Runtime, network, or CDP is launched.
Run with PYTHONPATH=.codex/nlit_private_capture:
  python -m unittest discover -s .codex/nlit_private_capture -p test_private_capture.py -v
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import private_capture as c


class FakeScraper:
    def _write_prompt_to_composer(self, prompt):
        self._log_stage("send_ready")
        return True

    def _log_stage(self, stage, detail=""):
        return None


class FakeBrowserClient:
    def __init__(self):
        self.scraper = FakeScraper()

    def ask(self, prompt, *, protocol_expected=None, stage=""):
        self.scraper._write_prompt_to_composer(prompt + "::transport_layer")
        self.scraper._log_stage("submit_returned")
        self.scraper._log_stage("user_sent")
        return "actual_observed_assistant_result"


class FakeUnconfirmedBrowser:
    def __init__(self):
        self.scraper = FakeScraper()

    def ask(self, prompt, *, protocol_expected=None, stage=""):
        self.scraper._write_prompt_to_composer(prompt)
        self.scraper._log_stage("submit_returned")
        return "transport_returned_without_user_turn_evidence"


class FakeProtocolLoop:
    def _run_impl(self, request, *, request_id="", task_id="", task_epoch="",
                  client=None, turn=2):
        return client.ask(
            "actual_pretransport_planner_payload",
            protocol_expected={"run_id": request_id, "task_id": task_id,
                               "task_epoch": task_epoch, "turn_id": turn,
                               "ack_result_id": "RES-PRIVATE-001"},
            stage="smartagent_tool 規劃回合",
        )


class TestPrivateCapture(unittest.TestCase):
    def setUp(self):
        # These fake classes are unique per Python process, and patching the
        # same ones more than once is idempotent.
        c.patch_protocol_loop(FakeProtocolLoop)
        c.patch_browser_client(FakeBrowserClient)
        c.patch_scraper(FakeScraper)
        c.patch_browser_client(FakeUnconfirmedBrowser)
        self._root = tempfile.TemporaryDirectory(prefix="nlit_trace_test_")
        self.root = Path(self._root.name)
        self.nonce = "abcdef1234567890abcdef1234567890"
        self.settings = patch.dict(os.environ, {
            "SMARTAGENT_NLIT_CAPTURE_NONCE": self.nonce,
            "SMARTAGENT_NLIT_CAPTURE_ROOT": str(self.root),
            "SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED": "YES",
        })
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
        self._root.cleanup()

    def request(self):
        return f"NLIT_TRACE_TOKEN={self.nonce}\nNew opt-in diagnostic request."

    def invoke(self, client=None, *, turn=2, request=None):
        return FakeProtocolLoop()._run_impl(
            request if request is not None else self.request(),
            request_id="RR-LOCAL_TEST-NEW-002",
            task_id="TASK-NEW-002", task_epoch="NEW-EPOCH-002",
            client=client or FakeBrowserClient(), turn=turn,
        )

    def traces(self):
        return list(self.root.glob("request-*/round-*"))

    def events(self, directory):
        return [json.loads(x) for x in (directory / "events.jsonl").read_text(
            encoding="utf-8").splitlines()]

    def test_matching_request_captures_exact_composer_and_return(self):
        self.assertEqual(self.invoke(), "actual_observed_assistant_result")
        folders = self.traces()
        self.assertEqual(len(folders), 1)
        folder = folders[0]
        self.assertEqual((folder / "composer_input_utf8.bin").read_bytes(),
                         b"actual_pretransport_planner_payload::transport_layer")
        self.assertEqual((folder / "assistant_utf8.bin").read_bytes(),
                         b"actual_observed_assistant_result")
        self.assertIn("USER_TURN_CONFIRMED_BY_PRODUCTION_SCRAPER",
                      [e["kind"] for e in self.events(folder)])
        returned = next(e for e in self.events(folder)
                        if e["kind"] == "MODEL_RESPONSE_RETURNED")
        self.assertTrue(returned["user_turn_confirmed"])
        self.assertTrue(returned["capture_complete"])

    def test_non_opted_request_is_noop(self):
        self.invoke(request="Some other unrelated production task.")
        self.assertEqual(self.traces(), [])

    def test_turn_one_is_recorded_as_initial_planner_round(self):
        self.invoke(turn=1)
        [folder] = self.traces()
        self.assertEqual(folder.name, "round-0001")
        self.assertFalse((folder / "round-0002").exists())

    def test_no_user_turn_is_never_claimed_as_confirmed(self):
        self.invoke(client=FakeUnconfirmedBrowser())
        [folder] = self.traces()
        returned = next(e for e in self.events(folder)
                        if e["kind"] == "MODEL_RESPONSE_RETURNED")
        self.assertFalse(returned["user_turn_confirmed"])

    def test_identity_mismatch_never_captures(self):
        class WrongExpected(FakeBrowserClient):
            def ask(self, prompt, *, protocol_expected=None, stage=""):
                protocol_expected = {**protocol_expected, "task_epoch": "WRONG"}
                return super().ask(prompt, protocol_expected=protocol_expected,
                                   stage=stage)
        self.invoke(client=WrongExpected())
        # A derived class does not have the opt-in wrapper, so must remain a
        # no-op. The production client wrapper independently checks identity.
        self.assertEqual(self.traces(), [])


if __name__ == "__main__":
    unittest.main()
