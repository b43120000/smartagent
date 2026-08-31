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
from pathlib import Path

from agent_core.agent_gateway import AgentIngressGateway
from agent_core.remote_events import RemoteEventStore
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.task_state import RemoteTaskQueue, TaskStateStore, TASK_QUEUED
from agent_core.transport_sessions import TransportSessionRouter
from agent_core.workspace import AGENT_PROJECT_ROOT
from RemoteAgent.telegram_pairing import TelegramPairingStore
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
    ) -> None:
        self.root = Path(root)
        self.base = self.root / ".agents"
        self.config = config or TelegramReceiverConfig.from_env()
        self.runtime_log = runtime_log or RemoteRuntimeLog(
            self.base / "remote_runtime.jsonl"
        )
        self.receiver: TelegramReceiver | None = None
        self.queue: RemoteTaskQueue | None = None
        self.state_path = self.base / "telegram_listener_state.json"
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        if not self.config.enabled:
            return

        self.config.validate()
        task_store = TaskStateStore(self.base / "remote_tasks.json")
        self.queue = RemoteTaskQueue(task_store)
        session_router = TransportSessionRouter(
            self.base / "remote_transport_sessions.json"
        )
        event_store = RemoteEventStore(self.base / "remote_events.json")
        ingress = AgentIngressGateway(
            task_queue=self.queue,
            session_router=session_router,
            event_store=event_store,
            runtime_log=self.runtime_log,
        )
        pairing_store = (
            TelegramPairingStore(self.base / "remote_telegram_pairing.json")
            if self.config.pairing_enabled
            else None
        )
        self.receiver = TelegramReceiver(
            config=self.config,
            client=client or TelegramBotClient(self.config),
            offset_store=TelegramOffsetStore(
                self.base / "remote_telegram_state.json"
            ),
            ingress=ingress,
            runtime_log=self.runtime_log,
            pairing_store=pairing_store,
            control_handler=control_handler,
        )

    @property
    def enabled(self) -> bool:
        return self.receiver is not None

    def _write_state(self, status: str) -> None:
        payload = {
            "version": 1,
            "status": str(status),
            "pid": os.getpid(),
            "heartbeat_at": time.time(),
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

        self._heartbeat_thread = threading.Thread(
            target=heartbeat,
            name="telegram-listener-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def start(self) -> bool:
        if self.receiver is None:
            return False
        self.receiver.start()
        self._write_state("RUNNING")
        self._start_heartbeat()
        self.runtime_log.write(
            "CONNECT",
            component="telegram_listener",
            stage="LISTENING_WITHOUT_AGENT0",
        )
        return True

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
