"""Shared logical clean-start barrier for the remote runtime.

The clean start intentionally preserves durable audit history.  It only removes
old work from every executable path: task queue leases, event delivery leases,
and request ownership leases.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .process_file_lock import exclusive_process_lock
from .paths import (
    remote_events_path,
    remote_handoff_path,
    remote_tasks_path,
)


def _cancel_handoff(root: Path, *, reason: str) -> int:
    path = remote_handoff_path(root)
    lock = path.with_name(path.name + ".lock")
    if not path.exists():
        return 0
    try:
        with exclusive_process_lock(lock, timeout_sec=10.0, label="remote handoff"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or str(value.get("state", "")).upper() not in {"PENDING", "CLAIMED"}:
                return 0
            now = time.time()
            value.update(state="CANCELLED", error=str(reason), cancelled_at=now, updated_at=now)
            temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
            return 1
    except (OSError, ValueError, TypeError, RuntimeError):
        return 0


def _terminate_task_processes(tasks: list[Any]) -> int:
    """Stop only worker PIDs owned by tasks closed by this clean start."""
    pids: set[int] = set()
    for task in tasks:
        metadata = dict(getattr(task, "metadata", {}) or {})
        for key in ("worker_pid", "launcher_pid", "dispatcher_pid"):
            try:
                pid = int(metadata.get(key, 0) or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0 and pid != os.getpid():
                pids.add(pid)
    terminated = 0
    for pid in sorted(pids):
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                terminated += int(getattr(result, "returncode", 1) == 0)
            else:
                os.kill(pid, signal.SIGTERM)
                terminated += 1
        except (OSError, subprocess.SubprocessError):
            continue
    return terminated


def remote_clean_start(root: str | Path, *, reason: str = "REMOTE_CLEAN_START") -> dict[str, int]:
    """Terminalize all remote work before a new runtime generation starts."""
    from .remote_events import RemoteEventStore
    from .request_ownership import RequestOwnershipRegistry
    from .task_state import RemoteTaskQueue, TaskStateStore

    queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path(root)))
    changed = queue.abandon_incomplete(
        transports={"TELEGRAM", "WEBGPT", "WEBGPT_COPILOT", "LOCAL_TEST"},
        request_prefixes={"RR-"},
        reason=str(reason),
    )
    processes_terminated = _terminate_task_processes(changed)
    task_ids = {str(task.task_id) for task in changed}
    paused = RemoteEventStore(remote_events_path(root)).pause_for_tasks(
        task_ids, reason=str(reason)
    )
    control_cancelled = 0
    try:
        from .remote_control_plane import RemoteControlPlane

        control_cancelled = int(
            RemoteControlPlane(root).cancel_for_clean_start(reason=str(reason))
        )
    except (OSError, RuntimeError):
        control_cancelled = 0
    handoff_cancelled = _cancel_handoff(root, reason=str(reason))
    ownership = RequestOwnershipRegistry(root).discard_for_restart(
        interface="remote"
    )
    approvals_cancelled = 0
    try:
        from .security_approval import SecurityApprovalLedger
        approval_roots = {str(Path(root).resolve())}
        approval_roots.update(str(Path(task.workspace).resolve()) for task in changed if getattr(task, "workspace", ""))
        approvals_cancelled = sum(
            SecurityApprovalLedger(workspace).clear_pending(reason=str(reason))
            for workspace in approval_roots
        )
    except (OSError, RuntimeError, ValueError):
        approvals_cancelled = 0
    return {
        "tasks_discarded": len(changed),
        "events_paused": int(paused),
        "ownerships_discarded": int(ownership),
        "control_cancelled": control_cancelled,
        "handoff_cancelled": handoff_cancelled,
        "processes_terminated": processes_terminated,
        "approvals_cancelled": approvals_cancelled,
    }


__all__ = ["remote_clean_start"]
