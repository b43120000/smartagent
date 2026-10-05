#!/usr/bin/env python3
"""Shared structured runtime events for all Tri-One interfaces."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .process_file_lock import exclusive_process_lock
from .paths import runtime_root


class RuntimeState(str, Enum):
    INITIALIZING = "INITIALIZING"
    BROWSER_ATTACHING = "BROWSER_ATTACHING"
    READY = "READY"
    WAITING_USER_INPUT = "WAITING_USER_INPUT"
    WAITING_WEBGPT_SIGNAL = "WAITING_WEBGPT_SIGNAL"
    WAITING_SIGNAL = "WAITING_SIGNAL"
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    VALIDATING = "VALIDATING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    UPLOADING_BATCH = "UPLOADING_BATCH"
    WAITING_BATCH_ACK = "WAITING_BATCH_ACK"
    WAITING_WEB_ACK = "WAITING_WEB_ACK"
    RESPONDING = "RESPONDING"
    WEBGPT_TURN_FINISHED = "WEBGPT_TURN_FINISHED"
    PAGE_PRESERVED = "PAGE_PRESERVED"
    TASK_COMPLETED = "TASK_COMPLETED"
    SESSION_CLOSING = "SESSION_CLOSING"
    ERROR = "ERROR"
    RECOVERING = "RECOVERING"
    SHUTDOWN_CLEANUP = "SHUTDOWN_CLEANUP"
    SELF_REPAIR_STALLED = "SELF_REPAIR_STALLED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class RuntimeEvent:
    interface: str
    generation_id: str
    state: str
    request_id: str = ""
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    pid: int = field(default_factory=os.getpid)
    event_protocol: str = "TRI_ONE_RUNTIME_EVENT_V1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_runtime_state(state: str | RuntimeState) -> str:
    if isinstance(state, RuntimeState):
        return state.value
    return str(state or "").strip().upper()


def emit_runtime_event(writer: "RuntimeEventWriter", interface: str, generation_id: str, state: str | RuntimeState, **kwargs: Any) -> dict[str, Any]:
    return writer.emit(interface, generation_id, normalize_runtime_state(state), **kwargs)


class RuntimeEventWriter:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = runtime_root(self.root) / "runtime_events.jsonl"
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def emit(self, interface: str, generation_id: str, state: str | RuntimeState, *, request_id: str = "", message: str = "", **detail: Any) -> dict[str, Any]:
        event = RuntimeEvent(
            interface=str(interface), generation_id=str(generation_id),
            state=normalize_runtime_state(state), request_id=str(request_id or ""),
            message=str(message or ""), detail=dict(detail),
        ).as_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_process_lock(self.lock_path, timeout_sec=10.0, label="Tri-One runtime events", legacy_kind="tri-one-runtime-events-v1"):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
        return event


__all__ = ["RuntimeState", "RuntimeEvent", "RuntimeEventWriter", "emit_runtime_event", "normalize_runtime_state"]
