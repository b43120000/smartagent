"""Tri-One 5B offline cross-interface routing/receipt cases.

No real ChatGPT/Telegram, no UI, no network or system mutation.

Run from repository root with PYTHONPATH=source:
  python -B source/WebAgent/tests/validate_tri_one_5b_shared_worker.py
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.current_conversation_adapter import (
    build_webcopilot_message, is_internal_webcopilot_turn,
)
from agent_core.tri_one_shared_worker import use_shared_v9_worker
from agent_core.remote_events import RemoteEventStore, DeliveryManager
from agent_core.tri_one_task_result import (
    make_task_result, validate_task_result,
)


class SharedWorkerSelectionTests(unittest.TestCase):
    def test_default_off_keeps_original_workers(self):
        self.assertTrue(use_shared_v9_worker("TELEGRAM", {}))
        self.assertTrue(use_shared_v9_worker("LOCAL_TEST", {}))
        self.assertFalse(use_shared_v9_worker("WEBGPT_COPILOT", {}))
        self.assertFalse(use_shared_v9_worker("LOCAL_AGENT", {
            "TRI_ONE_TASK_RESULT_V1": "1",
            "TRI_ONE_TEXT_ONLY_V1": "1",
            "TRI_ONE_WEBCOPILOT_V9_V1": "1",
        }))

    def test_webcopilot_requires_all_three_admin_flags(self):
        flags = {
            "TRI_ONE_TASK_RESULT_V1": "1",
            "TRI_ONE_TEXT_ONLY_V1": "1",
            "TRI_ONE_WEBCOPILOT_V9_V1": "1",
        }
        self.assertTrue(use_shared_v9_worker("WEBGPT_COPILOT", flags))
        for key in tuple(flags):
            changed = dict(flags)
            changed[key] = "0"
            with self.subTest(disabled=key):
                self.assertFalse(use_shared_v9_worker("WEBGPT_COPILOT", changed))


class WebCopilotIngressTests(unittest.TestCase):
    def test_v9_protocol_control_turn_cannot_reingest(self):
        for body in (
            "[WEBAGENT_REQUEST_SOURCE]\nREMOTEAGENT_WEBCOPILOT_V9",
            "[WEBAGENT_TOOL_RESULTS]\nTool result",
            "[WEBAGENT_PROTOCOL_REJECTED]\nmissing field",
            "[AGENT_SESSION_ATTACH]\nready",
            "[REMOTE_AGENT_RESULT]\nevent_id=EVT-1",
            "[SMARTAGENT_V8_REQUIRED] control",
        ):
            with self.subTest(first_line=body.splitlines()[0]):
                self.assertTrue(is_internal_webcopilot_turn(body))
                with self.assertRaises(ValueError):
                    build_webcopilot_message(
                        body,
                        conversation_url="https://chatgpt.com/c/fixture",
                        turn_index=1, allow_plain=True,
                    )

    def test_human_plain_request_is_still_ingested(self):
        message = build_webcopilot_message(
            "Explain RAII in C++.",
            conversation_url="https://chatgpt.com/c/fixture",
            turn_index=2, allow_plain=True,
        )
        self.assertEqual(message.transport, "WEBGPT_COPILOT")
        self.assertEqual(message.text, "Explain RAII in C++.")


class OutboxTests(unittest.TestCase):
    def test_duplicate_complete_event_and_confirmed_delivery_do_not_duplicate(self):
        # Feature flags OFF: this checks the unchanged transport-neutral
        # event/outbox semantics without requiring a signed V9 receipt.
        with tempfile.TemporaryDirectory() as directory:
            store = RemoteEventStore(Path(directory) / "events.json")
            task = SimpleNamespace(
                task_id="TASK-5B-OUTBOX", request_id="RR-5B-OUTBOX",
                reply_route={"transport": "WEBGPT_COPILOT",
                             "conversation_url": "https://chatgpt.com/c/fixture"},
                metadata={"transport": "WEBGPT_COPILOT"},
            )
            with patch.dict(os.environ, {"TRI_ONE_TASK_RESULT_V1": "0"}):
                event = store.emit("TASK_COMPLETED", task, status="COMPLETED",
                                   payload={"summary": "Result"})
                again = store.emit("TASK_COMPLETED", task, status="COMPLETED",
                                   payload={"summary": "Result"})
            self.assertEqual(event.event_id, again.event_id)
            self.assertEqual(len(store.events), 1)
            claimed = store.claim_delivery(event.event_id, "fixture-owner")
            self.assertIsNotNone(claimed)
            self.assertIsNone(store.claim_delivery(event.event_id, "second-owner"))
            delivered = store.mark_delivered(event.event_id, adapter_result={
                "delivered": True, "reply": "visible message",
            })
            self.assertEqual(delivered.delivery_state, "DELIVERED")
            self.assertEqual(store.ready(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
