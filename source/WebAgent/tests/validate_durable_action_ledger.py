#!/usr/bin/env python3
"""Focused persistence checks for the durable V8 action intent ledger."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.task_state import RemoteTaskQueue, TaskStateError, TaskStateStore


IDENTITY = {"task_epoch": "EPOCH-1", "intent_digest": "INTENT-1"}
ACTION = {"action_id": "A-1", "digest": "DIGEST-1", "tool": "read_file"}


def _queue(path: Path, *, claim: bool = True) -> tuple[RemoteTaskQueue, str]:
    queue = RemoteTaskQueue(TaskStateStore(path))
    task, created = queue.enqueue_remote_request({
        "protocol": "remote_agent", "protocol_version": 8,
        "request_id": "RR-DURABLE", "workspace": str(path.parent),
        "conversation_url": "https://chatgpt.com/c/durable", "request": "durable action test",
    }, origin_turn_fingerprint="TURN-DURABLE")
    assert created
    if claim:
        claimed = queue.claim_next()
        assert claimed is not None and claimed.task_id == task.task_id
    return queue, task.task_id


def _raises(callable_) -> None:
    try:
        callable_()
    except TaskStateError:
        return
    raise AssertionError("expected TaskStateError")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="durable-action-state-") as temp:
        queue, task_id = _queue(Path(temp) / "tasks.json", claim=False)
        _raises(lambda: queue.prepare_action_batch(
            task_id, IDENTITY, "TURN-QUEUED", [ACTION], {"phase": "TOOLS_ACCEPTED"},
        ))

    with tempfile.TemporaryDirectory(prefix="durable-action-ledger-") as temp:
        path = Path(temp) / "tasks.json"
        queue, task_id = _queue(path)

        checkpoint = {"phase": "TOOLS_ACCEPTED", "round": 1, "ack_id": "ACK-1"}
        prepared = queue.prepare_action_batch(task_id, IDENTITY, "TURN-1", [ACTION], checkpoint)
        assert prepared["checkpoint"] == checkpoint
        assert prepared["actions"]["A-1"]["state"] == "PREPARED"
        assert prepared["actions"]["A-1"]["digest"] == "DIGEST-1"
        executing = queue.mark_action_executing(task_id, "A-1")
        assert executing["state"] == "EXECUTING"
        committed = queue.commit_action_result(
            task_id, "A-1", "DIGEST-1", {"text": "prepared"}, {"status": "COMMITTED"},
        )
        assert committed["state"] == "COMMITTED"
        assert committed["prepared_result"] == {"text": "prepared"}

        reloaded = RemoteTaskQueue(TaskStateStore(path)).load_action_state(task_id)
        assert reloaded["identity"] == IDENTITY
        assert reloaded["checkpoint"] == checkpoint
        assert reloaded["actions"]["A-1"] == committed

        _raises(lambda: queue.prepare_action_batch(
            task_id, IDENTITY, "TURN-1", [{**ACTION, "digest": "OTHER"}], checkpoint,
        ))
        _raises(lambda: queue.commit_action_result(task_id, "A-1", "OTHER", {}, {}))

    with tempfile.TemporaryDirectory(prefix="durable-action-uncertain-") as temp:
        path = Path(temp) / "tasks.json"
        queue, task_id = _queue(path)
        queue.prepare_action_batch(task_id, IDENTITY, "TURN-2", [ACTION], {"phase": "TOOLS_ACCEPTED"})
        queue.mark_action_executing(task_id, "A-1")
        # Restart preserves the execution boundary.  It must not manufacture a
        # result or silently turn the action back into PREPARED.
        uncertain = RemoteTaskQueue(TaskStateStore(path)).load_action_state(task_id)
        assert uncertain["actions"]["A-1"]["state"] == "EXECUTING"
        assert "prepared_result" not in uncertain["actions"]["A-1"]
        _raises(lambda: RemoteTaskQueue(TaskStateStore(path)).mark_action_executing(task_id, "A-1"))
        _raises(lambda: RemoteTaskQueue(TaskStateStore(path)).prepare_action_batch(
            task_id, IDENTITY, "TURN-OTHER", [ACTION], {"phase": "TOOLS_ACCEPTED"},
        ))

    with tempfile.TemporaryDirectory(prefix="durable-action-checkpoint-") as temp:
        path = Path(temp) / "tasks.json"
        queue, task_id = _queue(path)
        final = queue.prepare_action_batch(task_id, IDENTITY, "TURN-FINAL", [], {"phase": "FINAL_ACCEPTED"})
        assert final["actions"] == {}
        checkpoint = queue.save_protocol_checkpoint(task_id, IDENTITY, {"phase": "RESULTS_READY", "final": "ok"})
        assert checkpoint["checkpoint"] == {"phase": "RESULTS_READY", "final": "ok"}

    with tempfile.TemporaryDirectory(prefix="durable-action-legacy-") as temp:
        path = Path(temp) / "tasks.json"
        queue, task_id = _queue(path)
        record = queue.store.get(task_id)
        record.action_ledger["A-OLD"] = True
        queue.store.put(record, persist=True)
        _raises(lambda: RemoteTaskQueue(TaskStateStore(path)).load_action_state(task_id))

    with tempfile.TemporaryDirectory(prefix="durable-action-bounds-") as temp:
        path = Path(temp) / "tasks.json"
        queue, task_id = _queue(path)
        _raises(lambda: queue.prepare_action_batch(
            task_id, IDENTITY, "TURN-1", [{**ACTION, "action_id": "X" * 257}], {},
        ))
        _raises(lambda: queue.save_protocol_checkpoint(
            task_id, IDENTITY, {"next_prompt": "X" * 131073},
        ))
        assert queue.load_action_state(task_id)["actions"] == {}


if __name__ == "__main__":
    run()
    print("DURABLE_ACTION_LEDGER_OK")
