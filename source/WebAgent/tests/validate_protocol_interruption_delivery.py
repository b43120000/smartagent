#!/usr/bin/env python3
"""Protocol exhaustion remains distinct from tool/task failure."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from RemoteAgent.telegram_webagent_worker import _protocol_interruption, _transport_interruption
from WebAgent.protocol_loop import ProtocolLoopInterrupted
from agent_core.task_state import RemoteTaskQueue, TaskStateStore, TASK_INTERRUPTED
from agent_core.web_runtime import WebScraperStageError


def run() -> None:
    assert _protocol_interruption(ProtocolLoopInterrupted("RR-1", 2, [], []))
    assert _protocol_interruption(WebScraperStageError("bad", stage="protocol_ack_recovery_failed", safe_to_retry=False))
    assert not _protocol_interruption(WebScraperStageError("ambiguous", stage="send_timeout", safe_to_retry=False))
    assert _transport_interruption(WebScraperStageError("ambiguous", stage="send_timeout", safe_to_retry=False))
    assert not _protocol_interruption(RuntimeError("tool failed"))
    with tempfile.TemporaryDirectory(prefix="protocol-interrupt-") as temp:
        queue = RemoteTaskQueue(TaskStateStore(Path(temp) / "tasks.json"))
        task, created = queue.enqueue_remote_request({
            "protocol": "remote_agent", "protocol_version": 1,
            "request_id": "RR-INTERRUPT", "workspace": temp,
            "conversation_url": "https://chatgpt.com/c/interrupt",
            "request": "Test interruption",
        }, origin_turn_fingerprint="TURN-INTERRUPT")
        assert created
        assert queue.claim_next() is not None
        interrupted = queue.interrupt_running(task.task_id, "PROTOCOL_INTERRUPTED")
        assert interrupted is not None and interrupted.state == TASK_INTERRUPTED
        assert interrupted.error == "PROTOCOL_INTERRUPTED"
        assert queue.fail_running(task.task_id, "late failure") is None
        reloaded = RemoteTaskQueue(TaskStateStore(Path(temp) / "tasks.json"))
        assert reloaded.store.get(task.task_id).state == TASK_INTERRUPTED

        other, created = queue.enqueue_remote_request({
            "protocol": "remote_agent", "protocol_version": 1,
            "request_id": "RR-RETRY", "workspace": temp,
            "conversation_url": "https://chatgpt.com/c/retry",
            "request": "Test explicit retry",
        }, origin_turn_fingerprint="TURN-RETRY")
        assert created and queue.claim_next() is not None
        queue.save_protocol_checkpoint(
            other.task_id,
            {"request_id": other.request_id, "task_id": other.task_id, "task_epoch": other.task_epoch},
            {"logical_round_state": "PROTOCOL_INTERRUPTED", "repair_attempts": 1},
        )
        assert queue.interrupt_running(other.task_id, "format exhausted") is not None
        assert queue.retry_task(task_id=other.task_id).state == "QUEUED"
        assert queue.load_action_state(other.task_id)["checkpoint"] == {}


if __name__ == "__main__":
    run()
    print("WEBAGENT_PROTOCOL_INTERRUPTION_DELIVERY_OK")
