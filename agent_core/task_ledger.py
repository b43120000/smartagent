#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Session-aware facade over the canonical durable TaskStateStore.

This module deliberately does not create a second persistence format.  It adds
session/control-plane operations while ``task_state.py`` remains the one task
state source of truth used by LocalAgent and RemoteAgent.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Iterable

from .task_state import (
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INTERRUPTED,
    TASK_PAUSED,
    TASK_PAUSING,
    TASK_QUEUED,
    TASK_RESUMING,
    TASK_RUNNING,
    TASK_TERMINAL_STATES,
    TaskRecord,
    TaskStateError,
    TaskStateStore,
)

PRIORITY_SYSTEM_RECOVERY = 0
PRIORITY_URGENT = 25
PRIORITY_HIGH = 50
PRIORITY_NORMAL = 100

SESSION_TRANSITIONS: dict[str, set[str]] = {
    TASK_QUEUED: {TASK_RUNNING, TASK_CANCELLED, TASK_FAILED},
    TASK_RUNNING: {TASK_PAUSING, TASK_COMPLETED, TASK_FAILED, TASK_INTERRUPTED, TASK_CANCELLED},
    TASK_PAUSING: {TASK_PAUSED, TASK_RUNNING, TASK_FAILED, TASK_INTERRUPTED},
    TASK_PAUSED: {TASK_RESUMING, TASK_CANCELLED, TASK_FAILED},
    TASK_RESUMING: {TASK_RUNNING, TASK_PAUSED, TASK_FAILED, TASK_INTERRUPTED},
    TASK_INTERRUPTED: {TASK_QUEUED, TASK_RESUMING, TASK_CANCELLED, TASK_FAILED},
    TASK_FAILED: {TASK_QUEUED},
    TASK_COMPLETED: set(),
    TASK_CANCELLED: set(),
}


class InvalidTaskTransition(TaskStateError):
    pass


def new_task_id() -> str:
    return "TASK-" + uuid.uuid4().hex[:20].upper()


def validate_transition(current: str, target: str) -> None:
    current = str(current or "")
    target = str(target or "")
    if current == target:
        return
    if target not in SESSION_TRANSITIONS.get(current, set()):
        raise InvalidTaskTransition(f"invalid task transition: {current} -> {target}")


class TaskLedger:
    """Control-plane task API backed by ``TaskStateStore``."""

    def __init__(self, store: TaskStateStore):
        self.store = store
        self.store.ensure_loaded()

    def create_task(
        self,
        *,
        session_id: str,
        description: str,
        priority: int = PRIORITY_NORMAL,
        assigned_agent: str = "",
        origin: str = "SESSION_MANAGER",
        workspace: str = "",
        conversation_url: str = "",
        metadata: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> TaskRecord:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise TaskStateError("session_id is required")
        record = TaskRecord(
            task_id=str(task_id or new_task_id()),
            state=TASK_QUEUED,
            session_id=session_id,
            priority=int(priority),
            assigned_agent=str(assigned_agent or ""),
            request=str(description or ""),
            origin=str(origin or "SESSION_MANAGER"),
            workspace=str(workspace or ""),
            conversation_url=str(conversation_url or ""),
            metadata=dict(metadata or {}),
        )
        with self.store.process_lock():
            self.store.load()
            if self.store.get(record.task_id) is not None:
                raise TaskStateError(f"task already exists: {record.task_id}")
            return self.store.put(record, persist=True)

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self.store.get(task_id)

    def list_tasks(self, *, session_id: str = "", states: Iterable[str] | None = None) -> list[TaskRecord]:
        wanted = {str(x) for x in states} if states is not None else None
        tasks = self.store.all()
        if session_id:
            tasks = [task for task in tasks if task.session_id == session_id]
        if wanted is not None:
            tasks = [task for task in tasks if task.state in wanted]
        return sorted(tasks, key=lambda task: (int(task.priority), task.created_at, task.task_id))

    def transition(self, task_id: str, target: str, *, error: str | None = None) -> TaskRecord:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            validate_transition(task.state, target)
            now = time.time()
            task.state = target
            if target == TASK_RUNNING and task.started_at is None:
                task.started_at = now
            if target in TASK_TERMINAL_STATES:
                task.completed_at = now
            elif target not in {TASK_FAILED, TASK_CANCELLED, TASK_COMPLETED}:
                task.completed_at = None
            if error is not None:
                task.error = str(error)
            return self.store.put(task, persist=True)

    def update_control_state(
        self,
        task_id: str,
        *,
        priority: int | None = None,
        assigned_agent: str | None = None,
        checkpoint: str | None = None,
        current_stage: str | None = None,
        next_action: str | None = None,
    ) -> TaskRecord:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            if priority is not None:
                task.priority = int(priority)
            if assigned_agent is not None:
                task.assigned_agent = str(assigned_agent)
            if checkpoint is not None:
                task.checkpoint = str(checkpoint)
            if current_stage is not None:
                task.current_stage = str(current_stage)
            if next_action is not None:
                task.next_action = str(next_action)
            return self.store.put(task, persist=True)

    def select_next(self, *, session_id: str = "") -> TaskRecord | None:
        candidates = self.list_tasks(session_id=session_id, states={TASK_QUEUED})
        return candidates[0] if candidates else None
