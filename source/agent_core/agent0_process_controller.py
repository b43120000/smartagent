#!/usr/bin/env python3
"""Single process owner for the demand-started Remote Agent 0 runtime."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


class Agent0ProcessController:
    """Own Agent 0 launch, liveness, termination, and restart backoff.

    Callers may request a running runtime, but only this class creates the
    process.  In particular, an exited process remains attached until ``tick``
    records its exit and applies backoff; this prevents a polling loop from
    bypassing supervision by replacing it immediately.
    """

    def __init__(
        self,
        *,
        root: Path,
        python: str,
        spawn: Callable[..., Any],
        runtime_log: Any,
        runtime_state_path: Path,
        terminate: Callable[[Any], None],
        dependency_available: Callable[[], bool],
        telegram_listener_active: Callable[[], bool],
        heartbeat_timeout: float,
    ) -> None:
        self.root = Path(root)
        self.python = str(python)
        self._spawn = spawn
        self.runtime_log = runtime_log
        self.runtime_state_path = Path(runtime_state_path)
        self._terminate = terminate
        self._dependency_available = dependency_available
        self._telegram_listener_active = telegram_listener_active
        self.heartbeat_timeout = float(heartbeat_timeout)

        self.process = None
        self.desired_state: dict[str, Any] = {}
        self.started_at = 0.0
        self.exit_pid = None
        self.restart_count = 0
        self.next_restart_at = 0.0
        self.hang_pid = None
        self._new_console = True

    def _launch(self, state: dict[str, Any], *, new_console: bool) -> Any:
        endpoint = str(state["cdp_endpoint"])
        script = self.root / "source" / "RemoteAgent" / "hidden_supervisor.py"
        cmd = [
            self.python,
            str(script),
            "--cdp",
            endpoint,
            "--parent-pid",
            str(os.getpid()),
            "--poll",
            "10.0",
        ]
        env = os.environ.copy()
        env["SMARTAGENT_SUPERVISOR_TOKEN"] = str(state.get("supervisor_token", ""))
        if state.get("startup_mode") == "TELEGRAM_INGRESS_FIRST":
            env["SMARTAGENT_REMOTE_INGRESS_FIRST"] = "1"
        if state.get("startup_mode") == "TASK_DEMAND":
            env["SMARTAGENT_REMOTE_DEMAND_SCOPED"] = "1"
        else:
            env.pop("SMARTAGENT_REMOTE_DEMAND_SCOPED", None)
        if self._telegram_listener_active():
            env["SMARTAGENT_TELEGRAM_RECEIVER_DISABLED"] = "1"
        env.setdefault("SMARTAGENT_REMOTE_CONSOLE", "1")

        visible_console = os.name == "nt" and env.get("SMARTAGENT_REMOTE_CONSOLE") == "1"
        if visible_console and new_console:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        elif os.name == "nt" and not visible_console:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            flags = 0
        streams: dict[str, Any] = {"stdin": subprocess.DEVNULL}
        if not visible_console:
            streams.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.desired_state = dict(state)
        self._new_console = bool(new_console)
        self.started_at = time.time()
        self.process = self._spawn(
            cmd,
            **streams,
            creationflags=flags,
            env=env,
        )
        self.runtime_log.write(
            "CONNECT",
            component="agent0_process_controller",
            stage="AGENT0_STARTED",
            agent0_pid=getattr(self.process, "pid", None),
            cdp=endpoint,
        )
        return self.process

    def request_running(
        self,
        state: dict[str, Any],
        *,
        new_console: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record demand and either launch once or advance supervision."""
        self.desired_state = dict(state)
        self._new_console = bool(new_console)
        if self.process is None:
            process = self._launch(self.desired_state, new_console=self._new_console)
            return {"status": "STARTED", "pid": getattr(process, "pid", None)}
        return self.tick(now=now)

    def start(self, state: dict[str, Any], *, new_console: bool = True) -> Any:
        """Compatibility entry point; creation still remains controller-owned."""
        if self.process is not None and self.process.poll() is None:
            return self.process
        return self._launch(dict(state), new_console=new_console)

    def tick(self, *, now: float | None = None) -> dict[str, Any]:
        """Advance health detection and bounded restart state exactly once."""
        now = float(time.time() if now is None else now)
        proc = self.process
        if proc is not None and proc.poll() is None:
            runtime_state = _load(self.runtime_state_path)
            heartbeat = float(runtime_state.get("heartbeat_at", 0.0) or 0.0)
            healthy = bool(
                str(runtime_state.get("status", "")).upper() == "RUNNING"
                and int(runtime_state.get("parent_pid", 0) or 0) == os.getpid()
                and heartbeat >= self.started_at - 1.0
                and now - heartbeat <= self.heartbeat_timeout
            )
            if healthy:
                self.hang_pid = None
                self.exit_pid = None
                self.restart_count = 0
                self.next_restart_at = 0.0
                return {
                    "status": "RUNNING",
                    "pid": runtime_state.get("runtime_pid") or getattr(proc, "pid", None),
                }
            if now - self.started_at <= self.heartbeat_timeout:
                return {"status": "STARTING", "pid": getattr(proc, "pid", None)}
            pid = getattr(proc, "pid", None)
            if pid != self.hang_pid:
                self.hang_pid = pid
                self.runtime_log.write(
                    "ERROR",
                    component="agent0_process_controller",
                    stage="AGENT0_HEARTBEAT_TIMEOUT",
                    agent0_pid=pid,
                    heartbeat_at=heartbeat,
                    heartbeat_timeout_sec=self.heartbeat_timeout,
                )
            stale = dict(runtime_state)
            stale.update(status="HUNG", detected_at=now)
            _atomic(self.runtime_state_path, stale)
            self._terminate(proc)
            try:
                proc.wait(timeout=2)
            except Exception:
                return {"status": "STOPPING_HUNG", "pid": pid}

        runtime_state = _load(self.runtime_state_path)
        heartbeat = float(runtime_state.get("heartbeat_at", 0.0) or 0.0)
        if (
            str(runtime_state.get("status", "")).upper() == "RUNNING"
            and int(runtime_state.get("parent_pid", 0) or 0) == os.getpid()
            and heartbeat > 0
            and now - heartbeat <= self.heartbeat_timeout
        ):
            return {
                "status": "RUNNING_DETACHED",
                "pid": runtime_state.get("runtime_pid"),
                "launcher_exit_code": proc.poll() if proc else None,
            }
        if not self.desired_state.get("cdp_endpoint"):
            return {"status": "NOT_STARTED"}
        if not self._dependency_available():
            return {"status": "UNSAFE_TO_RESTART", "reason": "agent1_unavailable"}

        exited_pid = getattr(proc, "pid", None)
        if exited_pid != self.exit_pid:
            self.exit_pid = exited_pid
            self.restart_count += 1
            delay = min(60.0, float(2 ** min(self.restart_count - 1, 5)))
            self.next_restart_at = now + delay
            self.runtime_log.write(
                "ERROR",
                component="agent0_process_controller",
                stage="AGENT0_EXIT",
                agent0_pid=exited_pid,
                exit_code=proc.poll() if proc else None,
                restart_count=self.restart_count,
                restart_delay_sec=delay,
            )
        if now < self.next_restart_at:
            return {"status": "BACKOFF", "retry_at": self.next_restart_at}

        self.runtime_log.write(
            "RECONNECT",
            component="agent0_process_controller",
            stage="AGENT0_RESTART",
            restart_count=self.restart_count,
        )
        restarted = self._launch(self.desired_state, new_console=self._new_console)
        return {"status": "RESTARTED", "pid": getattr(restarted, "pid", None)}

    def stop(self, *, reset: bool = True) -> None:
        proc = self.process
        if proc is not None and proc.poll() is None:
            self._terminate(proc)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        self.process = None
        self.hang_pid = None
        if reset:
            self.desired_state = {}
            self.exit_pid = None
            self.restart_count = 0
            self.next_restart_at = 0.0
            self.started_at = 0.0
