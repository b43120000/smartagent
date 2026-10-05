#!/usr/bin/env python3
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_core.remote_runtime_log import RemoteRuntimeLog
from RemoteAgent.remote_ingress import is_internal_agent_turn


class RemoteWorkerLauncher:
    """Dispatch one-shot Agent1 processes through one serialized shared CDP page."""

    def __init__(
        self,
        *,
        queue: Any,
        root: Path,
        cdp: str,
        allowed_bindings,
        max_workers: int = 1,
        python: str = sys.executable,
        popen=subprocess.Popen,
        runtime_log: RemoteRuntimeLog | None = None,
        event_store: Any | None = None,
        auth_state_provider=None,
    ):
        self.queue = queue
        self.root = Path(root)
        self.cdp = str(cdp)
        self.allowed_bindings = allowed_bindings
        self.max_workers = max(1, int(max_workers))
        self.python = str(python)
        self._popen = popen
        self.runtime_log = runtime_log
        self.event_store = event_store
        self.auth_state_provider = auth_state_provider
        self._processes = {}
        self._browser_state_paths: dict[str, Path] = {}

    def _log(self, event: str, **detail) -> None:
        if self.runtime_log is not None:
            self.runtime_log.write(event, component="worker_launcher", **detail)

    def _spawn(self, task, token: str):
        transport = str((getattr(task, "metadata", {}) or {}).get("transport", "")).upper()
        worker_entry = (
            [self.python, "-m", "RemoteAgent.telegram_webagent_worker"]
            if transport in {"TELEGRAM", "LOCAL_TEST"}
            else [self.python, str(self.root / "smart_agent.py")]
        )
        cmd = [
            *worker_entry,
            "--remote-worker-task", str(task.task_id),
            "--remote-worker-token", str(token),
            "--remote-cdp", self.cdp,
        ]
        env = os.environ.copy()
        env["SMARTAGENT_REMOTE_WORKER"] = "1"
        env["SMARTAGENT_ATTACH_CDP"] = "1"
        env.pop("SMARTAGENT_ISOLATED_BROWSER", None)
        env.pop("SMARTAGENT_REMOTE_BROWSER_STATE", None)
        env.setdefault("SMARTAGENT_REMOTE_WORKER_CONSOLE", "1")
        visible_console = os.name == "nt" and env.get("SMARTAGENT_REMOTE_WORKER_CONSOLE") == "1"
        flags = (
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if visible_console
            else getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt"
            else 0
        )
        streams = {"stdin": subprocess.DEVNULL}
        if not visible_console:
            streams.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self._popen(
            cmd,
            cwd=str(self.root),
            env=env,
            **streams,
            creationflags=flags,
        )

    def _reap_exited_workers(self) -> None:
        for task_id, process in list(self._processes.items()):
            code = process.poll()
            if code is None:
                continue
            self._processes.pop(task_id, None)
            state_path = self._browser_state_paths.pop(str(task_id), None)
            if state_path is not None:
                try:
                    state_path.unlink(missing_ok=True)
                except OSError:
                    pass
            failed = self.queue.fail_running(task_id, f"remote_worker_process_exit: exit_code={code}")
            if failed is not None:
                if self.event_store is not None:
                    self.event_store.emit("TASK_FAILED", failed, status="FAILED", payload={"error": failed.error})
                self._log("ERROR", stage="WORKER_PROCESS_EXIT", task_id=failed.task_id, request_id=failed.request_id, exit_code=code)

    def reconcile_workers(self) -> list[Any]:
        """Terminalize exited or orphaned workers without launching new work.

        This is deliberately safe to run before a browser is available.  A
        restarted Agent0 has no in-memory ``Popen`` handles for workers owned
        by its predecessor, so the durable heartbeat timeout is the recovery
        source of truth.
        """
        self._reap_exited_workers()
        interrupted = self.queue.interrupt_stale_workers()
        for task in interrupted:
            if self.event_store is not None:
                self.event_store.emit(
                    "TASK_INTERRUPTED", task, status="INTERRUPTED",
                    payload={"error": task.error},
                )
            self._log(
                "ERROR", stage="WORKER_HEARTBEAT_TIMEOUT",
                task_id=task.task_id, request_id=task.request_id,
            )
        return interrupted

    def dispatch_available(self) -> list[dict]:
        launched = []
        self.reconcile_workers()
        while len(self.queue.running()) < self.max_workers:
            reservation = self.queue.dispatch_next(
                max_active=self.max_workers,
                allowed_bindings=self.allowed_bindings(),
                dispatcher_pid=os.getpid(),
            )
            if reservation is None:
                break
            task, token = reservation
            if is_internal_agent_turn(str(getattr(task, "request", "") or "")):
                failed = self.queue.fail(task.task_id, "internal_agent_turn_rejected")
                if self.event_store is not None:
                    self.event_store.emit(
                        "TASK_FAILED", failed, status="FAILED",
                        payload={"error": failed.error},
                    )
                self._log(
                    "ERROR", stage="INTERNAL_TASK_REJECTED",
                    task_id=task.task_id, request_id=task.request_id,
                )
                continue
            try:
                process = self._spawn(task, token)
            except Exception as exc:
                state_path = self._browser_state_paths.pop(str(task.task_id), None)
                if state_path is not None:
                    try:
                        state_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self.queue.fail(task.task_id, f"worker_spawn_failed: {type(exc).__name__}: {exc}")
                if self.event_store is not None:
                    failed=self.queue.store.get(task.task_id); self.event_store.emit("TASK_FAILED",failed,status="FAILED",payload={"error":failed.error})
                self._log("ERROR", stage="WORKER_SPAWN", task_id=task.task_id, request_id=task.request_id, error=f"{type(exc).__name__}: {exc}")
                continue
            row = {
                "task_id": task.task_id, "request_id": task.request_id,
                "launcher_pid": getattr(process, "pid", None),
                "browser_mode": "SHARED_CDP",
                "worker_mode": (
                    "TELEGRAM_WEBAGENT_DIRECT"
                    if str((getattr(task, "metadata", {}) or {}).get("transport", "")).upper() in {"TELEGRAM", "LOCAL_TEST"}
                    else "SMART_AGENT_LEGACY"
                ),
            }
            launched.append(row)
            self._processes[task.task_id] = process
            self._log("TASK_STARTED", stage="WORKER_DISPATCHED", **row)
        return launched
