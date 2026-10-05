#!/usr/bin/env python3
"""WebAgent writes action intent before a side-effect boundary and fails closed."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import ActionReconciliationRequired, WebAgentProtocolLoop
from RemoteAgent.telegram_webagent_worker import _accepted_checkpoint_payload
from agent_core.task_state import RemoteTaskQueue, TaskStateStore


def fence(value: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(value) + "\n```"


def make_task(root: Path, request_id: str) -> tuple[RemoteTaskQueue, object]:
    queue = RemoteTaskQueue(TaskStateStore(root / f"{request_id}.json"))
    task, created = queue.enqueue_remote_request({
        "protocol": "remote_agent", "protocol_version": 1,
        "request_id": request_id, "workspace": str(root),
        "conversation_url": f"https://chatgpt.com/c/{request_id}",
        "request": "List files",
    }, origin_turn_fingerprint=request_id)
    assert created and queue.claim_next() is not None
    return queue, task


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="durable-loop-") as temp:
        root = Path(temp)
        queue, task = make_task(root, "RR-DURABLE-1")
        planner_calls = []

        def planner(prompt: str, expected: dict, attachments: list[str]) -> str:
            planner_calls.append(dict(expected))
            if len(planner_calls) == 1:
                return fence({"tool": "list_directory", "action_id": "A-LIST", "path": str(root)}) + "\n" + fence(
                    {"tool": "turn_commit", "action_count": 1}
                )
            return fence({"tool": "final_response", "action_id": "A-FINAL", "content": "done"}) + "\n" + fence(
                {"tool": "turn_commit", "action_count": 1}
            )

        loop = WebAgentProtocolLoop(root, planner, durable_ledger=queue)
        assert loop.run(task.request, request_id=task.request_id, task_id=task.task_id, task_epoch=task.task_epoch) == "done"
        state = queue.load_action_state(task.task_id)
        assert state["actions"]["A-LIST"]["state"] == "COMMITTED"
        assert state["actions"]["A-LIST"]["prepared_result"]
        assert state["checkpoint"]["logical_round_state"] == "FINAL_ACCEPTED"
        assert len(planner_calls) == 2
        handoff = _accepted_checkpoint_payload(task, state)
        assert handoff is not None and handoff[1]["summary"] == "done"
        assert queue.interrupt_running(task.task_id, "worker exited after final") is not None
        retried = queue.retry_task(task_id=task.task_id)
        assert retried.state == "QUEUED"
        assert queue.claim_next() is not None
        assert queue.load_action_state(task.task_id)["checkpoint"]["final_content"] == "done"

        queue2, task2 = make_task(root, "RR-DURABLE-2")
        calls = []
        original_commit = queue2.commit_action_result

        def commit_failure(*args, **kwargs):
            raise RuntimeError("injected result-store failure")

        queue2.commit_action_result = commit_failure

        def single_action(prompt: str, expected: dict, attachments: list[str]) -> str:
            calls.append(dict(expected))
            return fence({"tool": "list_directory", "action_id": "A-UNCERTAIN", "path": str(root)}) + "\n" + fence(
                {"tool": "turn_commit", "action_count": 1}
            )

        loop = WebAgentProtocolLoop(root, single_action, durable_ledger=queue2)
        try:
            loop.run(task2.request, request_id=task2.request_id, task_id=task2.task_id, task_epoch=task2.task_epoch)
        except ActionReconciliationRequired as exc:
            assert "result was not committed" in str(exc)
        else:
            raise AssertionError("missing durable result must stop the loop")
        state = queue2.load_action_state(task2.task_id)
        assert state["actions"]["A-UNCERTAIN"]["state"] == "EXECUTING"
        assert len(calls) == 1

        queue3, task3 = make_task(root, "RR-DURABLE-3")
        initial_calls = []

        def first_round(prompt: str, expected: dict, attachments: list[str]) -> str:
            initial_calls.append(dict(expected))
            return fence({"tool": "list_directory", "action_id": "A-RESUME", "path": str(root)}) + "\n" + fence(
                {"tool": "turn_commit", "action_count": 1}
            )

        def stop_after_results() -> None:
            checkpoint = queue3.load_action_state(task3.task_id)["checkpoint"]
            if checkpoint.get("logical_round_state") == "RESULTS_READY":
                raise RuntimeError("simulated worker stop after durable results")

        loop = WebAgentProtocolLoop(
            root, first_round, durable_ledger=queue3, cancel_check=stop_after_results,
        )
        try:
            loop.run(task3.request, request_id=task3.request_id, task_id=task3.task_id, task_epoch=task3.task_epoch)
        except RuntimeError as exc:
            assert "simulated worker stop" in str(exc)
        else:
            raise AssertionError("simulated stop expected")
        assert len(initial_calls) == 1
        assert queue3.load_action_state(task3.task_id)["checkpoint"]["logical_round_state"] == "RESULTS_READY"
        assert queue3.interrupt_running(task3.task_id, "worker stopped") is not None
        assert queue3.retry_task(task_id=task3.task_id).state == "QUEUED"
        assert queue3.claim_next() is not None
        resumed_calls = []

        def final_after_resume(prompt: str, expected: dict, attachments: list[str]) -> str:
            resumed_calls.append(dict(expected))
            assert "A-RESUME" in prompt
            assert expected["turn_id"] == 2
            assert expected["ack_result_id"].startswith("RES-")
            return fence({"tool": "final_response", "action_id": "A-RESUMED-FINAL", "content": "resumed"}) + "\n" + fence(
                {"tool": "turn_commit", "action_count": 1}
            )

        loop = WebAgentProtocolLoop(root, final_after_resume, durable_ledger=queue3)
        loop.tools.execute = lambda action: (_ for _ in ()).throw(AssertionError("action replayed"))
        assert loop.run(task3.request, request_id=task3.request_id, task_id=task3.task_id, task_epoch=task3.task_epoch) == "resumed"
        assert len(resumed_calls) == 1
        assert queue2.interrupt_running(task2.task_id, "worker exited during action") is not None
        try:
            queue2.retry_task(task_id=task2.task_id)
        except Exception as exc:
            assert "recovery required" in str(exc) or "reconciliation required" in str(exc)
        else:
            raise AssertionError("uncertain action must not be requeued")
        queue2.commit_action_result = original_commit
        loop = WebAgentProtocolLoop(root, single_action, durable_ledger=queue2)
        try:
            loop.run(task2.request, request_id=task2.request_id, task_id=task2.task_id, task_epoch=task2.task_epoch)
        except ActionReconciliationRequired:
            pass
        else:
            raise AssertionError("new planner run must not replay uncertain work")
        assert len(calls) == 1


if __name__ == "__main__":
    run()
    print("WEBAGENT_DURABLE_PROTOCOL_LOOP_OK")
