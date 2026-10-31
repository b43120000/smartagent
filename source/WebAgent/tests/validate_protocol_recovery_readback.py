#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline recovery-readback identity regression against CURRENT WebLLMScraper.

The obsolete reload helper and the old test-only CSS-selector FakePage are not
part of this Runtime anymore. This fixture tests the existing request-scope,
durable assistant identity, response hash and conservative freshness policy
without creating a browser, sending a prompt or inventing DOM selectors.

Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_protocol_recovery_readback.py
"""
from __future__ import annotations

import hashlib
import inspect
import json
import unittest
from types import SimpleNamespace

from agent_core.web_runtime import WebLLMScraper


class FakeTurn:
    def __init__(self, durable_id: str, fingerprint: str):
        self.durable_id = durable_id
        self.fingerprint = fingerprint


class FakeAdapter:
    def durable_turn_id(self, turn: FakeTurn) -> str:
        return turn.durable_id

    def element_fingerprint(self, turn: FakeTurn) -> str:
        return turn.fingerprint


def source(scope_id: str, assistant_id: str, text: str, *, fingerprint: str = "fp"):
    """Capture through the actual shared Runtime helper, not fake selectors."""
    scraper = WebLLMScraper.__new__(WebLLMScraper)
    scraper.service = "chatgpt"
    scraper._web_ui_adapter = lambda: FakeAdapter()
    return scraper._protocol_response_source(
        {"_web_ui_scope": SimpleNamespace(scope_id=scope_id)},
        FakeTurn(assistant_id, fingerprint),
        text,
    )


class RecoveryReadbackIdentityIT(unittest.TestCase):
    def setUp(self):
        self.before = source(
            "request-scope-old", "provider-assistant-old", "old V9 reply",
            fingerprint="fp-old",
        )

    def classify(self, current: dict):
        return WebLLMScraper._classify_protocol_recovery_readback(
            self.before, current,
        )

    def test_source_is_request_scoped_and_redacted(self):
        self.assertEqual(self.before["scope_id"], "request-scope-old")
        self.assertEqual(self.before["response_sha256"], hashlib.sha256(
            b"old V9 reply",
        ).hexdigest())
        self.assertEqual(self.before["assistant_id_sha256"], hashlib.sha256(
            b"provider-assistant-old",
        ).hexdigest())
        self.assertEqual(self.before["assistant_fingerprint"], "fp-old")
        self.assertNotIn("provider-assistant-old", json.dumps(self.before))

    def test_three_independent_changes_prove_freshness(self):
        after = source(
            "request-scope-new", "provider-assistant-new", "new V9 reply",
            fingerprint="fp-old",  # a weak fingerprint must not veto 3 changes
        )
        self.assertEqual(self.classify(after), {
            "fresh": True, "reason": "fresh_recovery_response",
        })

    def test_same_response_sha_is_stale_even_with_new_scope_and_id(self):
        after = source(
            "request-scope-new", "provider-assistant-new", "old V9 reply",
            fingerprint="fp-new",
        )
        self.assertEqual(self.classify(after), {
            "fresh": False, "reason": "response_sha_unchanged",
        })

    def test_same_request_scope_blocks_freshness(self):
        after = source(
            "request-scope-old", "provider-assistant-new", "new V9 reply",
            fingerprint="fp-new",
        )
        self.assertEqual(self.classify(after), {
            "fresh": False, "reason": "request_scope_unchanged",
        })

    def test_same_assistant_id_blocks_freshness(self):
        after = source(
            "request-scope-new", "provider-assistant-old", "new V9 reply",
            fingerprint="fp-new",
        )
        self.assertEqual(self.classify(after), {
            "fresh": False, "reason": "assistant_identity_unchanged",
        })

    def test_missing_provider_id_falls_back_to_fingerprint(self):
        previous = source(
            "request-scope-old", "", "old V9 reply", fingerprint="fp-old",
        )
        same_fp = source(
            "request-scope-new", "", "new V9 reply", fingerprint="fp-old",
        )
        new_fp = source(
            "request-scope-new", "", "new V9 reply", fingerprint="fp-new",
        )
        classifier = WebLLMScraper._classify_protocol_recovery_readback
        self.assertEqual(classifier(previous, same_fp), {
            "fresh": False, "reason": "assistant_fingerprint_unchanged",
        })
        self.assertEqual(classifier(previous, new_fp), {
            "fresh": True, "reason": "fresh_recovery_response",
        })

    def test_no_rejected_baseline_is_explicit(self):
        self.assertEqual(
            WebLLMScraper._classify_protocol_recovery_readback(
                {}, self.before,
            ),
            {"fresh": True, "reason": "no_rejected_baseline"},
        )

    def test_diagnostic_relations_and_tokens_do_not_expose_ids(self):
        after = source(
            "request-scope-new", "provider-assistant-new", "old V9 reply",
        )
        verdict = self.classify(after)
        detail = WebLLMScraper._gar_l4_readback_identity_summary(
            self.before, after, verdict,
        )
        self.assertEqual(detail["freshness"], "STALE")
        self.assertEqual(detail["reason"], "response_sha_unchanged")
        self.assertEqual(detail["assistant_id"]["relation"], "CHANGED")
        self.assertEqual(detail["scope_id"]["relation"], "CHANGED")
        self.assertEqual(detail["response_sha"]["relation"], "SAME")
        encoded = json.dumps(detail)
        self.assertNotIn("provider-assistant-old", encoded)
        self.assertNotIn("request-scope-old", encoded)
        self.assertNotIn("old V9 reply", encoded)

    def test_waiter_rejects_unattested_recovery_readback(self):
        waiter = inspect.getsource(WebLLMScraper._wait_for_response_complete)
        self.assertIn("_classify_protocol_recovery_readback(", waiter)
        self.assertIn('diagnostic["kind"] = "stale_recovery_readback"', waiter)
        self.assertIn('"protocol_recovery_stale_readback"', waiter)
        self.assertIn("if not bool(readback.get(\"fresh\")):", waiter)
        self.assertIn("action_executed=false", waiter)


def run() -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        RecoveryReadbackIdentityIT,
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("PROTOCOL_RECOVERY_READBACK_FAILED")
    report = {
        "schema": "SMARTAGENT_RECOVERY_READBACK_CURRENT_RUNTIME_V1",
        "passed": result.testsRun,
        "errors": len(result.errors),
        "failures": len(result.failures),
        "browser_started": False,
        "prompt_resubmitted": False,
        "action_executed": False,
    }
    print("PROTOCOL_RECOVERY_READBACK_OK")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    run()
