#!/usr/bin/env python3
from __future__ import annotations

"""Telegram-only ingress owned by HostSupervisor.

This component deliberately has no Playwright, CDP, WebGPT, or browser imports.
It turns Telegram updates into durable remote tasks; the supervisor starts the
full Agent0 runtime only after a queued task exists.
"""

import json
import os
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from agent_core.agent_gateway import AgentIngressGateway
from agent_core.remote_events import DeliveryManager, RemoteEventStore
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.task_state import RemoteTaskQueue, TaskStateStore, TASK_QUEUED
from agent_core.transport_sessions import TransportSessionRouter
from agent_core.workspace import AGENT_PROJECT_ROOT
from agent_core.paths import (remote_events_path, remote_runtime_log_path, remote_tasks_path, remote_transport_sessions_path, telegram_listener_state_path, telegram_offset_path, telegram_pairing_path)
from RemoteAgent.telegram_pairing import TelegramPairingStore
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from RemoteAgent.telegram_transport import (
    TelegramBotClient,
    TelegramOffsetStore,
    TelegramReceiver,
    TelegramReceiverConfig,
)


class TelegramIngressListener:
    """Persistent Telegram receiver that never owns a browser."""

    def __init__(
        self,
        *,
        root: str | Path = AGENT_PROJECT_ROOT,
        config: TelegramReceiverConfig | None = None,
        client: TelegramBotClient | None = None,
        runtime_log: RemoteRuntimeLog | None = None,
        control_handler=None,
        task_admission_guard=None,
    ) -> None:
        self.root = Path(root)
        self.config = config or TelegramReceiverConfig.from_env()
        self.runtime_log = runtime_log or RemoteRuntimeLog(
            remote_runtime_log_path(self.root)
        )
        self.receiver: TelegramReceiver | None = None
        self.queue: RemoteTaskQueue | None = None
        self.delivery: DeliveryManager | None = None
        self.state_path = telegram_listener_state_path(self.root)
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        if not self.config.enabled:
            return

        self.config.validate()
        task_store = TaskStateStore(remote_tasks_path(self.root))
        self.queue = RemoteTaskQueue(task_store)
        session_router = TransportSessionRouter(
            remote_transport_sessions_path(self.root)
        )
        event_store = RemoteEventStore(remote_events_path(self.root))
        ingress = AgentIngressGateway(
            task_queue=self.queue,
            session_router=session_router,
            event_store=event_store,
            runtime_log=self.runtime_log,
        )
        pairing_store = (
            TelegramPairingStore(telegram_pairing_path(self.root))
            if self.config.pairing_enabled
            else None
        )
        resolved_client = client or TelegramBotClient(self.config)
        accepted_delivery = DeliveryManager(
            event_store,
            {"TELEGRAM": TelegramDeliveryAdapter(resolved_client)},
        )
        self.delivery = accepted_delivery

        def deliver_accepted(task) -> None:
            event = event_store.emit("TASK_ACCEPTED", task, status="QUEUED", payload={})
            result = accepted_delivery.deliver(event)
            self.runtime_log.write(
                "DELIVERY",
                component="telegram_listener",
                stage="TASK_ACCEPTED_IMMEDIATE",
                task_id=task.task_id,
                request_id=task.request_id,
                delivered=bool(result.get("delivered")),
                reason=str(result.get("reason", "") or ""),
            )

        self.receiver = TelegramReceiver(
            config=self.config,
            client=resolved_client,
            offset_store=TelegramOffsetStore(
                telegram_offset_path(self.root)
            ),
            ingress=ingress,
            runtime_log=self.runtime_log,
            pairing_store=pairing_store,
            control_handler=control_handler,
            accepted_handler=deliver_accepted,
            task_admission_guard=task_admission_guard,
        )

    @property
    def enabled(self) -> bool:
        return self.receiver is not None

    def _write_state(self, status: str) -> None:
        receiver = self.receiver
        receiver_thread = getattr(receiver, "_thread", None) if receiver is not None else None
        receiver_alive = bool(receiver_thread is not None and receiver_thread.is_alive())
        now = time.time()
        poll_started = float(getattr(receiver, "last_poll_started_at", 0.0) or 0.0)
        poll_success = float(getattr(receiver, "last_poll_success_at", 0.0) or 0.0)
        poll_error = str(getattr(receiver, "last_poll_error", "") or "")
        poll_timeout = float(getattr(self.config, "poll_timeout_sec", 25) or 25)
        poll_recent = bool(
            receiver_alive
            and (
                (poll_started > 0 and now - poll_started <= poll_timeout + 15.0)
                or (poll_success > 0 and now - poll_success <= poll_timeout + 15.0)
            )
        )
        effective_status = str(status)
        if status == "RUNNING" and not (receiver_alive and poll_recent):
            effective_status = "DEGRADED"
        payload = {
            "version": 1,
            "status": effective_status,
            "pid": os.getpid(),
            "heartbeat_at": now,
            "receiver_thread_alive": receiver_alive,
            "poll_started_at": poll_started,
            "poll_success_at": poll_success,
            "poll_error": poll_error,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_name(
                f"{self.state_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp, self.state_path)
        except Exception as exc:
            self.runtime_log.write(
                "ERROR",
                component="telegram_listener",
                stage="STATE_WRITE",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()

        def heartbeat() -> None:
            while not self._heartbeat_stop.wait(5.0):
                self._write_state("RUNNING")
                if self.delivery is not None:
                    try:
                        self.delivery.retry_ready(transports={"TELEGRAM"})
                    except Exception as exc:
                        self.runtime_log.write(
                            "ERROR",
                            component="telegram_listener",
                            stage="DELIVERY_RETRY",
                            error=f"{type(exc).__name__}: {exc}",
                        )

        self._heartbeat_thread = threading.Thread(
            target=heartbeat,
            name="telegram-listener-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def start(self) -> bool:
        if self.receiver is None:
            return False
        # The runtime prepare step already discarded durable local work. Do
        # the transport-side equivalent before polling so Telegram backlog
        # cannot recreate those old requests after startup.
        self.receiver.discard_pending_updates_for_clean_start()
        self.receiver.start()
        self.receiver.announce_remote_feature_keyboard()
        self._write_state("RUNNING")
        self._start_heartbeat()
        self.runtime_log.write(
            "CONNECT",
            component="telegram_listener",
            stage="LISTENING_WITHOUT_AGENT0",
        )
        return True

    def is_healthy(self, *, now: float | None = None) -> bool:
        """Return true only while the actual Telegram polling thread is alive."""
        if self.receiver is None:
            return False
        now = float(time.time() if now is None else now)
        thread = getattr(self.receiver, "_thread", None)
        if thread is None or not thread.is_alive():
            return False
        started = float(getattr(self.receiver, "last_poll_started_at", 0.0) or 0.0)
        success = float(getattr(self.receiver, "last_poll_success_at", 0.0) or 0.0)
        window = float(self.config.poll_timeout_sec) + 15.0
        return bool(
            (started > 0 and now - started <= window)
            or (success > 0 and now - success <= window)
        )

    def update_workspace(self, workspace: str | Path) -> None:
        """Switch future Telegram ingress to a validated workspace in place."""
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError(f"telegram_workspace_unavailable:{root}")
        updated = replace(self.config, workspace=str(root))
        updated.validate()
        receiver = self.receiver
        lock = getattr(receiver, "binding_lock", None)
        context = lock if lock is not None else nullcontext()
        with context:
            self.config = updated
            if receiver is not None:
                receiver.config = updated
                receiver.adapter.config = updated
                if hasattr(receiver.client, "config"):
                    receiver.client.config = updated
        self.runtime_log.write(
            "CONNECT", component="telegram_listener",
            stage="WORKSPACE_HOT_APPLIED", workspace=str(root),
        )

    def stop(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
        if self.receiver is not None:
            self.receiver.stop()
        self._write_state("STOPPED")

    def pending_task_count(self) -> int:
        """Read fresh cross-process state instead of a cached queue snapshot."""
        if self.queue is None:
            return 0
        store = self.queue.store
        with store.process_lock():
            store.load()
            return len(store.list_by_state({TASK_QUEUED}))


__all__ = ["TelegramIngressListener"]
