#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .paths import webcopilot_tasks_path
from .task_state import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INTERRUPTED,
    TASK_QUEUED,
    TASK_RUNNING,
    TaskRecord,
    TaskStateError,
    TaskStateStore,
    request_fingerprint,
    task_id_from_dedupe_key,
)

DEFAULT_WEBCOPILOT_TASK_STORE = webcopilot_tasks_path()


def webcopilot_dedupe_key(*, conversation_url: str, turn_index: int) -> str:
    payload = {
        "origin": "WEBGPT_COPILOT",
        "conversation_url": str(conversation_url or ""),
        "turn_index": int(turn_index),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


class WebCopilotTaskQueue:
    """Durable exactly-once queue keyed by ChatGPT conversation + user-turn index."""

    def __init__(self, store: TaskStateStore | None = None):
        self.store = store or TaskStateStore(DEFAULT_WEBCOPILOT_TASK_STORE)
        self.store.ensure_loaded()

    def enqueue(
        self,
        *,
        conversation_url: str,
        workspace: str,
        turn_index: int,
        request: str,
        raw_user_text: str = "",
        request_id: str = "",
        session_id: str = "",
        reply_route: dict[str, Any] | None = None,
        ingress_metadata: dict[str, Any] | None = None,
    ) -> tuple[TaskRecord, bool]:
        with self.store.process_lock():
            self.store.load()
            dedupe = webcopilot_dedupe_key(
                conversation_url=conversation_url,
                turn_index=turn_index,
            )
            existing = self.store.find_by_dedupe_key(dedupe)
            if existing is not None:
                return existing, False
            task = TaskRecord(
                task_id=task_id_from_dedupe_key(dedupe),
                state=TASK_QUEUED,
                origin="WEBGPT_COPILOT",
                dedupe_key=dedupe,
                request_id=str(request_id or f"WEBCOPILOT-{turn_index}"),
                request=str(request or ""),
                request_fingerprint=request_fingerprint(request),
                session_id=str(session_id or ""),
                workspace=str(workspace or ""),
                conversation_url=str(conversation_url or ""),
                origin_turn_fingerprint=f"turn-index:{int(turn_index)}",
                reply_route=dict(reply_route or {}),
                metadata={
                    "turn_index": int(turn_index),
                    "raw_user_text": str(raw_user_text or "")[:4000],
                    "delivery_state": "PENDING",
                    "transport": "WEBGPT_COPILOT",
                    "ingress_schema": "AGENT_INGRESS_V1",
                    "ingress_metadata": dict(ingress_metadata or {}),
                },
            )
            self.store.put(task, persist=True)
            return task, True

    def enqueue_remote_request(
        self,
        message: dict[str, Any],
        *,
        origin_turn_fingerprint: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[TaskRecord, bool]:
        """AgentIngressGateway backend compatibility for WebCopilot.

        The historical method name is retained at the queue boundary so both
        execution backends can accept the same normalized ingress envelope.
        """
        detail = dict(metadata or {})
        ingress = dict(detail.get("ingress_metadata") or {})
        if str(message.get("transport", "")).upper() != "WEBGPT_COPILOT":
            raise ValueError("webcopilot_queue_transport_mismatch")
        try:
            turn_index = int(ingress["turn_index"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("webcopilot_turn_index_required") from None
        return self.enqueue(
            conversation_url=str(message.get("conversation_url", "") or ""),
            workspace=str(message.get("workspace", "") or ""),
            turn_index=turn_index,
            request=str(message.get("request", "") or ""),
            raw_user_text=str(ingress.get("raw_user_text", "") or ""),
            request_id=str(message.get("request_id", "") or ""),
            session_id=str(message.get("session_id", "") or ""),
            reply_route=dict(detail.get("reply_route") or {}),
            ingress_metadata=ingress,
        )

    def get(self, task_id: str) -> TaskRecord | None:
        return self.store.get(task_id)

    def find_by_turn(self, *, conversation_url: str, turn_index: int) -> TaskRecord | None:
        return self.store.find_by_dedupe_key(
            webcopilot_dedupe_key(conversation_url=conversation_url, turn_index=turn_index)
        )

    def claim(self, task_id: str) -> TaskRecord:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            if task.state == TASK_COMPLETED:
                return task
            if task.state != TASK_QUEUED:
                raise TaskStateError(f"task not claimable: {task.state}")
            task.state = TASK_RUNNING
            task.started_at = time.time()
            task.completed_at = None
            task.error = ""
            task.metadata["worker_pid"] = os.getpid()
            task.metadata["claimed_at"] = task.started_at
            task.metadata["attempt"] = int(task.metadata.get("attempt", 0) or 0) + 1
            return self.store.put(task, persist=True)

    def complete(self, task_id: str, *, result: Any = None) -> TaskRecord:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            task.state = TASK_COMPLETED
            task.completed_at = time.time()
            task.error = ""
            if result is not None:
                task.result_ledger["final"] = result
            task.metadata["delivery_state"] = "READY"
            return self.store.put(task, persist=True)

    def fail(self, task_id: str, error: str) -> TaskRecord:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            task.state = TASK_FAILED
            task.completed_at = time.time()
            task.error = str(error or "")
            task.metadata["delivery_state"] = "READY"
            return self.store.put(task, persist=True)

    def queued_for_conversation(self, conversation_url: str) -> list[TaskRecord]:
        return [
            task for task in self.store.list_by_state({TASK_QUEUED})
            if task.conversation_url == str(conversation_url or "")
        ]

    def undelivered_for_conversation(self, conversation_url: str) -> list[TaskRecord]:
        return [
            task for task in self.store.list_by_state({TASK_COMPLETED, TASK_FAILED})
            if task.conversation_url == str(conversation_url or "")
            and task.metadata.get("delivery_state") == "READY"
        ]

    def mark_delivered(self, task_id: str) -> TaskRecord:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            task.metadata["delivery_state"] = "DELIVERED"
            task.metadata["delivered_at"] = time.time()
            return self.store.put(task, persist=True)

    def reconcile_on_start(self) -> list[TaskRecord]:
        """Never replay persisted RUNNING work automatically after restart."""
        changed = []
        with self.store.process_lock():
            self.store.load()
            for task in self.store.list_by_state({TASK_RUNNING}):
                task.state = TASK_INTERRUPTED
                task.completed_at = time.time()
                task.error = "webcopilot_restart_requires_explicit_resume"
                task.metadata["interrupted_at"] = task.completed_at
                changed.append(self.store.put(task, persist=False))
            if changed:
                self.store.save()
        return changed


def run_webcopilot_task_queue_self_tests() -> dict[str, bool]:
    import tempfile

    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as td:
        store = TaskStateStore(Path(td) / "webcopilot.json")
        queue = WebCopilotTaskQueue(store)
        kwargs = {
            "conversation_url": "https://chatgpt.com/c/test",
            "workspace": "C:/workspace",
            "turn_index": 7,
            "request": "same request",
            "raw_user_text": "webcopilot same request",
        }
        first, created = queue.enqueue(**kwargs)
        again, created_again = queue.enqueue(**kwargs)
        results["same_turn_enqueues_once"] = bool(
            created and not created_again and first.task_id == again.task_id and len(store.all()) == 1
        )
        other, other_created = queue.enqueue(**{**kwargs, "turn_index": 8})
        results["identical_text_distinct_turns"] = bool(
            other_created and other.task_id != first.task_id and len(store.all()) == 2
        )
        running = queue.claim(first.task_id)
        results["claim_persists_running"] = running.state == TASK_RUNNING
        store2 = TaskStateStore(store.path)
        queue2 = WebCopilotTaskQueue(store2)
        changed = queue2.reconcile_on_start()
        recovered = queue2.get(first.task_id)
        results["restart_does_not_replay_running"] = bool(
            len(changed) == 1 and recovered and recovered.state == TASK_INTERRUPTED
        )
        interrupted_claim_blocked = False
        try:
            queue2.claim(first.task_id)
        except TaskStateError:
            interrupted_claim_blocked = True
        results["interrupted_not_claimable"] = interrupted_claim_blocked
        queued = queue2.claim(other.task_id)
        queue2.complete(other.task_id, result={"ok": True})
        completed = queue2.get(other.task_id)
        results["completed_result_persisted"] = bool(
            queued.state == TASK_RUNNING and completed and completed.state == TASK_COMPLETED
            and completed.result_ledger.get("final") == {"ok": True}
        )
        queue2.mark_delivered(other.task_id)
        delivered = queue2.get(other.task_id)
        results["delivery_state_persisted"] = bool(
            delivered and delivered.metadata.get("delivery_state") == "DELIVERED"
        )
    results["all_passed"] = all(results.values())
    return results


__all__ = [
    "DEFAULT_WEBCOPILOT_TASK_STORE", "WebCopilotTaskQueue",
    "webcopilot_dedupe_key", "run_webcopilot_task_queue_self_tests",
]
