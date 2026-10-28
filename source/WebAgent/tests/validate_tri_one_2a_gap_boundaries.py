"""PR10/2A focused offline coverage expansion.

Read original verification_matrix.md section 3 at planning SHA
4a64dc89df79a40422783b458641f522f2ef61cf before interpreting results.
This test-only file does not attest real Worker identity, real transport ingress,
a production Writer, or a full operating-system fault/recovery cycle.

Run with PYTHONPATH=source from the approved isolated source checkout:
    python -B source/WebAgent/tests/validate_tri_one_2a_gap_boundaries.py

Every fixture uses TemporaryDirectory. A child process is intentionally ended
abruptly with os._exit(71) after isolated queue completion and event persistence.
No production path, listener, browser, real Telegram or external dispatch.
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.remote_events import DeliveryManager, RemoteEventStore
from agent_core.tri_one_task_result import (
    TaskResultContractError,
    make_task_result,
    make_task_result_from_remote_event,
    validate_task_result,
)
from RemoteAgent.webgpt_delivery import WebGPTDeliveryAdapter
from validate_tri_one_2a_matrix import Fixture, FakeAdapter
from validate_tri_one_2a_task_result import build, case_receipt


# The child has no access to any production queue or workspace. The parent
# passes one new TemporaryDirectory via an isolated process environment.
_CRASH_AFTER_PERSIST_CHILD = r"""
import os
from pathlib import Path
from agent_core.agent_gateway import AgentIngressGateway
from agent_core.remote_events import RemoteEventStore
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.transport_message import NormalizedInboundMessage
from agent_core.transport_sessions import TransportSessionRouter

root = Path(os.environ["SA_2A_GAP_TEMP_ROOT"]).resolve()
workspace = root / "workspace"
workspace.mkdir(exist_ok=True)
queue = RemoteTaskQueue(TaskStateStore(root / "tasks.json"))
events = RemoteEventStore(root / "events.json")
gateway = AgentIngressGateway(
    task_queue=queue,
    session_router=TransportSessionRouter(root / "sessions.json"),
    event_store=events,
)
message = NormalizedInboundMessage(
    transport="LOCAL_TEST", endpoint="gap-child-fixture",
    conversation_key="GAP-CRASH", sender_id="fixture",
    source_message_id="1", text="static isolated fixture",
    received_at=1700000000.0, idempotency_key="gap-crash-one",
    reply_context={"transport": "LOCAL_TEST"}, metadata={},
)
task = gateway.accept(message, workspace=str(workspace)).task
receipt = {
    "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
    "admission_status": "ACCEPTED", "receipt_id": "TERM-GAP-CHILD",
    "request_id": task.request_id, "task_id": task.task_id,
    "task_epoch": task.task_epoch, "task_outcome": "SUCCESS",
    "runtime_state": "COMPLETED", "evidence_refs": ["ACT-1"],
    "evidence_verdict": "PASS",
}
result = {
    "response": "isolated child fixture",
    "intent_digest": task.metadata["intent_digest"],
    "task_outcome": "SUCCESS",
    "action_result_ledger": {"ACT-1": {"execution_status": "SUCCEEDED"}},
    "terminal_acceptance": receipt,
}
completed = queue.complete(task.task_id, result=result)
event = events.emit("TASK_COMPLETED", completed, status="COMPLETED", payload={
    "summary": result["response"], "task_outcome": "SUCCESS",
    "terminal_acceptance": receipt, "workspace": str(workspace),
})
assert event.event_type == "TASK_COMPLETED"
os._exit(71)
"""


class FocusedGaps(unittest.TestCase):
    def setUp(self):
        # Reuse deterministic legacy fixture helpers WITHOUT inheriting
        # their 17 test methods or rerunning prior 17/17 PASS.
        self.fixture = Fixture("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_gap_u03_foreign_request_task_epoch_and_intent(self):
        for field in ("request_id", "task_id", "task_epoch"):
            with self.subTest(field=field):
                params = {field: "FOREIGN-" + field}
                with self.assertRaisesRegex(
                    TaskResultContractError, "foreign_or_contradictory_receipt"
                ):
                    build(**params, terminal_acceptance=case_receipt())
        task = SimpleNamespace(
            request_id="RR-A", task_id="TASK-A", task_epoch="EPOCH-A",
            state="FAILED", metadata={
                "intent_digest": "ORIGINAL", "reply_route": {"transport": "LOCAL_TEST"}
            },
            reply_route={"transport": "LOCAL_TEST"},
            result_ledger={"final": {"intent_digest": "FOREIGN"}},
        )
        with self.assertRaisesRegex(TaskResultContractError, "intent_digest_mismatch"):
            make_task_result_from_remote_event(task, "TASK_FAILED", {})

    def test_gap_u04_failed_partial_and_contradictory_success(self):
        for outcome in ("FAILED", "PARTIAL", "UNKNOWN"):
            with self.subTest(outcome=outcome):
                result = build(
                    outcome=outcome, evidence_refs=["ACT-1"],
                    terminal_acceptance=case_receipt(refs=["ACT-1"], outcome=outcome),
                )
                self.assertEqual(result["outcome"], outcome)
                self.assertNotEqual(result["outcome"], "SUCCESS")
        bad = case_receipt(refs=["ACT-1"], outcome="SUCCESS")
        bad["evidence_verdict"] = "FAIL"
        with self.assertRaisesRegex(TaskResultContractError, "success_without_passing_evidence"):
            build(outcome="SUCCESS", evidence_refs=["ACT-1"], terminal_acceptance=bad)

    def test_gap_u05_interrupted_never_success(self):
        with self.assertRaisesRegex(TaskResultContractError, "noncompleted_success"):
            build(state="INTERRUPTED", outcome="SUCCESS")
        interrupted = build(state="INTERRUPTED", outcome="UNKNOWN")
        self.assertEqual(interrupted["task_state"], "INTERRUPTED")
        self.assertEqual(interrupted["terminal_acceptance"]["status"], "MISSING")
        # OPEN Writer belongs to the separate real writer ledger; NOT ATTESTED here.

    def test_gap_u06_delivery_statuses_cannot_promote_outcome(self):
        base = build()
        for status in ("PENDING", "FAILED", "UNKNOWN"):
            with self.subTest(status=status):
                item = copy.deepcopy(base)
                item["delivery"] = {
                    "status": status, "attempt_count": 2,
                    "delivery_receipt_id": None,
                }
                result = validate_task_result(item)
                self.assertEqual(result["outcome"], "UNKNOWN")
        delivered = copy.deepcopy(base)
        delivered["delivery"] = {
            "status": "DELIVERED", "attempt_count": 1,
            "delivery_receipt_id": "DREC-GAP-SYNTHETIC",
        }
        self.assertEqual(validate_task_result(delivered)["outcome"], "UNKNOWN")
        delivered["delivery"]["delivery_receipt_id"] = None
        with self.assertRaisesRegex(TaskResultContractError, "delivery_receipt_missing"):
            validate_task_result(delivered)
        # Production EventStore READY maps to result PENDING, not a new QUEUED result state.

    def test_gap_u07_retry_then_success_exactly_one_completion(self):
        task = self.fixture.accept()
        _, event = self.fixture.complete(task)
        class FailOnce(FakeAdapter):
            def deliver_event(self, route, wire):
                self.calls.append((dict(route), dict(wire)))
                return {
                    "delivered": len(self.calls) >= 2,
                    "reason": "synthetic_transient" if len(self.calls) == 1 else "",
                    "reply": "fixture-ack" if len(self.calls) == 2 else "",
                }
        adapter = FailOnce()
        manager = DeliveryManager(self.fixture.events, {"LOCAL_TEST": adapter})
        self.assertFalse(manager.deliver(event)["delivered"])
        self.assertTrue(manager.deliver(event)["delivered"])
        self.assertFalse(manager.deliver(event)["delivered"])
        self.fixture.events.load()
        stored = self.fixture.events.events[event.event_id]
        self.assertEqual(stored.attempts, 2)
        self.assertEqual(stored.payload["task_result_v1"]["delivery"]["status"], "DELIVERED")
        self.assertEqual(stored.payload["task_result_v1"]["outcome"], "SUCCESS")
        self.assertEqual(len([
            row for row in self.fixture.events.events.values()
            if row.event_type == "TASK_COMPLETED"
        ]), 1)

    def test_gap_u08_multiroute_and_u09_secret_route_exclusion(self):
        routes = [
            ("TELEGRAM", {"transport": "TELEGRAM", "chat_id": "fixture-secret-telegram"}),
            ("LOCAL_TEST", {"transport": "LOCAL_TEST", "local_secret": "fixture-secret-local"}),
            ("WEBGPT_COPILOT", {
                "transport": "WEBGPT_COPILOT", "conversation_url": "fixture-secret-web"
            }),
        ]
        ids = []
        for origin, route in routes:
            with self.subTest(origin=origin):
                result = build(source_interface=origin, trusted_reply_route=route)
                ids.append(result["reply_route_id"])
                self.assertNotIn("fixture-secret", repr(result))
                self.assertTrue(result["reply_route_id"].startswith("ROUTE-"))
        self.assertEqual(len(set(ids)), 3)
        with self.assertRaisesRegex(TaskResultContractError, "route_origin_mismatch"):
            build(source_interface="LOCAL_TEST", trusted_reply_route={"transport": "TELEGRAM"})
        with self.assertRaisesRegex(TaskResultContractError, "wrong_schema"):
            validate_task_result({**build(), "schema": "MALFORMED"})

    def test_gap_i03_webcopilot_retry_after_fake_transport_failure(self):
        task = self.fixture.accept("WEBGPT_COPILOT")
        _, event = self.fixture.complete(task)
        class FailOnceWeb:
            def __init__(self):
                self.calls = []
            def deliver_event(self, route, wire):
                self.calls.append((dict(route), dict(wire)))
                return {"delivered": len(self.calls) >= 2,
                        "reason": "synthetic_retry",
                        "reply": "fixture-web" if len(self.calls) == 2 else ""}
        transport = FailOnceWeb()
        manager = DeliveryManager(self.fixture.events, {
            "WEBGPT_COPILOT": WebGPTDeliveryAdapter(transport),
        })
        self.assertFalse(manager.deliver(event)["delivered"])
        self.assertTrue(manager.deliver(event)["delivered"])
        self.assertFalse(manager.deliver(event)["delivered"])
        self.fixture.events.load()
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(self.fixture.events.events[event.event_id].delivery_state, "DELIVERED")
        self.assertEqual(self.fixture.events.events[event.event_id].attempts, 2)

    def test_gap_i07_progress_then_terminal_receipt_identity(self):
        task = self.fixture.accept()
        progress = self.fixture.events.emit(
            "TASK_PROGRESS", task, status="RUNNING",
            payload={"summary": "fixture working"},
        )
        completed, terminal = self.fixture.complete(task)
        self.assertEqual(progress.request_id, terminal.request_id)
        self.assertEqual(progress.task_id, terminal.task_id)
        self.assertEqual(terminal.payload["task_result_v1"]["task_epoch"], completed.task_epoch)
        self.assertEqual(
            terminal.payload["task_result_v1"]["terminal_acceptance"]["status"], "ACCEPTED"
        )
        # A real Planner progress/receipt transition is NOT represented by this event fixture.

    def test_gap_i05_abrupt_process_exit_then_durable_delivery(self):
        root = self.fixture.root / "abrupt-process"
        root.mkdir()
        env = dict(os.environ)
        env["SA_2A_GAP_TEMP_ROOT"] = str(root)
        env["TRI_ONE_TASK_RESULT_V1"] = "1"
        source = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = str(source) + os.pathsep + str(Path(__file__).parent)
        child = subprocess.run(
            [sys.executable, "-B", "-c", _CRASH_AFTER_PERSIST_CHILD],
            cwd=str(Path(__file__).resolve().parents[3]),
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(child.returncode, 71, "isolated child did not exit at crash boundary")
        store = RemoteEventStore(root / "events.json")
        ready = [item for item in store.ready() if item.event_type == "TASK_COMPLETED"]
        self.assertEqual(len(ready), 1)
        adapter = FakeAdapter(True)
        manager = DeliveryManager(store, {"LOCAL_TEST": adapter})
        self.assertTrue(manager.deliver(ready[0])["delivered"])
        self.assertFalse(manager.deliver(ready[0])["delivered"])
        reopened = RemoteEventStore(root / "events.json")
        item = reopened.events[ready[0].event_id]
        self.assertEqual(item.delivery_state, "DELIVERED")
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(item.payload["task_result_v1"]["outcome"], "SUCCESS")
        # This tests process os._exit after persistence, NOT a real crash inside
        # the delivery in-flight uncertainty interval or a loaded production Worker.


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FocusedGaps)
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
