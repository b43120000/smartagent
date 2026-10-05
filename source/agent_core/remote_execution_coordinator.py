#!/usr/bin/env python3
"""Request-cycle orchestration for the persistent RemoteAgent host."""
from __future__ import annotations

from typing import Any


class RemoteExecutionCoordinator:
    """Translate durable queue demand into one Agent 0 execution cycle.

    The host owns long-lived ingress and controls.  The process controller owns
    Agent 0.  This coordinator owns only the state transition between them.
    """

    def __init__(self, host: Any) -> None:
        self.host = host
        self.task_cycle_active = False
        self.waiting_logged = False

    def reset(self) -> None:
        self.task_cycle_active = False
        self.waiting_logged = False

    def _emit(self, event: str) -> None:
        lifecycle = self.host.runtime_lifecycle
        if lifecycle is not None:
            lifecycle.emit(event)

    def step(self, *, now: float) -> float:
        """Advance one non-blocking execution step and return poll delay."""
        pending = self.host.pending_remote_task_count()
        if pending > 0 and bool(getattr(self.host, "security_mutation_disabled", False)):
            if not self.waiting_logged:
                self.host.remote_runtime_log.write(
                    "ERROR", component="remote_execution_coordinator",
                    stage="SECURITY_PREFLIGHT_BLOCKED",
                    detail=getattr(self.host, "security_preflight_report", None),
                )
                print("[RemoteAgent-0] SECURITY_PREFLIGHT_BLOCKED; listener remains active", flush=True)
                self.waiting_logged = True
            return 1.0
        if pending > 0 and not self.task_cycle_active:
            self._emit("REQUEST_RECEIVED")
            self._emit("EXECUTING")

        if pending <= 0:
            self.host._apply_pending_external_binding()
            if self.host.agent0 is not None:
                self.host.supervise_agent0(now=now)
            if self.task_cycle_active and self.host._remote_browser_state:
                if not self.host._remote_agent0_execution_idle():
                    return 0.25
                self.host._stop_demand_agent0()
                self.host.remote_runtime_log.write(
                    "CONNECT",
                    component="remote_execution_coordinator",
                    stage="SESSION_RETAINED",
                    conversation_url=str(
                        self.host._remote_browser_state.get("conversation_url", "")
                        or ""
                    ),
                )
                self.task_cycle_active = False
                self._emit("WEBGPT_TURN_FINISHED")
                self._emit("PAGE_PRESERVED")
                self._emit("TASK_COMPLETED")
                self._emit("WAITING_SIGNAL")
            if not self.waiting_logged:
                print("[RemoteAgent-0] WAITING_SIGNAL", flush=True)
                self.waiting_logged = True
            return 0.25

        self.waiting_logged = False
        self.task_cycle_active = True
        try:
            state = self.host._ensure_remote_browser_host()
        except Exception as exc:
            self.host.remote_runtime_log.write(
                "ERROR",
                component="remote_execution_coordinator",
                stage="REMOTE_BROWSER_START",
                error=f"{type(exc).__name__}: {exc}",
            )
            print(f"[RemoteAgent-0] ERROR remote browser: {exc}", flush=True)
            return 1.0

        endpoint = str(state.get("cdp_endpoint", "") or "")
        previous_endpoint = str(
            self.host._agent0_state.get("cdp_endpoint", "") or ""
        )
        if (
            previous_endpoint
            and previous_endpoint != endpoint
            and self.host.agent0 is not None
            and self.host.agent0.poll() is None
        ):
            self.host.agent0_controller.stop(reset=True)

        state = dict(state)
        state["startup_mode"] = "TASK_DEMAND"
        self.host.ensure_agent0_running(state, new_console=False, now=now)
        return 0.25
