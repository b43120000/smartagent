"""Synthetic local trace verifier tests. No browser, Planner or Runtime."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import verify_capture as v


class TestVerifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nlit_verifier_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _hash(blob):
        return hashlib.sha256(blob).hexdigest()

    def make_round(self, n=2, *, include_user=True, result_id="RES-NEW-001",
                   prompt_result_id="RES-NEW-001", omit_submit=False,
                   reverse_order=False):
        folder = self.root / "request-digest" / ("round-%04d" % n)
        folder.mkdir(parents=True)
        prompt = (f"[WEBAGENT_TOOL_RESULTS]\nRUN_ID=RR-NEW-001\n"
                  f"RESULT_ID={prompt_result_id}\n"
                  "{}\n[/WEBAGENT_TOOL_RESULTS]\n").encode()
        response = b"test-scoped synthetic model response"
        (folder / "composer_input_utf8.bin").write_bytes(prompt)
        (folder / "assistant_utf8.bin").write_bytes(response)
        def record(kind, **fields):
            return {
                "request_id": "RR-NEW-001",
                "task_id": "TASK-NEW-001",
                "task_epoch": "EPOCH-NEW-001",
                "round": n,
                "result_id": result_id,
                "kind": kind,
                **fields,
            }
        evs = [record("PLANNER_TRANSPORT_ENTERED"),
               record("composer_input_utf8.bin_CAPTURED", sha256=self._hash(prompt),
                      bytes=len(prompt)),
               record("PREPARED_NOT_SUBMITTED"),
               record("COMPOSER_WRITER_VERIFIED"),
               record("SEND_READY_VERIFIED")]
        if not omit_submit:
            evs.append(record("SUBMIT_CLICK_RETURNED_NOT_DELIVERY_PROOF"))
        if include_user:
            evs.append(record("USER_TURN_CONFIRMED_BY_PRODUCTION_SCRAPER"))
        evs += [
            record("assistant_utf8.bin_CAPTURED", sha256=self._hash(response),
                   bytes=len(response)),
            record("MODEL_RESPONSE_RETURNED",
                   user_turn_confirmed=include_user, capture_complete=True),
        ]
        if reverse_order:
            evs[3], evs[4] = evs[4], evs[3]
        (folder / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evs) + "\n", encoding="utf-8")
        return folder

    def test_correlated_real_post_tool_pattern(self):
        result = v.analyze_round(self.make_round())
        self.assertEqual(result["status"], "MODEL_RESPONSE_CORRELATED")
        self.assertEqual(result["failed_checks"], [])

    def test_wrong_result_id_fails(self):
        result = v.analyze_round(self.make_round(prompt_result_id="RES-WRONG"))
        self.assertIn("POST_TOOL_RESULT_ID_NOT_IN_EXACT_INPUT",
                      result["failed_checks"])

    def test_unconfirmed_user_turn_fails(self):
        result = v.analyze_round(self.make_round(include_user=False))
        self.assertIn("MISSING_USER_TURN_CONFIRMED_BY_PRODUCTION_SCRAPER",
                      result["failed_checks"])

    def test_click_not_proof(self):
        result = v.analyze_round(self.make_round(omit_submit=True))
        self.assertIn("MISSING_SUBMIT_CLICK_RETURNED_NOT_DELIVERY_PROOF",
                      result["failed_checks"])

    def test_stage_order_must_be_monotonic(self):
        result = v.analyze_round(self.make_round(reverse_order=True))
        self.assertIn("STAGE_ORDER_INCONSISTENT", result["failed_checks"])

    def test_initial_round_alone_must_not_exit_zero(self):
        self.make_round(n=1)
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["verify_capture.py", "--private-root", str(self.root)]):
            with contextlib.redirect_stdout(stdout):
                code = v.main()
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["post_tool_rounds_correlated"], 0)
        self.assertEqual(payload["original_historical_case_pass_inferred"], False)


if __name__ == "__main__":
    unittest.main()
