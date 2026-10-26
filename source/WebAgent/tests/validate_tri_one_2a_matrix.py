"""Tri-One 2A dedicated U01..U10 and I01..I07 offline matrix.

Real components: Gateway, TaskQueue, EventStore/DeliveryManager and rendering
adapters. Admitted receipts are deliberately *fixtures*, not proof that a
browser/Planner/Runtime ran. Browser and Live verification remain separate.

Run from the candidate checkout with PYTHONPATH=source:
    python -B source/WebAgent/tests/validate_tri_one_2a_matrix.py

Only creates files inside TemporaryDirectory. No external send, browser,
production task store, writer, Live request, service or process lifecycle.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.agent_gateway import AgentIngressGateway
from agent_core.remote_events import (
    DeliveryManager, RemoteEventStore, EVENT_TYPES,
)
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.transport_message import NormalizedInboundMessage
from agent_core.transport_sessions import TransportSessionRouter
from agent_core.tri_one_task_result import (
    FEATURE_FLAG, TaskResultContractError, enabled, make_task_result,
    make_task_result_from_remote_event, validate_task_result,
)
from RemoteAgent.telegram_artifacts import prepare_telegram_completion
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from RemoteAgent.webgpt_delivery import WebGPTDeliveryAdapter


def receipt(task, *, outcome="SUCCESS", state="COMPLETED",
            refs=("ACT-1",), verdict="PASS"):
    return {
        "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
        "admission_status": "ACCEPTED",
        "receipt_id": "TERM-2A-FIXTURE",
        "request_id": task.request_id,
        "task_id": task.task_id,
        "task_epoch": task.task_epoch,
        "task_outcome": outcome,
        "runtime_state": state,
        "evidence_refs": list(refs),
        "evidence_verdict": verdict,
    }


def result_for(task, *, outcome="SUCCESS", admission=True, refs=("ACT-1",),
               state="COMPLETED", artifacts=None, plan_id=""):
    result = {
        "response": "2A fixture answer",
        "intent_digest": task.metadata["intent_digest"],
        "task_outcome": outcome,
        "action_result_ledger": {"ACT-1": {"execution_status": "SUCCEEDED"}} if refs else {},
        "outbound_artifacts": list(artifacts or []),
        "plan_id": plan_id,
    }
    if admission:
        result["terminal_acceptance"] = receipt(
            task, outcome=outcome, state=state, refs=refs,
        )
    return result


class FakeAdapter:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.calls = []

    def deliver_event(self, route, event):
        self.calls.append((dict(route), dict(event)))
        return {
            "delivered": self.delivered,
            "reason": "fixture_rejected" if not self.delivered else "",
            "reply": "41" if self.delivered else "",
        }


class FakeTelegramClient:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {"message_id": 1000 + len(self.messages)}

    def send_photo(self, chat_id, file, **kwargs):
        return {"message_id": 2001}

    def send_document(self, chat_id, file, **kwargs):
        return {"message_id": 2002}


class FakeWebTransport:
    def __init__(self):
        self.messages = []

    def deliver_event(self, route, event):
        self.messages.append((route, event))
        return {"delivered": True, "reply": "fixture-WebCopilot"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sa-tri-one-2a-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.queue = RemoteTaskQueue(TaskStateStore(self.root / "tasks.json"))
        self.events = RemoteEventStore(self.root / "events.json")
        self.gateway = AgentIngressGateway(
            task_queue=self.queue,
            session_router=TransportSessionRouter(self.root / "sessions.json"),
            event_store=self.events,
        )
        self.flag = patch.dict(os.environ, {FEATURE_FLAG: "1"})
        self.flag.start()
        self.addCleanup(self.flag.stop)

    def message(self, transport="LOCAL_TEST", source_id="1", text="Read-only fixture question"):
        route = {"transport": transport}
        if transport == "TELEGRAM":
            route.update(chat_id=1, message_id=11)
        if transport == "WEBGPT_COPILOT":
            route.update(conversation_url="https://chatgpt.com/c/fake-isolated-test")
        return NormalizedInboundMessage(
            transport=transport,
            endpoint="test-fixture", conversation_key="2A-FIXTURE",
            sender_id="fake-user", source_message_id=source_id,
            text=text, received_at=1700000000.0,
            idempotency_key=f"2a-fixture-{transport}-{source_id}",
            reply_context=route, metadata={},
        )

    def accept(self, transport="LOCAL_TEST", source_id="1"):
        result = self.gateway.accept(self.message(transport, source_id), workspace=str(self.workspace))
        self.assertTrue(result.created)
        self.assertIsNotNone(result.task)
        return result.task

    def complete(self, task, *, outcome="SUCCESS", admission=True,
                 refs=("ACT-1",), plan_id="", artifacts=None):
        final = result_for(task, outcome=outcome, admission=admission,
                           refs=refs, plan_id=plan_id, artifacts=artifacts)
        complete = self.queue.complete(task.task_id, result=final)
        event = self.events.emit("TASK_COMPLETED", complete, status="COMPLETED", payload={
            "summary": final["response"], "task_outcome": outcome,
            "plan_id": plan_id, "artifacts": list(artifacts or []),
            "workspace": str(self.workspace),
            "terminal_acceptance": final.get("terminal_acceptance", {}),
        })
        return complete, event

    def test_2a_u01_contract_roundtrip(self):
        task = self.accept()
        _, event = self.complete(task)
        result = event.payload["task_result_v1"]
        self.assertEqual(validate_task_result(result)["schema"], "TRI_ONE_TASK_RESULT_V1")
        self.assertEqual(result["task_id"], task.task_id)

    def test_2a_u02_legacy_output_unchanged(self):
        task = self.accept()
        _, event = self.complete(task)
        for key in ("summary", "task_outcome", "workspace", "plan_id", "artifacts", "terminal_acceptance"):
            self.assertIn(key, event.payload)
        self.assertEqual(event.payload["summary"], "2A fixture answer")

    def test_2a_u03_foreign_identity_rejected(self):
        task = self.accept()
        bad = result_for(task)
        bad["terminal_acceptance"]["task_id"] = "TASK-FOREIGN"
        # Actual final in task state is authoritative over model data.
        completed, event = self.complete(task)
        self.assertEqual(event.payload["task_result_v1"]["task_id"], completed.task_id)
        with self.assertRaisesRegex(TaskResultContractError, "foreign_or_contradictory_receipt"):
            make_task_result(
                request_id=task.request_id, task_id=task.task_id,
                task_epoch=task.task_epoch, intent_digest=task.metadata["intent_digest"],
                source_interface="LOCAL_TEST", trusted_reply_route=task.reply_route,
                state="COMPLETED", outcome="SUCCESS",
                terminal_acceptance=bad["terminal_acceptance"], answer_text="fixture",
                evidence_refs=["ACT-1"],
            )

    def test_2a_u04_unknown_never_promoted(self):
        task = self.accept()
        _, event = self.complete(task, outcome="UNKNOWN", admission=False, refs=())
        r = event.payload["task_result_v1"]
        self.assertEqual((r["outcome"], r["terminal_acceptance"]["status"]),
                         ("UNKNOWN", "MISSING"))

    def test_2a_u05_paused_never_success(self):
        task = self.accept()
        task.state = "PAUSED"
        task.result_ledger["paused"] = {"task_outcome": "SUCCESS"}
        event = self.events.emit("TASK_PAUSED", task, status="PAUSED", payload={
            "error": "bounded stop", "task_outcome": "SUCCESS",
            "terminal_acceptance": {},
        })
        r = event.payload["task_result_v1"]
        self.assertEqual((r["task_state"], r["outcome"]), ("PAUSED", "UNKNOWN"))
        self.assertEqual(r["terminal_acceptance"]["status"], "MISSING")

    def test_2a_u06_delivery_independent(self):
        task = self.accept()
        _, event = self.complete(task)
        delivered = DeliveryManager(self.events, {"LOCAL_TEST": FakeAdapter(False)}).deliver(event)
        self.assertFalse(delivered["delivered"])
        self.events.load()
        r = self.events.events[event.event_id].payload["task_result_v1"]
        self.assertEqual(r["outcome"], "SUCCESS")
        self.assertEqual(r["delivery"]["status"], "PENDING")

    def test_2a_u07_duplicate_semantic_event(self):
        message = self.message()
        first = self.gateway.accept(message, workspace=str(self.workspace))
        again = self.gateway.accept(message, workspace=str(self.workspace))
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(first.task.task_id, again.task.task_id)
        task, event = self.complete(first.task)
        other = self.events.emit("TASK_COMPLETED", task, status="COMPLETED", payload={
            "summary": "duplicate terminal fixture"})
        self.assertEqual(event.event_id, other.event_id)
        self.assertEqual(len([e for e in self.events.events.values() if e.event_type == "TASK_COMPLETED"]), 1)

    def test_2a_u08_plan_and_artifact_lineage(self):
        task = self.accept("TELEGRAM")
        artifact = {"path": "one.png", "kind": "photo"}
        _, event = self.complete(task, plan_id="PLAN-2A-123", artifacts=[artifact])
        r = event.payload["task_result_v1"]
        self.assertEqual(r["plan_id"], "PLAN-2A-123")
        self.assertEqual(r["artifacts"], [artifact])
        self.assertEqual(r["source_interface"], "TELEGRAM")
        self.assertNotIn("chat_id", r)

    def test_2a_u09_malformed_result_secrets(self):
        task = self.accept("TELEGRAM")
        _, event = self.complete(task)
        r = event.payload["task_result_v1"]
        self.assertNotIn("chat_id", str(r))
        self.assertNotIn("message_id", str(r))
        with self.assertRaisesRegex(TaskResultContractError, "wrong_schema"):
            validate_task_result({**r, "schema": "invalid"})

    def test_2a_u10_default_off_legacy(self):
        task = self.accept()
        with patch.dict(os.environ, {FEATURE_FLAG: "0"}):
            _, event = self.complete(task)
        self.assertNotIn("task_result_v1", event.payload)
        self.assertEqual(event.payload["summary"], "2A fixture answer")

    def test_2a_i01_gateway_queue_event_audit(self):
        task = self.accept()
        completed, event = self.complete(task)
        self.assertEqual(event.payload["task_result_v1"]["request_id"], completed.request_id)
        self.assertEqual(event.payload["task_result_v1"]["terminal_acceptance"]["status"], "ACCEPTED")
        self.assertEqual(self.events.events[event.event_id].event_type, "TASK_COMPLETED")

    def test_2a_i02_fake_telegram_delivery_dedup(self):
        task = self.accept("TELEGRAM")
        _, event = self.complete(task)
        client = FakeTelegramClient()
        first = DeliveryManager(self.events, {"TELEGRAM": TelegramDeliveryAdapter(client)})
        self.assertTrue(first.deliver(event)["delivered"])
        self.assertFalse(first.deliver(event)["delivered"])
        self.assertEqual(len(client.messages), 1)
        self.events.load()
        receipt = self.events.events[event.event_id].payload["delivery_receipt"]
        self.assertEqual(receipt["receipt_kind"], "RUNTIME_ADAPTER_ACK")

    def test_2a_i03_webcopilot_render_and_delivery(self):
        task = self.accept("WEBGPT_COPILOT")
        _, event = self.complete(task)
        transport = FakeWebTransport()
        adapter = WebGPTDeliveryAdapter(transport)
        self.assertTrue(DeliveryManager(self.events, {"WEBGPT_COPILOT": adapter}).deliver(event)["delivered"])
        self.assertEqual(len(transport.messages), 1)
        self.assertIn("2A fixture answer", transport.messages[0][1]["rendered_text"])
        self.assertEqual(event.payload["task_result_v1"]["source_interface"], "WEB_COPILOT")

    def test_2a_i04_paused_no_terminal_event(self):
        task = self.accept()
        task.state = "PAUSED"
        task.result_ledger["paused"] = {"task_outcome": "UNKNOWN"}
        event = self.events.emit("TASK_PAUSED", task, status="PAUSED", payload={
            "summary": "Pending verification; task paused",
            "task_outcome": "UNKNOWN",
        })
        self.assertIn("TASK_PAUSED", EVENT_TYPES)
        self.assertEqual(event.payload["task_result_v1"]["task_state"], "PAUSED")
        self.assertNotIn("TASK_COMPLETED", {e.event_type for e in self.events.events.values()})
        self.assertIn("PAUSED", TelegramDeliveryAdapter.render(DeliveryManager._wire(event)))

    def test_2a_i05_restart_delivery_idempotency(self):
        task = self.accept()
        self.complete(task)
        reopened = RemoteEventStore(self.root / "events.json")
        event = next(e for e in reopened.ready() if e.event_type == "TASK_COMPLETED")
        adapter = FakeAdapter(True)
        manager = DeliveryManager(reopened, {"LOCAL_TEST": adapter})
        self.assertTrue(manager.deliver(event)["delivered"])
        self.assertFalse(manager.deliver(event)["delivered"])
        self.assertEqual(len(adapter.calls), 1)
        reopened.load()
        self.assertEqual(reopened.events[event.event_id].delivery_state, "DELIVERED")

    def test_2a_i06_plan_only_legacy_render(self):
        task = self.accept("TELEGRAM")
        plan = prepare_telegram_completion(
            request="只規劃，不執行",
            summary="Plan ready", workspace=self.workspace,
            artifacts=[], expect_artifact=False,
        )
        _, event = self.complete(task, outcome="UNKNOWN", admission=False,
                                 refs=(), plan_id="PLAN-FIXTURE-2A")
        self.assertEqual(plan["summary"], "Plan ready")
        self.assertIn("PLAN-FIXTURE-2A", TelegramDeliveryAdapter.render(
            DeliveryManager._wire(event)))
        self.assertEqual(event.payload["task_result_v1"]["plan_id"], "PLAN-FIXTURE-2A")

    def test_2a_i07_receipt_correlation_and_rejection(self):
        task = self.accept()
        completed, event = self.complete(task)
        result = event.payload["task_result_v1"]
        receipt_id = completed.result_ledger["final"]["terminal_acceptance"]["receipt_id"]
        self.assertEqual(result["terminal_acceptance"]["receipt_id"], receipt_id)
        forged = dict(result)
        forged["terminal_acceptance"] = dict(result["terminal_acceptance"], status="MISSING")
        with self.assertRaisesRegex(TaskResultContractError, "missing_receipt_has_claims"):
            validate_task_result(forged)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Fixture)
    runner = unittest.TextTestRunner(verbosity=2)
    outcome = runner.run(suite)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
