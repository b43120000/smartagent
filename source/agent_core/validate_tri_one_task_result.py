"""Isolated UT/IT smoke for the opt-in Tri-One 2A TaskResult contract.

Run from the repository root:
    python source/agent_core/validate_tri_one_task_result.py

All persistent event/outbox data is created under a temporary directory.
No browser, Telegram, network, production workspace, or user task is touched.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.remote_events import DeliveryManager, RemoteEventStore
from agent_core.tri_one_task_result import (
    FEATURE_FLAG,
    TaskResultContractError,
    make_task_result,
    make_task_result_from_remote_event,
    make_task_result_from_webagent_loop,
    validate_task_result,
)


def _receipt(*, request_id="RR-TRI-2A-UT", task_id="TASK-TRI-2A-UT",
             task_epoch="epoch-tri-2a", outcome="SUCCESS", state="COMPLETED",
             refs=None, verdict="PASS", receipt_id="TERM-TRI-2A-UT"):
    return {
        "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
        "admission_status": "ACCEPTED",
        "receipt_id": receipt_id,
        "request_id": request_id,
        "task_id": task_id,
        "task_epoch": task_epoch,
        "task_outcome": outcome,
        "runtime_state": state,
        "evidence_refs": list(refs if refs is not None else ["ACT-1"]),
        "evidence_verdict": verdict,
        "verification_status": verdict,
    }


def _task(*, outcome="SUCCESS", receipt=True, action_ledger=True):
    route = {
        "transport": "LOCAL_TEST",
        "chat_id": "123456789",
        "outbox_path": "C:/private/should-not-be-copied.jsonl",
    }
    final = {
        "response": "Verified fixture response",
        "task_outcome": outcome,
        "intent_digest": "a" * 64,
        "action_result_ledger": {"ACT-1": {"execution_status": "SUCCEEDED"}} if action_ledger else {},
        "outbound_artifacts": [],
    }
    if receipt:
        final["terminal_acceptance"] = _receipt(outcome=outcome)
    return SimpleNamespace(
        task_id="TASK-TRI-2A-UT",
        request_id="RR-TRI-2A-UT",
        task_epoch="epoch-tri-2a",
        state="COMPLETED",
        completed_at=1700000000.0,
        reply_route=route,
        metadata={
            "transport": "LOCAL_TEST",
            "intent_digest": "a" * 64,
            "reply_route": route,
        },
        result_ledger={"final": final},
        action_ledger={"ACT-1": True} if action_ledger else {},
    )


def _make_result(*, outcome="SUCCESS", receipt=None, state="COMPLETED",
                 route=None, kind=None):
    request_id = "RR-TRI-2A-UT"
    task_id = "TASK-TRI-2A-UT"
    task_epoch = "epoch-tri-2a"
    terminal = receipt if receipt is not None else _receipt(
        request_id=request_id, task_id=task_id, task_epoch=task_epoch,
        outcome=outcome, state=state,
    )
    return make_task_result(
        request_id=request_id,
        task_id=task_id,
        task_epoch=task_epoch,
        intent_digest="a" * 64,
        source_interface="LOCAL_TEST",
        trusted_reply_route=route or {
            "transport": "LOCAL_TEST",
            "chat_id": "123456789",
            "outbox_path": "C:/private/should-not-be-copied.jsonl",
        },
        state=state,
        outcome=outcome,
        terminal_acceptance=terminal,
        answer_text="Verified fixture response",
        evidence_refs=list(terminal.get("evidence_refs", [])),
        operational_action_count=1,
        operational_action_count_basis="RESULT_LEDGER",
        artifacts=[],
        created_at="2026-10-27T00:00:00.000Z",
        completion_kind=kind,
    )


class _Adapter:
    def __init__(self, delivered):
        self.delivered = delivered
        self.calls = []

    def deliver_event(self, route, event):
        self.calls.append((dict(route), dict(event)))
        return {"delivered": self.delivered, "reason": "" if self.delivered else "fixture_delivery_failed"}


class TaskResultContractTests(unittest.TestCase):
    def test_success_requires_accepted_passing_evidence(self):
        with self.assertRaisesRegex(TaskResultContractError, "success_without_terminal_receipt"):
            _make_result(outcome="SUCCESS", receipt={})
        receipt = _receipt(verdict="UNVERIFIED")
        with self.assertRaisesRegex(TaskResultContractError, "success_without_passing_evidence"):
            _make_result(outcome="SUCCESS", receipt=receipt)

    def test_foreign_receipt_identity_is_rejected(self):
        receipt = _receipt(task_id="TASK-FOREIGN")
        with self.assertRaisesRegex(TaskResultContractError, "foreign_or_contradictory_receipt"):
            _make_result(receipt=receipt)

    def test_answer_only_is_not_enabled_in_2a(self):
        with self.assertRaisesRegex(TaskResultContractError, "answer_only_not_implemented_in_2a"):
            _make_result(kind="ANSWER_ONLY")

    def test_failed_terminal_can_be_evidence_backed_without_claiming_success(self):
        receipt = _receipt(outcome="FAILED", verdict="FAIL")
        result = _make_result(outcome="FAILED", receipt=receipt)
        self.assertEqual(result["completion_kind"], "EVIDENCE_BACKED")
        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(result["terminal_acceptance"]["evidence_verdict"], "FAIL")
        self.assertEqual(validate_task_result(result)["result_id"], result["result_id"])

    def test_route_secrets_are_not_copied_to_task_result(self):
        result = _make_result()
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("123456789", serialized)
        self.assertNotIn("should-not-be-copied", serialized)
        self.assertNotIn("chat_id", serialized)
        self.assertNotIn("outbox_path", serialized)

    def test_result_id_is_stable_for_same_terminal_identity(self):
        one = _make_result()
        two = _make_result()
        self.assertEqual(one["result_id"], two["result_id"])

    def test_validator_rejects_fabricated_delivery_receipt(self):
        result = _make_result()
        result["delivery"]["status"] = "DELIVERED"
        result["delivery"]["delivery_receipt_id"] = None
        with self.assertRaisesRegex(TaskResultContractError, "delivery_receipt_missing"):
            validate_task_result(result)

    def test_remote_event_feature_flag_off_preserves_legacy_payload(self):
        task = _task()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {FEATURE_FLAG: "0"}):
            store = RemoteEventStore(Path(td) / "events.json")
            event = store.emit(
                "TASK_COMPLETED", task, status="COMPLETED",
                payload={"summary": "Verified fixture response", "task_outcome": "SUCCESS"},
            )
            self.assertNotIn("task_result_v1", event.payload)
            self.assertNotIn("task_result_contract_status", event.payload)
            self.assertEqual(event.payload["summary"], "Verified fixture response")

    def test_remote_event_persists_result_and_delivery_receipt(self):
        task = _task()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {FEATURE_FLAG: "1"}):
            store = RemoteEventStore(Path(td) / "events.json")
            event = store.emit(
                "TASK_COMPLETED", task, status="COMPLETED",
                payload={
                    "summary": "Verified fixture response",
                    "task_outcome": "SUCCESS",
                    "terminal_acceptance": task.result_ledger["final"]["terminal_acceptance"],
                },
            )
            self.assertIn("task_result_v1", event.payload)
            initial = event.payload["task_result_v1"]
            self.assertEqual(initial["completion_kind"], "EVIDENCE_BACKED")
            self.assertEqual(initial["delivery"]["status"], "PENDING")
            self.assertEqual(initial["source_commit"], "NOT_ATTESTED")
            adapter = _Adapter(True)
            result = DeliveryManager(store, {"LOCAL_TEST": adapter}).deliver(event)
            self.assertTrue(result["delivered"])
            store.load()
            persisted = store.events[event.event_id]
            task_result = persisted.payload["task_result_v1"]
            self.assertEqual(persisted.delivery_state, "DELIVERED")
            self.assertEqual(task_result["delivery"]["status"], "DELIVERED")
            self.assertEqual(task_result["delivery"]["delivery_receipt_id"], event.event_id)
            self.assertEqual(task_result["delivery"]["attempt_count"], 1)
            self.assertEqual(task_result["outcome"], "SUCCESS")
            self.assertEqual(task_result["task_state"], "COMPLETED")

    def test_failed_delivery_does_not_rewrite_task_outcome(self):
        task = _task()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {FEATURE_FLAG: "1"}):
            store = RemoteEventStore(Path(td) / "events.json")
            event = store.emit(
                "TASK_COMPLETED", task, status="COMPLETED",
                payload={
                    "summary": "Verified fixture response",
                    "task_outcome": "SUCCESS",
                    "terminal_acceptance": task.result_ledger["final"]["terminal_acceptance"],
                },
            )
            result = DeliveryManager(store, {"LOCAL_TEST": _Adapter(False)}).deliver(event)
            self.assertFalse(result["delivered"])
            store.load()
            persisted = store.events[event.event_id]
            self.assertEqual(persisted.payload["task_result_v1"]["outcome"], "SUCCESS")
            self.assertEqual(persisted.payload["task_result_v1"]["delivery"]["status"], "PENDING")
            self.assertEqual(persisted.payload["task_result_v1"]["delivery"]["attempt_count"], 1)
            self.assertIsNone(persisted.payload["task_result_v1"]["delivery"]["delivery_receipt_id"])

    def test_invalid_result_metadata_does_not_break_legacy_event(self):
        task = _task(receipt=False)
        task.result_ledger["final"]["task_outcome"] = "SUCCESS"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {FEATURE_FLAG: "1"}):
            store = RemoteEventStore(Path(td) / "events.json")
            event = store.emit(
                "TASK_COMPLETED", task, status="COMPLETED",
                payload={"summary": "legacy event still delivered", "task_outcome": "SUCCESS"},
            )
            self.assertEqual(event.payload["summary"], "legacy event still delivered")
            self.assertNotIn("task_result_v1", event.payload)
            self.assertEqual(event.payload["task_result_contract_status"], "ERROR")
            self.assertEqual(event.payload["task_result_contract_error"], "success_without_terminal_receipt")

    def test_direct_webagent_uses_same_contract(self):
        receipt = _receipt(
            request_id="RR-DIRECT-UT", task_id="TASK-DIRECT-UT",
            task_epoch="direct-epoch", outcome="SUCCESS",
        )
        loop = SimpleNamespace(
            run_id="RR-DIRECT-UT",
            task_id="TASK-DIRECT-UT",
            task_epoch="direct-epoch",
            intent_digest="b" * 64,
            terminal_outcome="SUCCESS",
            terminal_acceptance=receipt,
            action_result_ledger={"ACT-1": {"execution_status": "SUCCEEDED"}},
        )
        result = make_task_result_from_webagent_loop(
            loop, "Direct response", source_interface="WEB_DIRECT",
        )
        self.assertEqual(result["source_interface"], "WEB_DIRECT")
        self.assertEqual(result["completion_kind"], "EVIDENCE_BACKED")
        self.assertEqual(result["delivery"]["status"], "PENDING")
        self.assertEqual(validate_task_result(result)["task_id"], "TASK-DIRECT-UT")

    def test_remote_event_factory_preserves_unknown_without_success_promotion(self):
        task = _task(outcome="UNKNOWN", receipt=False, action_ledger=False)
        result = make_task_result_from_remote_event(
            task, "TASK_COMPLETED", {"summary": "Unknown outcome", "task_outcome": "UNKNOWN"},
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["completion_kind"], "LEGACY_UNVERIFIED")
        self.assertEqual(result["terminal_acceptance"]["status"], "MISSING")
        self.assertEqual(result["operational_action_count_basis"], "RESULT_LEDGER")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TaskResultContractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
