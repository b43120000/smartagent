#!/usr/bin/env python3
"""Isolated queue/protocol-interruption persistence and explicit retry regression.

This version checks existing public RemoteTaskQueue APIs. The older test
referenced removed Worker-private helper functions and deleted checkpoint APIs,
so those assertions were never exercising the current implementation.
This does NOT verify live Worker exception classification or browser transport.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from agent_core.task_state import (
    RemoteTaskQueue, TaskStateStore, TASK_INTERRUPTED,
)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="protocol-interrupt-") as temp:
        path = Path(temp) / "tasks.json"
        queue = RemoteTaskQueue(TaskStateStore(path))
        task, created = queue.enqueue_remote_request({
            "protocol": "remote_agent", "protocol_version": 1,
            "request_id": "RR-INTERRUPT", "workspace": temp,
            "conversation_url": "https://chatgpt.com/c/isolated-interrupt",
            "request": "Test isolated interruption",
        }, origin_turn_fingerprint="TURN-INTERRUPT")
        assert created
        assert queue.claim_next() is not None
        snapshot = {
            "task_outcome": "UNKNOWN",
            "action_result_ledger": {"ACT-FIXTURE": {"execution_status": "SUCCEEDED"}},
            "terminal_acceptance": {},
        }
        interrupted = queue.interrupt_running(
            task.task_id, "PROTOCOL_INTERRUPTED", result=snapshot,
        )
        assert interrupted is not None and interrupted.state == TASK_INTERRUPTED
        assert interrupted.error == "PROTOCOL_INTERRUPTED"
        assert interrupted.result_ledger["interrupted"] == snapshot
        assert queue.fail_running(task.task_id, "late failure") is None
        reloaded = RemoteTaskQueue(TaskStateStore(path))
        stored = reloaded.store.get(task.task_id)
        assert stored is not None and stored.state == TASK_INTERRUPTED
        assert stored.result_ledger["interrupted"] == snapshot

        other, created = queue.enqueue_remote_request({
            "protocol": "remote_agent", "protocol_version": 1,
            "request_id": "RR-RETRY", "workspace": temp,
            "conversation_url": "https://chatgpt.com/c/isolated-retry",
            "request": "Test explicit retry",
        }, origin_turn_fingerprint="TURN-RETRY")
        assert created and queue.claim_next() is not None
        assert queue.interrupt_running(
            other.task_id, "format exhausted",
            result={"task_outcome": "UNKNOWN", "terminal_acceptance": {}},
        ) is not None
        retry = queue.retry_task(task_id=other.task_id)
        assert retry.state == "QUEUED"
        reloaded = RemoteTaskQueue(TaskStateStore(path))
        assert reloaded.store.get(other.task_id).state == "QUEUED"
        assert reloaded.store.get(task.task_id).state == TASK_INTERRUPTED


if __name__ == "__main__":
    run()
    print("WEBAGENT_PROTOCOL_INTERRUPTION_DELIVERY_OK")
