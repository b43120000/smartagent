"""Tri-One PR10-2A remaining isolated legacy and sensitive-injection boundaries.

Fixture-only test. It does not create a real Worker, browser, task writer,
live delivery or a trusted source identity. Do not count as Live acceptance.

With checkout root as cwd:
    PYTHONPATH=source python -B source/WebAgent/tests/validate_tri_one_2a_legacy_edges.py
"""
from __future__ import annotations

import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from agent_core.remote_events import DeliveryManager, RemoteEventStore
from agent_core.tri_one_task_result import (
    FEATURE_FLAG, TaskResultContractError, validate_task_result,
)
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from RemoteAgent.webgpt_delivery import WebGPTDeliveryAdapter

from validate_tri_one_2a_matrix import (
    Fixture, FakeTelegramClient, FakeWebTransport,
)
from validate_tri_one_2a_task_result import build


class LegacyAndSensitiveEdges(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_u02_u10_legacy_off_telegram_store_and_adapter(self):
        task = self.fixture.accept("TELEGRAM", source_id="legacy-T")
        with patch.dict(os.environ, {FEATURE_FLAG: "0"}):
            _, event = self.fixture.complete(
                task, plan_id="PLAN-LEGACY-T",
                artifacts=[{"kind": "photo", "path": "fixture-not-sent.png"}],
            )
        baseline = deepcopy(event.payload)
        self.assertNotIn("task_result_v1", baseline)
        self.assertNotIn("source_metadata", baseline)
        for field in ("summary", "task_outcome", "plan_id", "artifacts",
                      "terminal_acceptance", "workspace"):
            self.assertIn(field, baseline)
        reopened = RemoteEventStore(self.fixture.root / "events.json")
        loaded = reopened.events[event.event_id]
        self.assertEqual(loaded.payload, baseline)
        # Do not actually deliver the nonexistent fixture artifact. Render-only
        # compatibility checks exercise public legacy formatting without send.
        wire = DeliveryManager._wire(loaded)
        self.assertIn("PLAN-LEGACY-T", TelegramDeliveryAdapter.render(wire))
        self.assertIn("2A fixture answer", TelegramDeliveryAdapter.render(wire))

    def test_u02_u10_legacy_off_web_store_and_fake_transport(self):
        task = self.fixture.accept("WEBGPT_COPILOT", source_id="legacy-W")
        with patch.dict(os.environ, {FEATURE_FLAG: "0"}):
            _, event = self.fixture.complete(task, plan_id="PLAN-LEGACY-W")
        baseline = deepcopy(event.payload)
        self.assertNotIn("task_result_v1", baseline)
        self.assertNotIn("source_metadata", baseline)
        reopened = RemoteEventStore(self.fixture.root / "events.json")
        self.assertEqual(reopened.events[event.event_id].payload, baseline)
        fake = FakeWebTransport()
        manager = DeliveryManager(reopened, {
            "WEBGPT_COPILOT": WebGPTDeliveryAdapter(fake)
        })
        self.assertTrue(manager.deliver(reopened.events[event.event_id])["delivered"])
        self.assertEqual(len(fake.messages), 1)
        wire = fake.messages[0][1]
        self.assertIn("2A fixture answer", wire["rendered_text"])
        self.assertNotIn("task_result_v1", wire["payload"])
        self.assertEqual(wire["payload"]["plan_id"], "PLAN-LEGACY-W")
        reloaded = RemoteEventStore(self.fixture.root / "events.json")
        self.assertEqual(reloaded.events[event.event_id].delivery_state, "DELIVERED")

    def test_u09_canonical_builder_strips_route_secrets(self):
        for route, origin in [
            ({"transport": "TELEGRAM", "chat_id": "secret-route-1"}, "TELEGRAM"),
            ({"transport": "WEBGPT_COPILOT", "conversation_url": "secret-route-2"},
             "WEBGPT_COPILOT"),
            ({"transport": "LOCAL_TEST", "private_token": "secret-route-3"},
             "LOCAL_TEST"),
        ]:
            with self.subTest(source=origin):
                record = build(trusted_reply_route=route, source_interface=origin)
                output = json.dumps(record, ensure_ascii=False)
                self.assertNotIn("secret-route-", output)
                self.assertNotIn("chat_id", record)
                self.assertNotIn("conversation_url", record)

    def test_u09_direct_stored_sensitive_injection_is_rejected(self):
        # This is an explicit security-boundary probe, not a declaration that
        # production currently rejects it. If it fails, report objective
        # regression evidence to Owner; Codex must not change product Source.
        clean = build()
        for location in ("top_level", "delivery", "terminal_acceptance"):
            with self.subTest(location=location):
                forged = deepcopy(clean)
                if location == "top_level":
                    forged["chat_id"] = "secret-sentinel-top"
                elif location == "delivery":
                    forged["delivery"]["auth_token"] = "secret-sentinel-delivery"
                else:
                    forged["terminal_acceptance"]["auth_token"] = "secret-sentinel-receipt"
                with self.assertRaises(TaskResultContractError):
                    validate_task_result(forged)


if __name__ == "__main__":
    outcome = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LegacyAndSensitiveEdges)
    )
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
