#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic Session Manager control-plane foundation.

SessionManager decides which tasks exist and how their lifecycle changes.  It
never starts/stops processes; HostSupervisor/AgentHost remain runtime owners.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .task_ledger import PRIORITY_NORMAL, TaskLedger
from .task_state import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PAUSED,
    TASK_PAUSING,
    TASK_RESUMING,
    TASK_RUNNING,
    TaskRecord,
    TaskStateError,
    TaskStateStore,
)

SESSION_CREATED = "CREATED"
SESSION_RUNNING = "RUNNING"
SESSION_PAUSED = "PAUSED"
SESSION_COMPLETED = "COMPLETED"
SESSION_FAILED = "FAILED"
SESSION_STORE_VERSION = 1


@dataclass
class SessionRecord:
    session_id: str
    state: str = SESSION_CREATED
    description: str = ""
    workspace: str = ""
    conversation_url: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    """Small durable registry for session identity; task truth stays in TaskStateStore."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.records: dict[str, SessionRecord] = {}
        self.loaded = False

    def load(self) -> dict[str, SessionRecord]:
        with self._lock:
            if not self.path.exists():
                self.records = {}
                self.loaded = True
                return self.records
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise TaskStateError(f"session state unreadable: {self.path}: {exc}") from exc
            items = payload.get("sessions", {}) if isinstance(payload, dict) else None
            if not isinstance(items, dict):
                raise TaskStateError(f"session state sessions must be object: {self.path}")
            allowed = set(SessionRecord.__dataclass_fields__)
            records: dict[str, SessionRecord] = {}
            for session_id, raw in items.items():
                if not isinstance(raw, dict):
                    raise TaskStateError(f"session {session_id!r} is not an object")
                data = {k: v for k, v in raw.items() if k in allowed}
                data["session_id"] = str(session_id)
                records[str(session_id)] = SessionRecord(**data)
            self.records = records
            self.loaded = True
            return records

    def ensure_loaded(self) -> None:
        if not self.loaded:
            self.load()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": SESSION_STORE_VERSION,
                "sessions": {
                    sid: {k: v for k, v in asdict(record).items() if k != "session_id"}
                    for sid, record in self.records.items()
                },
            }
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def put(self, record: SessionRecord) -> SessionRecord:
        self.ensure_loaded()
        record.updated_at = time.time()
        self.records[record.session_id] = record
        self.save()
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        self.ensure_loaded()
        return self.records.get(str(session_id))

    def all(self) -> list[SessionRecord]:
        self.ensure_loaded()
        return sorted(self.records.values(), key=lambda item: (item.created_at, item.session_id))


class SessionManager:
    def __init__(self, *, session_store: SessionStore, task_store: TaskStateStore):
        self.sessions = session_store
        self.tasks = TaskLedger(task_store)
        self.sessions.ensure_loaded()

    @staticmethod
    def new_session_id() -> str:
        return "SESSION-" + uuid.uuid4().hex[:20].upper()

    def create_session(
        self,
        description: str = "",
        *,
        workspace: str = "",
        conversation_url: str = "",
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        sid = str(session_id or self.new_session_id())
        if self.sessions.get(sid) is not None:
            raise TaskStateError(f"session already exists: {sid}")
        return self.sessions.put(SessionRecord(
            session_id=sid,
            description=str(description or ""),
            workspace=str(workspace or ""),
            conversation_url=str(conversation_url or ""),
            metadata=dict(metadata or {}),
        ))

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def list_sessions(self) -> list[SessionRecord]:
        return self.sessions.all()

    def create_task(
        self,
        session_id: str,
        description: str,
        *,
        priority: int = PRIORITY_NORMAL,
        assigned_agent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        session = self._require_session(session_id)
        return self.tasks.create_task(
            session_id=session.session_id,
            description=description,
            priority=priority,
            assigned_agent=assigned_agent,
            workspace=session.workspace,
            conversation_url=session.conversation_url,
            metadata=metadata,
        )

    def start_task(self, task_id: str) -> TaskRecord:
        task = self.tasks.transition(task_id, TASK_RUNNING)
        self._sync_session_state(task.session_id)
        return task

    def begin_pause(self, task_id: str, *, next_action: str = "") -> TaskRecord:
        task = self.tasks.transition(task_id, TASK_PAUSING)
        if next_action:
            task = self.tasks.update_control_state(task_id, next_action=next_action)
        return task

    def finish_pause(self, task_id: str, *, checkpoint: str) -> TaskRecord:
        if not str(checkpoint or "").strip():
            raise TaskStateError("checkpoint is required before PAUSED")
        self.tasks.update_control_state(task_id, checkpoint=checkpoint)
        task = self.tasks.transition(task_id, TASK_PAUSED)
        self._sync_session_state(task.session_id)
        return task

    def begin_resume(self, task_id: str) -> TaskRecord:
        task = self.tasks.transition(task_id, TASK_RESUMING)
        if not task.checkpoint:
            # Fail closed: a resumable lifecycle must have an explicit recovery point.
            self.tasks.transition(task_id, TASK_PAUSED)
            raise TaskStateError(f"task has no checkpoint: {task_id}")
        return task

    def finish_resume(self, task_id: str) -> TaskRecord:
        task = self.tasks.transition(task_id, TASK_RUNNING)
        self._sync_session_state(task.session_id)
        return task

    def complete_task(self, task_id: str) -> TaskRecord:
        task = self.tasks.transition(task_id, TASK_COMPLETED)
        self._sync_session_state(task.session_id)
        return task

    def fail_task(self, task_id: str, error: str) -> TaskRecord:
        task = self.tasks.transition(task_id, TASK_FAILED, error=error)
        self._sync_session_state(task.session_id)
        return task

    def get_active_task(self, session_id: str) -> TaskRecord | None:
        active = self.tasks.list_tasks(session_id=session_id, states={TASK_RUNNING, TASK_PAUSING, TASK_RESUMING})
        if len(active) > 1:
            raise TaskStateError(f"session has multiple active tasks: {session_id}")
        return active[0] if active else None

    def select_next_task(self, *, session_id: str = "") -> TaskRecord | None:
        return self.tasks.select_next(session_id=session_id)

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self.sessions.get(session_id)
        if session is None:
            raise TaskStateError(f"unknown session: {session_id}")
        return session

    def _sync_session_state(self, session_id: str) -> None:
        session = self._require_session(session_id)
        tasks = self.tasks.list_tasks(session_id=session_id)
        states = {task.state for task in tasks}
        if any(state in {TASK_RUNNING, TASK_PAUSING, TASK_RESUMING} for state in states):
            session.state = SESSION_RUNNING
        elif TASK_PAUSED in states:
            session.state = SESSION_PAUSED
        elif tasks and all(task.state == TASK_COMPLETED for task in tasks):
            session.state = SESSION_COMPLETED
        elif tasks and all(task.state in {TASK_COMPLETED, TASK_FAILED} for task in tasks) and TASK_FAILED in states:
            session.state = SESSION_FAILED
        else:
            session.state = SESSION_CREATED
        self.sessions.put(session)
