#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared persistent task lifecycle + exactly-once queue primitives (Stage 7).

This module stays protocol-agnostic at the storage layer.  RemoteAgent may feed
validated REMOTE_AGENT_REQUEST dictionaries into ``RemoteTaskQueue`` while the
future Local/Remote workers can reuse the same TaskRecord/ledger primitives.

Safety invariants:
- durable enqueue happens before a conversation watcher cursor is acknowledged;
- a dedupe_key maps to at most one task across process restarts;
- COMPLETED tasks are never implicitly re-queued;
- RUNNING reservations are bounded by the Agent0 worker concurrency cap;
- orphaned RUNNING state is reconciled conservatively to INTERRUPTED on restart,
  never silently re-executed;
- corrupted task state fails closed instead of pretending the store is empty.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .process_file_lock import exclusive_process_lock
from .json_state_io import read_json_retry, write_json_atomic

TASK_QUEUED = "QUEUED"
TASK_RUNNING = "RUNNING"
TASK_PAUSING = "PAUSING"
TASK_PAUSED = "PAUSED"
TASK_RESUMING = "RESUMING"
TASK_COMPLETED = "COMPLETED"
TASK_FAILED = "FAILED"
TASK_INTERRUPTED = "INTERRUPTED"
TASK_CANCELLED = "CANCELLED"
TASK_TERMINAL_STATES = {TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED}
TASK_ACTIVE_STATES = {TASK_RUNNING, TASK_PAUSING, TASK_RESUMING}
TASK_PENDING_STATES = {TASK_QUEUED}
TASK_STORE_VERSION = 2
DEFAULT_LEASE_TIMEOUT_SEC = 90.0
DEFAULT_MAX_ATTEMPTS = 3


class TaskStateError(RuntimeError):
    pass


class ActionResultLedger(dict):
    """Exactly-once action result cache with the same dict API used by SmartAgent."""
    def remember(self, action_id: str, result: dict) -> None:
        if action_id:
            self[action_id] = result


@dataclass
class TaskRecord:
    task_id: str
    state: str = TASK_QUEUED
    origin: str = ""
    dedupe_key: str = ""
    request_id: str = ""
    task_epoch: str = ""
    request: str = ""
    request_fingerprint: str = ""
    session_id: str = ""
    priority: int = 100
    assigned_agent: str = ""
    checkpoint: str = ""
    current_stage: str = ""
    next_action: str = ""
    workspace: str = ""
    conversation_url: str = ""
    origin_turn_fingerprint: str = ""
    reply_route: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    error: str = ""
    action_ledger: dict = field(default_factory=dict)
    result_ledger: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStateStore:
    """Atomic JSON store for shared task lifecycle state."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.records: dict[str, TaskRecord] = {}
        self.loaded = False

    @contextmanager
    def process_lock(self, timeout_sec: float = 10.0):
        """Serialize transactions with a crash-safe kernel lock."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            with exclusive_process_lock(
                lock_path,
                timeout_sec=timeout_sec,
                label="task store",
                legacy_kind="remote-task-sentinel-v2",
            ):
                yield
        except RuntimeError as exc:
            raise TaskStateError(str(exc)) from exc

    def load(self) -> dict[str, TaskRecord]:
        with self._lock:
            if not self.path.exists():
                self.records = {}
                self.loaded = True
                return self.records
            try:
                payload = read_json_retry(self.path)
            except Exception as exc:
                raise TaskStateError(
                    f"task state unreadable; refusing empty-store fallback: {self.path}: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise TaskStateError(f"task state root must be object: {self.path}")
            items = payload.get("tasks", {})
            if not isinstance(items, dict):
                raise TaskStateError(f"task state tasks must be object: {self.path}")
            loaded: dict[str, TaskRecord] = {}
            for task_id, raw in items.items():
                if not isinstance(raw, dict):
                    raise TaskStateError(f"task {task_id!r} is not an object")
                data = dict(raw)
                data["task_id"] = str(task_id)
                # Forward/backward compatibility: ignore fields unknown to this
                # dataclass while defaulting fields absent from older Stage 1 stores.
                allowed = set(TaskRecord.__dataclass_fields__)
                data = {k: v for k, v in data.items() if k in allowed}
                try:
                    record = TaskRecord(**data)
                except Exception as exc:
                    raise TaskStateError(f"invalid task record {task_id!r}: {exc}") from exc
                loaded[record.task_id] = record
            self.records = loaded
            self.loaded = True
            return self.records

    def ensure_loaded(self) -> None:
        if not self.loaded:
            self.load()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": TASK_STORE_VERSION,
                "tasks": {
                    task_id: {k: v for k, v in asdict(record).items() if k != "task_id"}
                    for task_id, record in self.records.items()
                },
            }
            write_json_atomic(self.path, payload)

    def get(self, task_id: str) -> TaskRecord | None:
        self.ensure_loaded()
        return self.records.get(str(task_id))

    def put(self, record: TaskRecord, persist: bool = True) -> TaskRecord:
        with self._lock:
            self.ensure_loaded()
            record.updated_at = time.time()
            self.records[record.task_id] = record
            if persist:
                self.save()
            return record

    def all(self) -> list[TaskRecord]:
        self.ensure_loaded()
        return list(self.records.values())

    def find_by_dedupe_key(self, dedupe_key: str) -> TaskRecord | None:
        self.ensure_loaded()
        key = str(dedupe_key or "")
        for record in self.records.values():
            if record.dedupe_key == key:
                return record
        return None

    def list_by_state(self, states: Iterable[str]) -> list[TaskRecord]:
        wanted = {str(x) for x in states}
        return sorted(
            (record for record in self.all() if record.state in wanted),
            key=lambda r: (r.created_at, r.task_id),
        )


def normalize_request_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split())


def request_fingerprint(request_text: str) -> str:
    return hashlib.sha256(normalize_request_text(request_text).encode("utf-8", errors="replace")).hexdigest()


def remote_request_dedupe_key(message: dict[str, Any], *, origin_turn_fingerprint: str = "") -> str:
    """Durable RemoteAgent request identity.

    V1 identity is transport + endpoint + conversation + source turn/message
    identity. Request text and request_id are payload/correlation data only and
    must not collapse two distinct turns. A legacy request_id fallback is used
    only when a caller has no durable turn identity.
    """
    turn_identity = str(
        origin_turn_fingerprint
        or message.get("origin_turn_fingerprint", "")
        or ""
    ).strip()
    payload = {
        "transport": str(message.get("transport", "WEBGPT") or "WEBGPT").upper(),
        "endpoint": str(message.get("endpoint", "chatgpt.com") or "chatgpt.com").lower(),
        "conversation_url": str(message.get("conversation_url", "") or ""),
    }
    if turn_identity:
        payload["turn_identity"] = turn_identity
    else:
        payload["legacy_request_id"] = str(message.get("request_id", "") or "")
        payload["workspace"] = os.path.normcase(str(message.get("workspace", "") or ""))
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def task_id_from_dedupe_key(dedupe_key: str) -> str:
    return "TASK-" + str(dedupe_key)[:20].upper()


def _normalize_task_origin(message: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    meta = dict(metadata or {})
    route = meta.get("reply_route") or message.get("reply_route") or {}
    def token(v: object) -> str:
        return str(v or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "WEBCOPILOT": "WEBGPT_COPILOT", "WEB_COPILOT": "WEBGPT_COPILOT",
        "WEBGPTCOPILOT": "WEBGPT_COPILOT", "WEBGPT_COPILOT": "WEBGPT_COPILOT",
        "REMOTEAGENT": "REMOTE_AGENT", "REMOTE_AGENT": "REMOTE_AGENT",
    }
    explicit = token(message.get("origin") or meta.get("origin"))
    if explicit:
        return aliases.get(explicit, explicit)
    candidates = [token(message.get("transport") or meta.get("transport"))]
    if isinstance(route, dict):
        candidates.extend(token(route.get(k)) for k in ("type", "transport", "source", "origin"))
    if any(v in {"WEBCOPILOT", "WEB_COPILOT", "WEBGPTCOPILOT", "WEBGPT_COPILOT"} for v in candidates):
        return "WEBGPT_COPILOT"
    return "REMOTE_AGENT"


class RemoteTaskQueue:
    """Persistent queue for bounded request-scoped RemoteAgent workers."""
    def __init__(self, store: TaskStateStore):
        self.store = store
        self._lock = threading.RLock()
        self.store.ensure_loaded()

    def enqueue_remote_request(
        self,
        message: dict[str, Any],
        *,
        origin_turn_fingerprint: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[TaskRecord, bool]:
        """Durably enqueue a request. Return (record, created_new)."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                dedupe = remote_request_dedupe_key(message, origin_turn_fingerprint=origin_turn_fingerprint)
                existing = self.store.find_by_dedupe_key(dedupe)
                if existing is not None:
                    return existing, False

                text = str(message.get("request", "") or "")
                fingerprint = request_fingerprint(text)
                meta = dict(metadata or {})
                meta.setdefault("protocol_family", "SMARTAGENT_V9")
                meta.setdefault("protocol_version", 9)
                meta.setdefault("intent_digest", hashlib.sha256(" ".join(text.split()).encode("utf-8", errors="replace")).hexdigest())
                meta.setdefault("action_result_ledger", {})
                # user_fallback request IDs are derived from conversation,
                # turn index and user text. Preserve that stable lineage across
                # identity-version or branch-fingerprint changes during rollout.
                if str(meta.get("origin_role", "") or "") == "user_fallback":
                    request_id = str(message.get("request_id", "") or "")
                    conversation_url = str(message.get("conversation_url", "") or "")
                    for prior in self.store.all():
                        if (
                            str((prior.metadata or {}).get("origin_role", "") or "") == "user_fallback"
                            and prior.request_id == request_id
                            and prior.request_fingerprint == fingerprint
                            and prior.conversation_url == conversation_url
                        ):
                            return prior, False
                task = TaskRecord(
                    task_id=task_id_from_dedupe_key(dedupe),
                    state=TASK_QUEUED,
                    origin=_normalize_task_origin(message, metadata),
                    dedupe_key=dedupe,
                    request_id=str(message.get("request_id", "") or ""),
                    task_epoch=uuid.uuid4().hex,
                    request=text,
                    request_fingerprint=fingerprint,
                    session_id=str(message.get("session_id", "") or ""),
                    workspace=str(message.get("workspace", "") or ""),
                    conversation_url=str(message.get("conversation_url", "") or ""),
                    origin_turn_fingerprint=str(
                        origin_turn_fingerprint
                        or message.get("origin_turn_fingerprint", "")
                        or ""
                    ),
                    reply_route=dict(meta.get("reply_route") or {}),
                    metadata={**meta, "source_text": text, "delivery_state": "PENDING"},
                )
                self.store.put(task, persist=True)
                return task, True

    def active(self) -> TaskRecord | None:
        running = self.store.list_by_state({TASK_RUNNING})
        if len(running) > 1:
            raise TaskStateError(f"single-active invariant violated: {len(running)} RUNNING tasks")
        return running[0] if running else None

    def queued(self) -> list[TaskRecord]:
        return self.store.list_by_state({TASK_QUEUED})

    def running(self) -> list[TaskRecord]:
        return self.store.list_by_state({TASK_RUNNING})

    def dispatch_next(
        self,
        *,
        max_active: int = 3,
        allowed_bindings: Iterable[tuple[str, str]] | None = None,
        dispatcher_pid: int | None = None,
    ) -> tuple[TaskRecord, str] | None:
        """Reserve one queued task for a request-scoped worker process."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                if len(self.running()) >= max(1, int(max_active)):
                    return None
                queued = self.queued()
                from .conversation_identity import conversation_id as _cid
                active_cids = {_cid(str(t.conversation_url or "")) for t in self.running()}
                queued = [t for t in queued if _cid(str(t.conversation_url or "")) not in active_cids]
                if allowed_bindings is not None:
                    allowed = {
                        (os.path.normcase(os.path.abspath(workspace)), url)
                        for workspace, url in allowed_bindings
                    }
                    queued = [
                        task for task in queued
                        if (os.path.normcase(os.path.abspath(task.workspace)), task.conversation_url) in allowed
                    ]
                if not queued:
                    return None
                task = queued[0]
                token = uuid.uuid4().hex
                now = time.time()
                task.state = TASK_RUNNING
                task.started_at = now
                task.completed_at = None
                task.error = ""
                task.metadata.update({
                    "dispatcher_pid": int(dispatcher_pid or os.getpid()),
                    "dispatch_token": token,
                    "dispatched_at": now,
                    "heartbeat_at": now,
                    "worker_pid": 0,
                    "cancel_requested": False,
                    "attempt": int(task.metadata.get("attempt", 0) or 0) + 1,
                })
                self.store.put(task, persist=True)
                return task, token

    def adopt_dispatched(self, task_id: str, dispatch_token: str, *, worker_pid: int | None = None) -> TaskRecord:
        """Bind a reserved task to exactly one spawned worker PID."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(task_id)
                if task is None or task.state != TASK_RUNNING:
                    raise TaskStateError(f"task is not dispatched: {task_id}")
                if str(task.metadata.get("dispatch_token", "")) != str(dispatch_token or ""):
                    raise TaskStateError(f"dispatch token mismatch: {task_id}")
                owner = int(task.metadata.get("worker_pid", 0) or 0)
                wanted = int(worker_pid or os.getpid())
                if owner and owner != wanted:
                    raise TaskStateError(f"worker already adopted: {task_id}")
                task.metadata["worker_pid"] = wanted
                task.metadata["adopted_at"] = time.time()
                task.metadata["heartbeat_at"] = task.metadata["adopted_at"]
                return self.store.put(task, persist=True)

    def interrupt_stale_workers(self, *, timeout_sec: float = 45.0, now: float | None = None) -> list[TaskRecord]:
        """Fail closed when a dispatched/adopted worker stops heartbeating."""
        now = float(time.time() if now is None else now)
        changed = []
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                for task in self.running():
                    heartbeat = float(task.metadata.get("heartbeat_at", 0.0) or 0.0)
                    if heartbeat and now - heartbeat <= max(1.0, float(timeout_sec)):
                        continue
                    task.state = TASK_INTERRUPTED
                    task.completed_at = now
                    task.error = "remote_worker_heartbeat_timeout"
                    task.metadata["interrupted_at"] = now
                    changed.append(self.store.put(task, persist=False))
                if changed:
                    self.store.save()
        return changed

    def claim_next(
        self,
        *,
        workspace: str = "",
        conversation_url: str = "",
        allowed_bindings: Iterable[tuple[str, str]] | None = None,
    ) -> TaskRecord | None:
        """Transition oldest QUEUED task to RUNNING iff none is active."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                if self.active() is not None:
                    return None
                queued = self.queued()
                if allowed_bindings is not None:
                    allowed = {
                        (os.path.normcase(os.path.abspath(bound_workspace)), bound_url)
                        for bound_workspace, bound_url in allowed_bindings
                    }
                    queued = [
                        task for task in queued
                        if (
                            os.path.normcase(os.path.abspath(task.workspace)),
                            task.conversation_url,
                        ) in allowed
                    ]
                if workspace:
                    wanted_workspace = os.path.normcase(os.path.abspath(workspace))
                    queued = [
                        task for task in queued
                        if os.path.normcase(os.path.abspath(task.workspace)) == wanted_workspace
                    ]
                if conversation_url:
                    queued = [task for task in queued if task.conversation_url == conversation_url]
                if not queued:
                    return None
                task = queued[0]
                task.state = TASK_RUNNING
                task.started_at = time.time()
                task.completed_at = None
                task.error = ""
                task.metadata["worker_pid"] = os.getpid()
                task.metadata["claimed_at"] = task.started_at
                task.metadata["heartbeat_at"] = task.started_at
                task.metadata["cancel_requested"] = False
                task.metadata["attempt"] = int(task.metadata.get("attempt", 0) or 0) + 1
                self.store.put(task, persist=True)
                return task

    def complete(self, task_id: str, *, result: Any = None) -> TaskRecord:
        with self._lock:
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

    def heartbeat(self, task_id: str, *, worker_pid: int | None = None) -> TaskRecord:
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(task_id)
                if task is None or task.state != TASK_RUNNING:
                    raise TaskStateError(f"task is not RUNNING: {task_id}")
                owner = int(task.metadata.get("worker_pid", 0) or 0)
                if worker_pid is not None and owner and owner != int(worker_pid):
                    raise TaskStateError(f"worker ownership mismatch: {task_id}")
                task.metadata["heartbeat_at"] = time.time()
                return self.store.put(task, persist=True)

    def cancellation_requested(self, task_id: str) -> bool:
        with self.store.process_lock():
            self.store.load()
            task = self.store.get(task_id)
            return bool(task and task.metadata.get("cancel_requested"))

    def cancel(self, *, request_id: str, reason: str = "remote_cancel_requested") -> TaskRecord:
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = next((t for t in self.store.all() if t.request_id == request_id), None)
                if task is None:
                    raise TaskStateError(f"unknown request_id: {request_id}")
                if task.state == TASK_QUEUED:
                    task.state = TASK_CANCELLED
                    task.completed_at = time.time()
                    task.error = reason
                elif task.state == TASK_RUNNING:
                    task.metadata["cancel_requested"] = True
                    task.metadata["cancel_requested_at"] = time.time()
                    task.metadata["cancel_reason"] = reason
                return self.store.put(task, persist=True)

    def cancel_task(
        self, task_id: str, reason: str = "remote_cancel_requested"
    ) -> TaskRecord:
        """Request cancellation for one exact task without request-id ambiguity."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(str(task_id))
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if task.state == TASK_QUEUED:
                    task.state = TASK_CANCELLED
                    task.completed_at = time.time()
                    task.error = reason
                    task.metadata["delivery_state"] = "DELIVERED"
                elif task.state == TASK_RUNNING:
                    task.metadata["cancel_requested"] = True
                    task.metadata["cancel_requested_at"] = time.time()
                    task.metadata["cancel_reason"] = reason
                return self.store.put(task, persist=True)

    def _requeue_retryable(self, task: TaskRecord) -> TaskRecord:
        attempts = int(task.metadata.get("attempt", 0) or 0)
        if task.state not in {TASK_INTERRUPTED, TASK_FAILED}:
            raise TaskStateError(f"task is not retryable: {task.state}")
        if attempts >= DEFAULT_MAX_ATTEMPTS:
            raise TaskStateError(f"max attempts reached: {attempts}")
        task.state = TASK_QUEUED
        task.error = ""
        task.started_at = None
        task.completed_at = None
        task.metadata["cancel_requested"] = False
        task.metadata["retry_requested_at"] = time.time()
        return self.store.put(task, persist=True)

    def retry_task(self, *, task_id: str) -> TaskRecord:
        """Retry one exact durable task without ambiguous request-id lookup."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(str(task_id))
                if task is None:
                    raise TaskStateError(f"unknown task_id: {task_id}")
                return self._requeue_retryable(task)

    def retry_interrupted(self, *, request_id: str) -> TaskRecord:
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                matches = [t for t in self.store.all() if t.request_id == request_id]
                if not matches:
                    raise TaskStateError(f"unknown request_id: {request_id}")
                if len(matches) != 1:
                    raise TaskStateError(
                        f"ambiguous request_id: {request_id}; use retry_task(task_id=...)"
                    )
                return self._requeue_retryable(matches[0])

    def mark_cancelled(self, task_id: str, reason: str = "remote_cancelled") -> TaskRecord:
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                if task.state in TASK_TERMINAL_STATES:
                    return task
                task.state = TASK_CANCELLED
                task.completed_at = time.time()
                task.error = reason
                task.metadata["cancel_requested"] = True
                task.metadata["delivery_state"] = "DELIVERED"
                return self.store.put(task, persist=True)

    def fail_running(self, task_id: str, error: str) -> TaskRecord | None:
        """Atomically fail only a task that is still RUNNING."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(task_id)
                if task is None or task.state != TASK_RUNNING:
                    return None
                task.state = TASK_FAILED
                task.completed_at = time.time()
                task.error = str(error or "")
                task.metadata["delivery_state"] = "READY"
                return self.store.put(task, persist=True)

    def interrupt_running(
        self, task_id: str, error: str, *, result: Any = None,
    ) -> TaskRecord | None:
        """Preserve completed action evidence when only orchestration stopped.

        A transport/protocol closure failure is retryable and must not rewrite
        already committed local actions as TASK_FAILED.  The caller supplies a
        compact evidence snapshot so resume/recovery can continue without
        replaying those actions.
        """
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(task_id)
                if task is None or task.state != TASK_RUNNING:
                    return None
                task.state = TASK_INTERRUPTED
                task.completed_at = time.time()
                task.error = str(error or "")
                if result is not None:
                    task.result_ledger["interrupted"] = result
                task.metadata["delivery_state"] = "READY"
                return self.store.put(task, persist=True)

    def fail(self, task_id: str, error: str) -> TaskRecord:
        with self._lock:
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

    def abandon_incomplete(
        self,
        *,
        transports: Iterable[str],
        reason: str,
        created_before: float | None = None,
        request_prefixes: Iterable[str] = (),
    ) -> list[TaskRecord]:
        """Terminalize work left by an unclean transport supervisor exit.

        This is intentionally different from retry recovery: RemoteAgent's
        desktop launcher promises that closing the receiver does not replay an
        old mobile request on the next launch.  Other transports sharing the
        task store are excluded explicitly.
        """
        allowed = {str(value or "").upper() for value in transports}
        prefixes = tuple(str(value or "") for value in request_prefixes if str(value or ""))
        cutoff = float(created_before) if created_before is not None else None
        changed: list[TaskRecord] = []
        now = time.time()
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                for task in self.store.list_by_state({TASK_QUEUED, TASK_RUNNING}):
                    transport = str((task.metadata or {}).get("transport", "") or "").upper()
                    if transport not in allowed and not any(
                        str(task.request_id or "").startswith(prefix)
                        for prefix in prefixes
                    ):
                        continue
                    if cutoff is not None and float(task.created_at or 0.0) >= cutoff:
                        continue
                    was_running = task.state == TASK_RUNNING
                    task.state = TASK_INTERRUPTED if was_running else TASK_FAILED
                    task.completed_at = now
                    task.error = str(reason or "remote_supervisor_unclean_shutdown")
                    task.metadata["delivery_state"] = "READY"
                    task.metadata["abandoned_at"] = now
                    task.metadata["abandoned_from_state"] = (
                        TASK_RUNNING if was_running else TASK_QUEUED
                    )
                    changed.append(self.store.put(task, persist=False))
                if changed:
                    self.store.save()
        return changed

    def record_result_delivery(
        self, task_id: str, *, delivered: bool, reply: str = "", error: str = ""
    ) -> TaskRecord:
        """Persist Stage 9 delivery independently from execution completion."""
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                task = self.store.get(task_id)
                if task is None:
                    raise TaskStateError(f"unknown task: {task_id}")
                state = "DELIVERED" if delivered else "READY"
                task.metadata["delivery_state"] = state
                task.metadata["result_delivery"] = {
                    "state": state,
                    "attempted_at": time.time(),
                    "reply": str(reply or "")[:6000],
                    "error": str(error or "")[:2000],
                }
                return self.store.put(task, persist=True)

    def remember_action_result(self, task_id: str, action_id: str, result: Any) -> TaskRecord:
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise TaskStateError(f"unknown task: {task_id}")
            if action_id:
                task.action_ledger[str(action_id)] = True
                task.result_ledger[str(action_id)] = result
            return self.store.put(task, persist=True)

    def reconcile_on_start(self, *, lease_timeout_sec: float = DEFAULT_LEASE_TIMEOUT_SEC) -> list[TaskRecord]:
        """Interrupt only stale RUNNING leases and preserve live workers."""
        changed: list[TaskRecord] = []
        with self._lock:
            with self.store.process_lock():
                self.store.load()
                now = time.time()
                for task in self.store.list_by_state({TASK_RUNNING}):
                    heartbeat = float(task.metadata.get("heartbeat_at", 0.0) or 0.0)
                    if heartbeat and now - heartbeat <= max(
                        1.0, float(lease_timeout_sec)
                    ):
                        continue
                    task.state = TASK_INTERRUPTED
                    task.completed_at = now
                    task.error = "remote_worker_heartbeat_timeout"
                    task.metadata["interrupted_at"] = now
                    changed.append(self.store.put(task, persist=False))
                if changed:
                    self.store.save()
        return changed

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.store.all():
            counts[task.state] = counts.get(task.state, 0) + 1
        return counts


def run_task_state_self_tests() -> dict[str, Any]:
    import tempfile

    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "tasks.json"
        store = TaskStateStore(path)
        queue = RemoteTaskQueue(store)
        req_a = {
            "protocol": "remote_agent", "protocol_version": 1,
            "request_id": "RR-A", "workspace": "C:/workspace",
            "conversation_url": "https://chatgpt.com/c/a", "request": "task A",
        }
        req_b = {**req_a, "request_id": "RR-B", "request": "task B"}

        a1, created1 = queue.enqueue_remote_request(req_a, origin_turn_fingerprint="TURN-A")
        same = [queue.enqueue_remote_request(req_a, origin_turn_fingerprint="TURN-A") for _ in range(99)]
        results["same_request_100_scans_create_once"] = created1 and all(
            (not created and rec.task_id == a1.task_id) for rec, created in same
        ) and len(store.all()) == 1

        running = queue.claim_next()
        b, created_b = queue.enqueue_remote_request(req_b, origin_turn_fingerprint="TURN-B")
        results["single_active_worker_queue"] = bool(
            running and running.task_id == a1.task_id and running.state == TASK_RUNNING
            and created_b and b.state == TASK_QUEUED and queue.claim_next() is None
        )

        queue.complete(a1.task_id, result={"ok": True})
        next_task = queue.claim_next()
        results["next_claim_after_completion"] = bool(next_task and next_task.task_id == b.task_id)

        # Simulate supervisor restart while B was RUNNING.
        store2 = TaskStateStore(path)
        queue2 = RemoteTaskQueue(store2)
        reconciled = queue2.reconcile_on_start()
        b2 = store2.get(b.task_id)
        results["restart_reconciles_running_without_replay"] = bool(
            len(reconciled) == 1 and b2 and b2.state == TASK_INTERRUPTED
        )

        # Completed A must remain deduped across restart.
        a_again, created_again = queue2.enqueue_remote_request(req_a, origin_turn_fingerprint="TURN-A-REPAINT")
        results["completed_restart_not_reexecuted"] = bool(
            not created_again and a_again.task_id == a1.task_id and a_again.state == TASK_COMPLETED
        )

        # Corrupt store must fail closed.
        bad = Path(td) / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        try:
            TaskStateStore(bad).load()
            corrupt_rejected = False
        except TaskStateError:
            corrupt_rejected = True
        results["corrupt_store_fails_closed"] = corrupt_rejected

    results["all_passed"] = all(results.values())
    return results


__all__ = [
    "ActionResultLedger", "TaskRecord", "TaskStateStore", "TaskStateError",
    "RemoteTaskQueue", "TASK_QUEUED", "TASK_RUNNING", "TASK_PAUSING", "TASK_PAUSED",
    "TASK_RESUMING", "TASK_COMPLETED", "TASK_FAILED", "TASK_INTERRUPTED", "TASK_CANCELLED",
    "TASK_TERMINAL_STATES",
    "request_fingerprint", "remote_request_dedupe_key", "task_id_from_dedupe_key",
    "run_task_state_self_tests",
]
