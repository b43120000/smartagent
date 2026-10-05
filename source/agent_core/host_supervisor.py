#!/usr/bin/env python3
"""External lifecycle owner for Agent 1, persistent Agent 0, and Meta Recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT
from .windows_power_guard import WindowsPowerGuard
from .remote_runtime_log import RemoteRuntimeLog
from .runtime_cleanup import RuntimeLifecycle
from .agent0_process_controller import Agent0ProcessController
from .remote_execution_coordinator import RemoteExecutionCoordinator
from .remote_control_dispatcher import RemoteControlDispatcher
from .remote_browser_session_manager import RemoteBrowserSessionManager
from .remote_restart_coordinator import RemoteRestartCoordinator
from .paths import (agent_host_state_path, conversation_registry_path, dispatcher_lock_path, dispatcher_state_path, remote_binding_path, remote_control_output_root, remote_events_path, remote_execution_page_lock_path, remote_runtime_log_path, remote_runtime_state_path, remote_supervisor_lock_path, remote_supervisor_state_path, remote_tasks_path, install_root, self_repair_root, source_root, status_metadata_path, telegram_config_path, telegram_listener_state_path )

SELF_REPAIR_STALLED = "SELF_REPAIR_STALLED"
NEEDS_HUMAN = "NEEDS_HUMAN"
META_ACTIVE = "META_RECOVERY_ACTIVE"
META_RESTARTING = "META_RECOVERY_RESTARTING"
META_RESUMING = "META_RECOVERY_RESUMING"
META_COMPLETED = "META_RECOVERY_COMPLETED"


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _process_is_alive(pid: int) -> bool:
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class HostSupervisor:
    def __init__(
        self,
        *,
        root: Path = AGENT_PROJECT_ROOT,
        python: str = sys.executable,
        popen=subprocess.Popen,
        enable_agent0: bool = True,
        enable_telegram_listener: bool | None = None,
        process_alive=_process_is_alive,
    ):
        self.root = Path(root)
        self.python = python
        self._popen = popen
        self.enable_agent0 = bool(enable_agent0)
        self.enable_telegram_listener = (
            self.enable_agent0
            if enable_telegram_listener is None
            else bool(enable_telegram_listener)
        )
        self._process_alive = process_alive
        self.token = uuid.uuid4().hex
        self.agent1 = None
        self.telegram_listener = None
        self._telegram_listener_started_at = 0.0
        self._remote_browser_scraper = None
        self._remote_browser_state: dict[str, Any] = {}
        self._remote_close_requested = threading.Event()
        self._remote_restart_requested = threading.Event()
        self._remote_runtime_restart_requested = threading.Event()
        from .remote_control_plane import RemoteControlPlane
        self.remote_control_plane = RemoteControlPlane(self.root)
        self._external_agent1_pid = 0
        self.generation = 0
        repair = self_repair_root(self.root)
        self.host_state = agent_host_state_path(self.root)
        self.status_state = status_metadata_path(self.root)
        self.active_repair = repair / "active_repair.json"
        self.restart_request = repair / "restart_request.json"
        self.restart_result = repair / "restart_result.json"
        self.meta_state = repair / "meta_recovery.json"
        self.issue_store = repair / "issues.jsonl"
        self.checkpoint_dir = repair / "checkpoints"
        self.remote_runtime_log = RemoteRuntimeLog(remote_runtime_log_path(self.root))
        self.remote_runtime_state = remote_runtime_state_path(self.root)
        self.remote_supervisor_state = remote_supervisor_state_path(self.root)
        self.telegram_listener_state = telegram_listener_state_path(self.root)
        self.agent0_heartbeat_timeout = max(5.0, float(os.environ.get("SMARTAGENT_AGENT0_HEARTBEAT_TIMEOUT_SEC", "30")))
        self.heartbeat_timeout = float(os.environ.get("SMARTAGENT_SELF_REPAIR_HEARTBEAT_TIMEOUT_SEC", "30"))
        self.stage_timeout = float(os.environ.get("SMARTAGENT_SELF_REPAIR_STAGE_STALL_SEC", "90"))
        self.meta_budget = max(1, int(os.environ.get("SMARTAGENT_META_REPAIR_BUDGET", "2")))
        self.restart_budget = max(1, int(os.environ.get("SMARTAGENT_META_RESTART_BUDGET", "3")))
        self.power_guard = WindowsPowerGuard()
        self.runtime_lifecycle: RuntimeLifecycle | None = None
        self.agent0_controller = Agent0ProcessController(
            root=self.root,
            python=self.python,
            spawn=self._spawn,
            runtime_log=self.remote_runtime_log,
            runtime_state_path=self.remote_runtime_state,
            terminate=self._terminate_agent0_tree,
            dependency_available=self._agent0_dependency_available,
            telegram_listener_active=lambda: bool(
                self.telegram_listener is not None
                or self.external_telegram_listener_active()
            ),
            heartbeat_timeout=self.agent0_heartbeat_timeout,
        )
        self.remote_browser_session = RemoteBrowserSessionManager(self)
        self.remote_restart_coordinator = RemoteRestartCoordinator(self)
        self.remote_control_dispatcher = self._build_remote_control_dispatcher()

    @property
    def agent0(self):
        return self.agent0_controller.process

    @agent0.setter
    def agent0(self, value) -> None:
        self.agent0_controller.process = value

    @property
    def _agent0_state(self) -> dict[str, Any]:
        return self.agent0_controller.desired_state

    @_agent0_state.setter
    def _agent0_state(self, value: dict[str, Any]) -> None:
        self.agent0_controller.desired_state = dict(value or {})

    @property
    def _agent0_started_at(self) -> float:
        return self.agent0_controller.started_at

    def _runtime_shutdown_requested(self) -> bool:
        return bool(
            self.runtime_lifecycle is not None
            and self.runtime_lifecycle.shutdown_requested()
        )

    def _spawn(self, cmd: list[str], **kwargs):
        return self._popen(cmd, cwd=str(self.root), **kwargs)

    def start_agent1(
        self,
        extra_args: list[str] | None = None,
        *,
        new_console: bool = False,
    ):
        env = os.environ.copy()
        env["SMARTAGENT_EXTERNAL_SUPERVISOR"] = "1"
        env["SMARTAGENT_SUPERVISOR_TOKEN"] = self.token
        env["SMARTAGENT_SELF_REPAIR_ROOT"] = str(self_repair_root(self.root))
        env["SMARTAGENT_PROJECT_ROOT"] = str(self.root)
        env["SMARTAGENT_REMOTE_AUTOSTART"] = "1" if self.enable_agent0 else "0"
        try:
            self.host_state.unlink(missing_ok=True)
        except OSError:
            pass
        self.generation += 1
        creationflags = (
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            if os.name == "nt" and new_console
            else 0
        )
        self.agent1 = self._spawn(
            [self.python, str(self.root / "source" / "smart_agent.py"), *(extra_args or [])],
            env=env,
            creationflags=creationflags,
        )
        return self.agent1

    def start_agent0(self, state: dict, *, new_console: bool = True):
        state = dict(state)
        state["supervisor_token"] = self.token
        return self.agent0_controller.start(state, new_console=new_console)

    def ensure_agent0_running(
        self,
        state: dict,
        *,
        new_console: bool,
        now: float | None = None,
    ) -> dict:
        """Single demand path: first launch once, then supervise/back off."""
        state = dict(state)
        state["supervisor_token"] = self.token
        if self.agent0 is None:
            proc = self.start_agent0(state, new_console=new_console)
            # Preserve the original same-cycle health observation.  If the
            # child exits immediately, this call records BACKOFF before the
            # next host-loop iteration can request another launch.
            observed = self.supervise_agent0(now=now)
            if observed.get("status") not in {"RUNNING", "STARTING"}:
                return observed
            return {"status": "STARTED", "pid": getattr(proc, "pid", None)}
        self._agent0_state = state
        self.agent0_controller._new_console = bool(new_console)
        return self.supervise_agent0(now=now)

    def external_telegram_listener_active(self, *, now: float | None = None) -> bool:
        """Detect the listener owned by the integrated LocalAgent supervisor."""
        state = _load(self.telegram_listener_state)
        pid = int(state.get("pid", 0) or 0)
        heartbeat = float(state.get("heartbeat_at", 0.0) or 0.0)
        now = float(time.time() if now is None else now)
        return bool(
            str(state.get("status", "")).upper() in {"RUNNING", "DEGRADED"}
            and pid != os.getpid()
            and self._process_alive(pid)
            and heartbeat > 0
            and now - heartbeat <= 15.0
            and bool(state.get("receiver_thread_alive", True))
        )

    def start_telegram_listener(self) -> bool:
        """Start durable Telegram ingress without creating Agent0 or a page."""
        if not self.enable_agent0:
            if not self.enable_telegram_listener:
                return False
        try:
            from RemoteAgent.telegram_listener import TelegramIngressListener

            listener = TelegramIngressListener(
                root=self.root,
                runtime_log=self.remote_runtime_log,
                control_handler=self._handle_remote_control,
                task_admission_guard=self._remote_task_security_admission,
            )
            if not listener.start():
                return False
            self.telegram_listener = listener
            self._telegram_listener_started_at = time.time()
            print(
                "[HostSupervisor] Telegram listener ready; Agent0/WebGPT "
                "will start only after a remote request.",
                flush=True,
            )
            return True
        except Exception as exc:
            self.remote_runtime_log.write(
                "ERROR",
                component="host_supervisor",
                stage="TELEGRAM_LISTENER_START",
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def _remote_task_security_admission(self, _message=None) -> tuple[bool, str]:
        """Keep software controls live while rejecting unsafe task creation."""
        if not bool(getattr(self, "security_mutation_disabled", False)):
            return True, ""
        report = getattr(self, "security_preflight_report", None) or {}
        attestation = report.get("attestation", {}) if isinstance(report, dict) else {}
        reasons = (
            report.get("reasons") or attestation.get("reasons")
            if isinstance(report, dict) and isinstance(attestation, dict)
            else None
        )
        detail = ", ".join(str(item) for item in (reasons or []))
        return False, detail or "security_preflight_failed"

    def ensure_telegram_listener_alive(self) -> bool:
        """Keep ingress alive independently from the browser/runtime worker."""
        listener = self.telegram_listener
        if listener is None:
            return self.start_telegram_listener()
        if listener.is_healthy():
            return True
        # A long-poll may need a short grace period while its thread starts.
        if time.time() - self._telegram_listener_started_at < 15.0:
            return True
        self.remote_runtime_log.write(
            "RECONNECT", component="host_supervisor",
            stage="TELEGRAM_LISTENER_RESTART",
        )
        self.stop_telegram_listener()
        return self.start_telegram_listener()

    def stop_telegram_listener(self) -> None:
        listener = self.telegram_listener
        self.telegram_listener = None
        if listener is not None:
            listener.stop()

    def _request_remote_session_close(self, _message=None) -> str:
        self._remote_close_requested.set()
        return "RemoteAgent 已收到關閉任務指令；目前遠端工作階段將關閉。"

    def _request_full_remote_restart(self, message=None) -> str:
        """Hand a force-stop/relaunch transaction to a detached helper."""
        from RemoteAgent.remote_restart import spawn_restart_helper

        context = dict(getattr(message, "reply_context", {}) or {})
        chat_id = int(context.get("chat_id", 0) or 0)
        reply_id = int(
            context.get("message_id", 0)
            or getattr(message, "source_message_id", 0)
            or 0
        )
        lifecycle = getattr(self, "runtime_lifecycle", None)
        generation = str(
            getattr(lifecycle, "generation_id", "") or ""
        )
        if chat_id <= 0 or lifecycle is None or not generation:
            raise RuntimeError("remote_restart_context_unavailable")
        helper_pid = spawn_restart_helper(
            root=self.root,
            old_pid=os.getpid(),
            old_generation=generation,
            chat_id=chat_id,
            reply_to_message_id=reply_id,
        )
        requested = lifecycle.manager.request_shutdown(
            generation, reason="TELEGRAM_FORCE_RESTART"
        )
        if not requested:
            raise RuntimeError("remote_restart_shutdown_not_requested")
        self.remote_runtime_log.write(
            "RECONNECT",
            component="host_supervisor",
            stage="FORCE_RESTART_HELPER_STARTED",
            helper_pid=helper_pid,
            old_generation=generation,
        )
        return (
            "已開始完整重啟；系統將關閉所有 Agent 後重新啟動 "
            "RemoteAgent。新 listener 就緒後會再回覆「已重新連線」。"
        )

    def _build_remote_control_dispatcher(self) -> RemoteControlDispatcher:
        restart_event = getattr(self, "_remote_runtime_restart_requested", None)
        if restart_event is None:
            restart_event = threading.Event()
            self._remote_runtime_restart_requested = restart_event
        return RemoteControlDispatcher(
            root=getattr(self, "root", AGENT_PROJECT_ROOT),
            python=getattr(self, "python", sys.executable),
            runtime_log=self.remote_runtime_log,
            control_plane=self.remote_control_plane,
            session_state=lambda: dict(
                getattr(self, "_remote_browser_state", {}) or {}
            ),
            request_restart=self._request_full_remote_restart,
            request_close=self._request_remote_session_close,
            runtime_apply_binding=self._apply_remote_binding,
            binding_update_allowed=lambda: self.pending_remote_task_count() == 0,
        )

    def _apply_remote_binding(self, binding: dict) -> None:
        """Atomically hot-apply an idle RemoteAgent binding.

        The task-store lock is the ingress serialization boundary: Telegram
        cannot enqueue work while the candidate is attested and committed.
        """
        from .conversation_registry import ConversationRegistry
        from .remote_binding import activate, load, save
        from .security_preflight import preflight
        from .task_state import (
            TASK_PAUSED,
            TASK_PAUSING,
            TASK_QUEUED,
            TASK_RESUMING,
            TASK_RUNNING,
            TaskStateStore,
        )
        from .windows_security import restricted_executor_required

        task_store = TaskStateStore(remote_tasks_path(self.root))
        registry_path = conversation_registry_path(self.root)
        registry = ConversationRegistry(registry_path)
        settings_path = remote_binding_path(self.root)
        telegram_settings_path = telegram_config_path(self.root)
        listener = getattr(self, "telegram_listener", None)
        receiver = getattr(listener, "receiver", None) if listener is not None else None
        ingress_lock = getattr(receiver, "binding_lock", None)

        # Lock order is ingress -> task store -> conversation registry.
        # A Telegram message therefore either queues entirely before this
        # transaction (which makes it abort) or observes the new binding.
        with ingress_lock if ingress_lock is not None else nullcontext():
            with task_store.process_lock():
                task_store.load()
                if task_store.list_by_state({
                    TASK_QUEUED, TASK_RUNNING, TASK_PAUSING,
                    TASK_PAUSED, TASK_RESUMING,
                }):
                    raise RuntimeError("remote_binding_update_requires_idle_runtime")

                security_report = None
                if restricted_executor_required():
                    security_report = preflight("remote", workspace=binding["workspace"])
                    if security_report.get("status") != "PASS":
                        attestation = security_report.get("attestation", {})
                        reasons = (
                            attestation.get("reasons", [])
                            if isinstance(attestation, dict) else []
                        )
                        detail = ",".join(str(value) for value in reasons)
                        raise RuntimeError(
                            "remote_binding_workspace_not_os_authorized:"
                            + (detail or "security_preflight_failed")
                        )

                previous = load(self.root)
                old_report = getattr(self, "security_preflight_report", None)
                old_disabled = bool(getattr(self, "security_mutation_disabled", False))
                settings_snapshot = (
                    settings_path.read_bytes() if settings_path.is_file() else None
                )
                telegram_settings_snapshot = (
                    telegram_settings_path.read_bytes()
                    if telegram_settings_path.is_file() else None
                )

                with registry.process_lock():
                    registry_snapshot = (
                        registry_path.read_bytes() if registry_path.is_file() else None
                    )
                    try:
                        applied = save(binding, self.root)
                        activate(self.root)
                        registry.load()
                        registry.upsert_binding(
                            applied["workspace"], applied["gpt_url"], persist=False
                        )
                        registry.configure_remote_conversation(
                            applied["workspace"], applied["gpt_url"], enabled=True,
                            poll_profile="normal", transport="WEBGPT", persist=False,
                        )
                        registry.save()
                        self.security_preflight_report = security_report
                        self.security_mutation_disabled = False
                        if listener is not None:
                            listener.update_workspace(applied["workspace"])
                        self._close_remote_session(reason="REMOTE_BINDING_UPDATED")
                    except Exception as apply_error:
                        rollback_errors = []
                        try:
                            if settings_snapshot is None:
                                settings_path.unlink(missing_ok=True)
                            else:
                                tmp = settings_path.with_name(
                                    settings_path.name
                                    + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.rollback"
                                )
                                tmp.write_bytes(settings_snapshot)
                                tmp.replace(settings_path)
                            activate(self.root)
                        except Exception as exc:
                            rollback_errors.append(f"binding:{type(exc).__name__}:{exc}")
                        try:
                            if telegram_settings_snapshot is None:
                                telegram_settings_path.unlink(missing_ok=True)
                            else:
                                tmp = telegram_settings_path.with_name(
                                    telegram_settings_path.name
                                    + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.rollback"
                                )
                                tmp.write_bytes(telegram_settings_snapshot)
                                tmp.replace(telegram_settings_path)
                        except Exception as exc:
                            rollback_errors.append(f"telegram:{type(exc).__name__}:{exc}")
                        try:
                            if registry_snapshot is None:
                                registry_path.unlink(missing_ok=True)
                            else:
                                tmp = registry_path.with_name(
                                    registry_path.name
                                    + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.rollback"
                                )
                                tmp.write_bytes(registry_snapshot)
                                tmp.replace(registry_path)
                        except Exception as exc:
                            rollback_errors.append(f"registry:{type(exc).__name__}:{exc}")
                        self.security_preflight_report = old_report
                        self.security_mutation_disabled = old_disabled
                        if listener is not None:
                            try:
                                listener.update_workspace(previous["workspace"])
                            except Exception as exc:
                                rollback_errors.append(
                                    f"listener:{type(exc).__name__}:{exc}"
                                )
                        if rollback_errors:
                            self.security_mutation_disabled = True
                            raise RuntimeError(
                                "remote_binding_apply_and_rollback_failed:"
                                f"apply={type(apply_error).__name__}:{apply_error};"
                                f"rollback={'|'.join(rollback_errors)}"
                            ) from apply_error
                        raise

    def _handle_remote_control(self, message=None):
        dispatcher = getattr(self, "remote_control_dispatcher", None)
        if dispatcher is None:
            dispatcher = self._build_remote_control_dispatcher()
            self.remote_control_dispatcher = dispatcher
        return dispatcher.handle(message)

    def _apply_pending_external_binding(self) -> bool:
        from .remote_binding import process_external_update
        row = process_external_update(self._apply_remote_binding, self.root)
        if not row:
            return False
        self.remote_runtime_log.write(
            "CONNECT" if row.get("status") == "DONE" else "ERROR",
            component="host_supervisor", stage="EXTERNAL_BINDING_UPDATE",
            request_id=str(row.get("request_id", "")),
            status=str(row.get("status", "")), error=str(row.get("error", "")),
        )
        return row.get("status") == "DONE"

    def _service_remote_control_if_idle(self, pending_count=None) -> bool:
        if pending_count is None:
            pending_count=self.pending_remote_task_count()
        if int(pending_count or 0)>0: return False
        request=self.remote_control_plane.claim(f"host:{os.getpid()}")
        if request is None: return False
        cid=request["request_id"]; command=str(request.get("command","") or "")
        try:
            from .remote_control_plane import execute_page_control
            from WebAgent.browser_bridge import execution_page_lease
            state=self._ensure_remote_browser_host()
            scraper=self._remote_browser_scraper
            if scraper is None or scraper._page is None: raise RuntimeError("remote_control_page_unavailable")
            if command in {"refresh","重新整理"}:
                self.remote_runtime_log.write("CONNECT",component="host_supervisor",stage="REFRESH_STARTED",control_id=cid)
            with execution_page_lease(timeout_sec=5.0,label="RemoteAgent idle control",marker_path=remote_execution_page_lock_path(self.root)):
                self.remote_runtime_log.write("CONNECT",component="host_supervisor",stage="CONTROL_PAGE_LEASE_ACQUIRED",control_id=cid)
                result=execute_page_control(scraper._page,state["conversation_url"],request,output_dir=remote_control_output_root(self.root))
            stage="SNAPSHOT_COMPLETED" if command=="snapshot webgpt" else "REFRESH_COMPLETED"
            self.remote_runtime_log.write("CONNECT",component="host_supervisor",stage=stage,control_id=cid)
            self.remote_control_plane.complete(cid,result=result)
            self.remote_runtime_log.write("CONNECT",component="host_supervisor",stage="CONTROL_RESUME",control_id=cid)
            return True
        except Exception as exc:
            self.remote_control_plane.complete(cid,error=f"{type(exc).__name__}: {exc}")
            self.remote_runtime_log.write("ERROR",component="host_supervisor",stage="CONTROL_RESUME",control_id=cid,error=f"{type(exc).__name__}: {exc}")
            return False

    def pending_remote_control_count(self) -> int:
        """Return control signals waiting for the demand-start runtime."""
        try:
            state = self.remote_control_plane._load()
            return int(str(state.get("status", "")).upper() == "PENDING")
        except Exception as exc:
            self.remote_runtime_log.write(
                "ERROR", component="host_supervisor", stage="PENDING_CONTROL_COUNT",
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0

    def _reconcile_unclean_remote_shutdown(self, *, started_at: float) -> list[Any]:
        """Do not replay incomplete remote work from an earlier launcher generation."""
        previous = _load(self.remote_supervisor_state)
        previous_pid = int(previous.get("pid", 0) or 0)

        from .remote_events import RemoteEventStore
        from .task_state import RemoteTaskQueue, TaskStateStore, TASK_INTERRUPTED

        queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path(self.root)))
        changed = queue.abandon_incomplete(
            transports={"TELEGRAM", "LOCAL_TEST"},
            reason="remote_supervisor_startup_generation_boundary",
            created_before=started_at,
        )
        events = RemoteEventStore(remote_events_path(self.root))
        for task in changed:
            event_type = "TASK_INTERRUPTED" if task.state == TASK_INTERRUPTED else "TASK_FAILED"
            events.emit(event_type, task, status=task.state, payload={"error": task.error})
        if changed:
            self.remote_runtime_log.write(
                "ERROR",
                component="host_supervisor",
                stage="STARTUP_STALE_TASKS_ABANDONED",
                previous_pid=previous_pid,
                task_ids=[task.task_id for task in changed],
            )
        return changed

    def _remote_binding(self) -> dict:
        manager = getattr(self, "remote_browser_session", None)
        if manager is None:
            manager = RemoteBrowserSessionManager(self)
            self.remote_browser_session = manager
        return manager.binding()

    def _ensure_remote_browser_host(self) -> dict:
        manager = getattr(self, "remote_browser_session", None)
        if manager is None:
            manager = RemoteBrowserSessionManager(self)
            self.remote_browser_session = manager
        return manager.ensure()

    def _close_remote_session(self, *, reason: str = "") -> None:
        if self.runtime_lifecycle is not None:
            self.runtime_lifecycle.emit("SESSION_CLOSING", message=str(reason or "UNSPECIFIED"))
        self.remote_runtime_log.write(
            "CONNECT", component="host_supervisor", stage="CLOSING_SESSION",
            reason=str(reason or "UNSPECIFIED"),
        )
        self.agent0_controller.stop(reset=True)
        self._mark_remote_runtime_stopped(reason=reason or "REMOTE_SESSION_CLOSED")
        manager = getattr(self, "remote_browser_session", None)
        if manager is None:
            manager = RemoteBrowserSessionManager(self)
            self.remote_browser_session = manager
        manager.close()
        print("[RemoteAgent-0] STOPPED remote session; WAITING_SIGNAL",flush=True)

    def _discard_remote_requests_for_restart(self) -> dict[str, int]:
        """Terminalize old RR work before rebuilding the remote runtime."""
        return self.remote_restart_coordinator.clean_requests()

    def pending_remote_task_count(self) -> int:
        """Return work that still needs Agent0 supervision.

        RUNNING is intentionally included.  A request-scoped worker can exit
        together with an older Agent0 process, leaving a durable RUNNING lease
        behind.  Treating only QUEUED work as demand would then prevent the
        replacement Agent0 from starting and reconciling that orphaned lease.
        """
        try:
            from .task_state import (
                RemoteTaskQueue,
                TaskStateStore,
                TASK_QUEUED,
                TASK_RUNNING,
            )
            from .remote_events import DELIVERED, RemoteEventStore
            store=RemoteTaskQueue(TaskStateStore(remote_tasks_path(self.root))).store
            with store.process_lock():
                store.load()
                tasks=store.list_by_state({TASK_QUEUED, TASK_RUNNING})
            event_store=RemoteEventStore(remote_events_path(self.root))
            accepted_delivered={
                event.task_id
                for event in event_store.events.values()
                if event.event_type=="TASK_ACCEPTED" and event.delivery_state==DELIVERED
            }
            return sum(
                1 for task in tasks
                if task.state==TASK_RUNNING
                or str((task.metadata or {}).get("transport", "")).upper() not in {"TELEGRAM","LOCAL_TEST"}
                or task.task_id in accepted_delivered
            )
        except Exception as exc:
            self.remote_runtime_log.write("ERROR",component="host_supervisor",stage="PENDING_TASK_COUNT",error=f"{type(exc).__name__}: {exc}")
            return 0

    def start_agent0_for_pending_task(self, ready_state: dict) -> bool:
        """Demand-start Agent0 after any ingress durably queues work."""
        supervised_tasks=self.pending_remote_task_count()
        if supervised_tasks <= 0:
            return False
        current = _load(self.host_state)
        if (
            current.get("status") not in {"ready", "idle", "busy"}
            or not str(current.get("cdp_endpoint", "") or "").strip()
        ):
            current = dict(ready_state or {})
        if (
            current.get("status") not in {"ready", "idle", "busy"}
            or not str(current.get("cdp_endpoint", "") or "").strip()
        ):
            return False

        current["startup_mode"] = "TASK_DEMAND"
        outcome = self.ensure_agent0_running(current, new_console=True)
        self.remote_runtime_log.write(
            "REQUEST_DETECTED",
            component="host_supervisor",
            stage="AGENT0_DEMAND_START",
            supervised_tasks=supervised_tasks,
            process_status=str(outcome.get("status", "")),
        )
        return outcome.get("status") in {"STARTED", "RESTARTED"}

    def _agent0_dependency_available(self) -> bool:
        if self.agent1 is not None:
            return self.agent1.poll() is None
        return bool(
            self._external_agent1_pid
            and self._process_alive(self._external_agent1_pid)
        )

    def live_local_agent_state(self) -> dict:
        """Return a live LocalAgent CDP publication, never a stale state file."""
        state = _load(self.host_state)
        host_pid = int(state.get("host_pid", 0) or 0)
        if (
            state.get("status") not in {"idle", "busy", "ready"}
            or not str(state.get("cdp_endpoint", "") or "").strip()
            or not self._process_alive(host_pid)
        ):
            return {}
        return state

    def _terminate_agent0_tree(self, proc) -> None:
        """Stop Agent0 itself without killing independent Agent1 workers.

        Request-scoped Agent1 processes are launched by the Agent0 runtime on
        Windows, so ``taskkill /T`` incorrectly treats them as expendable Agent0
        descendants.  Kill the recorded runtime and launcher PIDs individually;
        their Playwright pipes close, while adopted Agent1 workers stay alive.
        """
        pid = int(getattr(proc, "pid", 0) or 0)
        if not pid:
            return
        if os.name == "nt":
            state = _load(self.remote_runtime_state)
            runtime_pid = int(state.get("runtime_pid", 0) or 0)
            state_parent = int(state.get("parent_pid", 0) or 0)
            targets = []
            if runtime_pid and state_parent == os.getpid():
                targets.append(runtime_pid)
            targets.append(pid)
            attempted = False
            for target in dict.fromkeys(targets):
                try:
                    result = subprocess.run(
                        ["taskkill", "/PID", str(target), "/F"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5, check=False,
                    )
                    attempted = attempted or int(getattr(result, "returncode", 1) or 0) == 0
                except Exception:
                    pass
            if attempted:
                return
        try:
            proc.terminate()
        except Exception:
            pass

    def _mark_remote_runtime_stopped(self, *, reason: str) -> None:
        """Converge Agent0 state even when taskkill prevents its finally block."""
        state = _load(self.remote_runtime_state)
        if not state:
            return
        parent_pid = int(state.get("parent_pid", 0) or 0)
        if parent_pid not in {0, os.getpid()}:
            return
        now = time.time()
        state.update(
            status="STOPPED",
            heartbeat_at=now,
            stopped_at=now,
            invalidated_at=now,
            shutdown_reason=str(reason or "supervisor_shutdown"),
        )
        _atomic(self.remote_runtime_state, state)

    def _remote_agent0_execution_idle(self) -> bool:
        """Confirm Agent0 has reaped every request-scoped worker."""
        proc = self.agent0
        if proc is None or proc.poll() is not None:
            return True
        state = _load(self.remote_runtime_state)
        heartbeat = float(state.get("heartbeat_at", 0.0) or 0.0)
        return bool(
            str(state.get("status", "")).upper() == "RUNNING"
            and int(state.get("parent_pid", 0) or 0) == os.getpid()
            and bool(state.get("worker_state_known", False))
            and int(state.get("active_worker_count", -1) or 0) == 0
            and heartbeat >= self._agent0_started_at - 1.0
            and time.time() - heartbeat <= self.agent0_heartbeat_timeout
        )

    def _stop_demand_agent0(self) -> None:
        """Stop computation after one task while leaving the browser page alive."""
        proc = self.agent0
        if proc is None:
            return
        self.agent0_controller.stop(reset=True)
        self._mark_remote_runtime_stopped(reason="TASK_CYCLE_COMPLETE")
        self.remote_runtime_log.write(
            "CONNECT", component="host_supervisor",
            stage="AGENT0_STOPPED_AFTER_TASK",
            conversation_url=str(self._remote_browser_state.get("conversation_url", "") or ""),
        )

    def supervise_agent0(self, *, now: float | None = None) -> dict:
        """Compatibility facade for the controller-owned state machine."""
        return self.agent0_controller.tick(now=now)

    def stop_agent1(self, timeout: float = 8) -> None:
        proc = self.agent1
        if not proc or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def restart_agent1(self):
        self.stop_agent1()
        return self.start_agent1()

    @staticmethod
    def failure_fingerprint(reason: str, detail: str = "", stage: str = "") -> str:
        payload = json.dumps({"reason": reason, "detail": detail, "stage": stage}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    def load_meta_recovery(self) -> dict:
        return _load(self.meta_state)

    def _repair_context_active(self, active: dict | None = None) -> bool:
        active = dict(active or _load(self.active_repair))
        state = str(active.get("state", "") or "").upper()
        if not active:
            return False
        if state in {"", "COMPLETED", "FAILED", "CANCELLED", "NEEDS_HUMAN"}:
            return False
        return bool(active.get("issue_id") or active.get("repair_id") or active.get("run_id") or state)

    def _checkpoint_metadata(self) -> dict:
        active = _load(self.active_repair)
        run_id = str(active.get("run_id", "") or "")
        user = _load(self.checkpoint_dir / f"{run_id}.json") if run_id else {}
        return {
            "self_repair_checkpoint": active,
            "user_task_checkpoint": user,
            "resume_order": ["self_repair", "user_task"],
        }

    def _watchdog_snapshot(self) -> dict:
        host = _load(self.host_state)
        status = _load(self.status_state)
        repair = _load(self.active_repair)
        return {"host": host, "status": status, "repair": repair}

    def _latest_issue_event(self, *, run_id: str = "") -> dict:
        try:
            lines=self.issue_store.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        fallback={}
        for line in reversed(lines[-100:]):
            try:
                event=json.loads(line)
            except Exception:
                continue
            if not isinstance(event,dict):
                continue
            if not fallback:
                fallback=event
            if run_id and str(event.get("run_id") or "")==run_id:
                return event
        return {} if run_id else fallback

    def collect_diagnostic_evidence(self, *, reason: str = "", stage: str = "", detail: str = "") -> dict:
        snap=self._watchdog_snapshot()
        status=dict(snap.get("status") or {})
        repair=dict(snap.get("repair") or {})
        host=dict(snap.get("host") or {})
        run_id=str(repair.get("run_id") or status.get("run_id") or "")
        issue=self._latest_issue_event(run_id=run_id)
        return {
            "reason": str(reason or ""),
            "stage": str(stage or ""),
            "detail": str(detail or "")[:4000],
            "generation": self.generation,
            "agent1_exit_code": self.agent1.poll() if self.agent1 else None,
            "host": {k:host.get(k) for k in ("status","cdp_endpoint","startup_recovery","startup_meta") if k in host},
            "status": {k:status.get(k) for k in ("stage","state","actor","error_code","message","detail","task_phase","observed_state","heartbeat_at") if k in status},
            "repair": {k:repair.get(k) for k in ("state","issue_id","repair_id","run_id","failure_type","failure_message","failure_stage","updated_at") if k in repair},
            "issue": {k:issue.get(k) for k in ("issue_id","fingerprint","classification","exception_type","message","traceback","stage","tool","action_id","error_code","source_file","source_line","source_symbol","detail","status") if k in issue},
        }


    def classify_watchdog(
        self,
        *,
        now: float,
        agent1_alive: bool,
        ready: bool,
        heartbeat_at: float | None = None,
        stage: str = "",
        stage_changed_at: float | None = None,
        coordinator_alive: bool = True,
        repair_active: bool = True,
    ) -> dict:
        if not agent1_alive:
            reason = "agent1_exited_before_ready" if not ready else "repair_coordinator_interrupted"
            return {"stalled": True, "reason": reason, "stage": stage}
        if not repair_active:
            return {"stalled": False, "reason": "", "stage": stage}
        if not coordinator_alive:
            return {"stalled": True, "reason": "repair_coordinator_interrupted", "stage": stage}
        if heartbeat_at is not None and now - float(heartbeat_at) > self.heartbeat_timeout:
            return {"stalled": True, "reason": "heartbeat_timeout", "stage": stage}
        if stage_changed_at is not None and now - float(stage_changed_at) > self.stage_timeout:
            return {"stalled": True, "reason": "stage_stagnation", "stage": stage}
        return {"stalled": False, "reason": "", "stage": stage}

    def record_self_repair_stall(self, *, reason: str, detail: str = "", stage: str = "", now: float | None = None, evidence: dict | None = None) -> dict:
        now = float(time.time() if now is None else now)
        evidence = dict(evidence or self.collect_diagnostic_evidence(reason=reason, stage=stage, detail=detail))
        fp = self.failure_fingerprint(reason, detail, stage)
        cur = self.load_meta_recovery()
        active_states = {SELF_REPAIR_STALLED, META_ACTIVE, META_RESTARTING, META_RESUMING}
        meta_recovery_active = cur.get("state") in active_states
        # A suppressed/closed historical SELF_REPAIR_STALLED record must not
        # mask a failure from a later Agent1 generation.  Only treat it as an
        # active recovery lane while dispatch is pending or a repair context is
        # still active.  Otherwise the new stall replaces the stale evidence.
        if meta_recovery_active and cur.get("state") == SELF_REPAIR_STALLED:
            meta_recovery_active = bool(cur.get("repair_dispatch_required")) or self._repair_context_active()
        if meta_recovery_active:
            if cur.get("fingerprint") == fp:
                cur["duplicate_events"] = int(cur.get("duplicate_events", 0)) + 1
                cur["updated_at"] = now
                _atomic(self.meta_state, cur)
                return cur
            cur["blocked_secondary_fingerprint"] = fp
            cur["blocked_secondary_reason"] = reason
            cur["updated_at"] = now
            _atomic(self.meta_state, cur)
            return cur

        attempts = int(cur.get("meta_repair_attempts", 0))
        restarts = int(cur.get("restart_attempts", 0))
        if attempts >= self.meta_budget:
            out = {
                "version": 1,
                "state": NEEDS_HUMAN,
                "reason": "meta_repair_budget_exhausted",
                "failed_reason": reason,
                "fingerprint": fp,
                "meta_repair_attempts": attempts,
                "restart_attempts": restarts,
                "generation": self.generation,
                "diagnostic_evidence": evidence,
                "updated_at": now,
                **self._checkpoint_metadata(),
            }
            _atomic(self.meta_state, out)
            return out

        out = {
            "version": 1,
            "state": SELF_REPAIR_STALLED,
            "meta_issue_required": True,
            "repair_dispatch_required": True,
            "fingerprint": fp,
            "failed_reason": reason,
            "detail": detail,
            "stage": stage,
            "diagnostic_evidence": evidence,
            "generation": self.generation,
            "meta_repair_attempts": attempts,
            "restart_attempts": restarts,
            "duplicate_events": 0,
            "created_at": now,
            "updated_at": now,
            **self._checkpoint_metadata(),
        }
        _atomic(self.meta_state, out)
        return out
    def mark_meta_repair_dispatched(self, issue_id: str) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") == NEEDS_HUMAN:
            return data
        if data.get("state") not in {SELF_REPAIR_STALLED, META_ACTIVE}:
            data.update(state=NEEDS_HUMAN, reason="invalid_meta_dispatch_state", updated_at=time.time())
        elif data.get("state") == SELF_REPAIR_STALLED:
            data.update(
                state=META_ACTIVE,
                issue_id=str(issue_id),
                meta_issue_required=False,
                repair_dispatch_required=False,
                meta_repair_attempts=int(data.get("meta_repair_attempts", 0)) + 1,
                updated_at=time.time(),
            )
        _atomic(self.meta_state, data)
        return data

    def mark_meta_repair_succeeded(self) -> dict:
        data = self.load_meta_recovery()
        restarts = int(data.get("restart_attempts", 0))
        if restarts >= self.restart_budget:
            data.update(state=NEEDS_HUMAN, reason="restart_budget_exhausted", updated_at=time.time())
        elif data.get("state") != META_ACTIVE:
            data.update(state=NEEDS_HUMAN, reason="invalid_meta_success_state", updated_at=time.time())
        else:
            data.update(
                state=META_RESTARTING,
                restart_attempts=restarts + 1,
                clean_restart_required=True,
                resume_order=["self_repair", "user_task"],
                updated_at=time.time(),
            )
        _atomic(self.meta_state, data)
        return data

    def mark_restart_complete(self) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") not in {META_RESTARTING, META_RESUMING}:
            return data
        data.update(
            state=META_RESUMING,
            clean_restart_required=False,
            next_resume="self_repair",
            self_repair_resumed=False,
            user_task_resumed=False,
            resume_order=["self_repair", "user_task"],
            updated_at=time.time(),
        )
        _atomic(self.meta_state, data)
        return data

    def mark_self_repair_resumed(self) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") != META_RESUMING or data.get("next_resume") != "self_repair":
            return data
        data.update(self_repair_resumed=True, next_resume="user_task", updated_at=time.time())
        _atomic(self.meta_state, data)
        return data

    def mark_user_task_resumed(self) -> dict:
        data = self.load_meta_recovery()
        if data.get("state") != META_RESUMING:
            return data
        if data.get("next_resume") != "user_task" or not data.get("self_repair_resumed"):
            data.update(state=NEEDS_HUMAN, reason="resume_order_violation", updated_at=time.time())
        else:
            data.update(state=META_COMPLETED, user_task_resumed=True, next_resume="", updated_at=time.time())
        _atomic(self.meta_state, data)
        return data

    def wait_agent1_ready(self, timeout: float | None = None) -> dict:
        timeout = float(timeout or os.environ.get("SMARTAGENT_READINESS_TIMEOUT_SEC", "180"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._runtime_shutdown_requested():
                return {"status": "SHUTDOWN_REQUESTED", "supervisor_token": self.token}
            if self.agent1 and self.agent1.poll() is not None:
                meta = self.record_self_repair_stall(
                    reason="agent1_exited_before_ready",
                    detail=f"generation={self.generation}",
                    stage="AGENT1_BOOTSTRAP",
                )
                return {"status": SELF_REPAIR_STALLED, "meta_recovery": meta, "supervisor_token": self.token}
            state = _load(self.host_state)
            if state.get("supervisor_token") == self.token and state.get("cdp_endpoint") and state.get("status") in {"idle", "busy", "ready"}:
                return state
            time.sleep(0.2)
        meta = self.record_self_repair_stall(
            reason="agent1_readiness_timeout",
            detail=f"generation={self.generation}",
            stage="AGENT1_BOOTSTRAP",
        )
        return {"status": SELF_REPAIR_STALLED, "meta_recovery": meta, "supervisor_token": self.token}

    def evaluate_runtime_watchdog(self, *, now: float | None = None) -> dict:
        now = float(time.time() if now is None else now)
        snap = self._watchdog_snapshot()
        status = snap["status"]
        repair = snap["repair"]
        alive = bool(self.agent1 and self.agent1.poll() is None)
        ready = bool(snap["host"].get("status") in {"idle", "busy", "ready"})
        repair_active = self._repair_context_active(repair)

        # Completed status is terminal: stale TASK_RESUMING must not trigger meta recovery.
        status_stage = str(status.get("stage", "") or "").upper()
        status_state = str(status.get("state", "") or "").upper()
        if status_stage == "COMPLETED" or status_state == "COMPLETED":
            repair_active = False

        stage = str(status.get("stage", "") or repair.get("state", "") or "RUNTIME")
        heartbeat_at = status.get("heartbeat_at") if repair_active else None
        stage_changed_at = repair.get("updated_at") if repair_active else None
        return self.classify_watchdog(
            now=now,
            agent1_alive=alive,
            ready=ready,
            heartbeat_at=heartbeat_at,
            stage=stage,
            stage_changed_at=stage_changed_at,
            coordinator_alive=alive,
            repair_active=repair_active,
        )
    def _handle_restart_request(self) -> bool:
        if not self.restart_request.exists():
            return False
        req = _load(self.restart_request)
        if req.get("supervisor_token") != self.token:
            _atomic(self.restart_result, {"state": "STALE_REQUEST_REJECTED", "generation": self.generation, "at": time.time()})
            self.restart_request.unlink(missing_ok=True)
            return True
        if req.get("state") != "REQUESTED":
            return False

        self.restart_agent1()
        state = self.wait_agent1_ready()
        if state.get("status") == SELF_REPAIR_STALLED:
            _atomic(self.restart_result, {
                "state": SELF_REPAIR_STALLED,
                "generation": self.generation,
                "meta_recovery": state.get("meta_recovery", {}),
                "at": time.time(),
            })
            return True

        _atomic(self.restart_result, {"state": "RESTARTED", "generation": self.generation, "at": time.time()})
        self.restart_request.unlink(missing_ok=True)
        meta = self.load_meta_recovery()
        if meta.get("state") == META_RESTARTING:
            self.mark_restart_complete()
            from .self_repair_coordinator import SelfRepairCoordinator
            SelfRepairCoordinator(self_repair_root(self.root), project_root=self.root).sync_meta_resume_state()
        return True

    def _dispatch_meta_if_required(self) -> dict:
        meta = self.load_meta_recovery()
        if not meta.get("repair_dispatch_required"):
            return {"status": "NOT_REQUIRED"}
        try:
            from .meta_recovery_dispatcher import MetaRecoveryDispatcher
            result = MetaRecoveryDispatcher(root=self.root).dispatch_if_required(meta)
        except Exception as exc:
            result = {"status": "DISPATCH_FAILED", "error": f"{type(exc).__name__}: {exc}"}
        if result.get("status") == "PLAN_READY":
            issue_id=str(result.get("issue_id", "") or "")
            self.mark_meta_repair_dispatched(issue_id)
            try:
                from .meta_recovery_executor import execute_meta_plan
                execution=execute_meta_plan(result.get("plan_path", ""), self.root)
            except Exception as exc:
                execution={"status":"EXECUTOR_ERROR","error":f"{type(exc).__name__}: {exc}"}
            result["execution"]=execution
            if execution.get("status") in {"APPLIED","NO_CHANGES"}:
                progressed=self.mark_meta_repair_succeeded()
                if progressed.get("state")==META_RESTARTING:
                    from .self_repair_coordinator import SelfRepairCoordinator
                    coordinator=SelfRepairCoordinator(self_repair_root(self.root), project_root=self.root)
                    user_checkpoint=dict(progressed.get("user_task_checkpoint") or {})
                    self_checkpoint=dict(progressed.get("self_repair_checkpoint") or {})
                    run_id=str(user_checkpoint.get("run_id") or self_checkpoint.get("run_id") or "")
                    modified=list(result.get("modified_paths") or [])
                    reproducer=[self.python,"-m","py_compile",*modified] if modified else [self.python,"-c","import agent_core.host_supervisor, agent_core.self_repair_coordinator"]
                    coordinator.request_restart(issue_id=issue_id,repair_id="META-"+issue_id,run_id=run_id,candidate_revision=str(execution.get("candidate_revision","") or ""),reproducer_argv=reproducer,recovery_kind="meta_recovery",supervisor_token=self.token)
            else:
                cur=self.load_meta_recovery()
                cur.update(state=NEEDS_HUMAN,reason="meta_executor_failed",last_execution_result=execution,updated_at=time.time())
                _atomic(self.meta_state,cur)
        else:
            cur = self.load_meta_recovery()
            cur["last_dispatch_result"] = result
            cur["repair_dispatch_required"] = False
            cur["dispatch_suppressed"] = True
            cur["dispatch_failure_status"] = str(result.get("status", "DISPATCH_FAILED") or "DISPATCH_FAILED")
            cur["updated_at"] = time.time()
            _atomic(self.meta_state, cur)
        return result

    def run(self) -> int:
        self.power_guard.acquire()
        # The integrated launcher owns only a Telegram network listener here.
        # Full Agent0 (and therefore Playwright/CDP/WebGPT) is demand-started
        # after that listener durably queues a real request.
        self.start_telegram_listener()
        self.start_agent1()
        state = self.wait_agent1_ready()
        if self.runtime_lifecycle is not None and not self._runtime_shutdown_requested():
            if state.get("status") == SELF_REPAIR_STALLED:
                meta = dict(state.get("meta_recovery") or {})
                self.runtime_lifecycle.bootstrap_failed(
                    str(meta.get("failed_reason") or meta.get("reason") or "agent1_bootstrap_failed"),
                    detail=str(meta.get("detail", "")),
                )
            elif state.get("status") in {"idle", "busy", "ready"} and state.get("cdp_endpoint"):
                self.runtime_lifecycle.ready("WAITING_USER_INPUT")

        # Bootstrap failure is already persisted as SELF_REPAIR_STALLED. Keep
        # Host alive so an external/meta repair controller can act instead of
        # collapsing the supervisor process immediately.
        runtime_exit_seen_at = 0.0
        try:
            while True:
                if self._runtime_shutdown_requested():
                    return 0
                self.ensure_telegram_listener_alive()
                if self._handle_restart_request():
                    runtime_exit_seen_at = 0.0
                    time.sleep(0.25)
                    continue

                meta = self.load_meta_recovery()
                if meta.get("state") == NEEDS_HUMAN:
                    return 2
                if meta.get("repair_dispatch_required"):
                    self._dispatch_meta_if_required()
                    meta = self.load_meta_recovery()
                    if meta.get("state") == NEEDS_HUMAN:
                        return 2

                watchdog = self.evaluate_runtime_watchdog()
                if self.enable_agent0:
                    self.start_agent0_for_pending_task(state)
                    if self.agent0 is not None:
                        self.supervise_agent0()
                if watchdog.get("stalled"):
                    reason = str(watchdog.get("reason", "repair_coordinator_interrupted"))
                    stage = str(watchdog.get("stage", "RUNTIME"))
                    code = self.agent1.poll() if self.agent1 else None
                    detail = f"generation={self.generation};agent1_exit_code={code}"
                    stalled = self.record_self_repair_stall(reason=reason, detail=detail, stage=stage)
                    if stalled.get("state") == NEEDS_HUMAN:
                        return 2

                # Agent1 may die while Meta Recovery is pending. Do not return;
                # Host/Agent0 owns the outer lifecycle and must stay available
                # for repair dispatch and clean restart.
                if self.agent1 and self.agent1.poll() is not None:
                    if not runtime_exit_seen_at:
                        runtime_exit_seen_at = time.time()
                else:
                    runtime_exit_seen_at = 0.0

                time.sleep(0.25)
        finally:
            self.stop_telegram_listener()
            self.power_guard.release()
            self.stop_agent1()
            self.agent0_controller.stop(reset=True)

    def run_dispatcher_only(self) -> int:
        """Shared visible Agent0 dispatcher for WebCopilot without Telegram ownership."""
        self.power_guard.acquire()
        state_path=dispatcher_state_path(self.root)
        heartbeat_at=0.0
        print("[Agent0 Dispatcher] Ready; waiting for queued WebCopilot work.",flush=True)
        try:
            while True:
                now=time.time()
                if now-heartbeat_at>=5.0:
                    _atomic(state_path,{"status":"RUNNING","pid":os.getpid(),"heartbeat_at":now})
                    heartbeat_at=now
                pending=self.pending_remote_task_count()
                host=self.live_local_agent_state()
                if pending > 0 and host:
                    remote_state=dict(host)
                    remote_state["startup_mode"]="TASK_DEMAND"
                    outcome = self.ensure_agent0_running(
                        remote_state, new_console=True, now=now
                    )
                    if outcome.get("status") in {"STARTED", "RESTARTED"}:
                        print(
                            f"[Agent0 Dispatcher] Queued work detected: {pending}; "
                            f"Agent0 {outcome['status'].lower()}.",
                            flush=True,
                        )
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\n[Agent0 Dispatcher] Stopped.",flush=True)
            return 0
        finally:
            _atomic(state_path,{"status":"STOPPED","pid":os.getpid(),"heartbeat_at":time.time()})
            self.power_guard.release()
            self.agent0_controller.stop(reset=True)

    def run_remote_only(self, *, auto_start_local: bool = False) -> int:
        """Own Telegram ingress and the RemoteAgent browser lifecycle independently."""
        started_at = time.time()
        if not self.power_guard.acquire():
            detail = str(getattr(self.power_guard, "last_error", "") or "unknown")
            self.remote_runtime_log.write(
                "ERROR", component="host_supervisor",
                stage="REMOTE_SLEEP_PREVENTION_FAILED", error=detail,
            )
            if self.runtime_lifecycle is not None:
                self.runtime_lifecycle.bootstrap_failed(
                    "remote_sleep_prevention_failed", detail=detail
                )
            print(
                "[RemoteAgent-0] ERROR sleep prevention unavailable; "
                "listener will not start.", flush=True,
            )
            return 3
        power_state = {
            "sleep_prevention_active": bool(getattr(self.power_guard, "active", True)),
            "sleep_prevention_mode": str(getattr(self.power_guard, "mode", "test") or "test"),
        }
        self.remote_runtime_log.write(
            "STATUS", component="host_supervisor",
            stage="REMOTE_SLEEP_PREVENTION_ACTIVE", **power_state,
        )
        print(
            "[RemoteAgent-0] SLEEP_PREVENTION_ACTIVE "
            f"mode={power_state['sleep_prevention_mode']}", flush=True,
        )
        self._reconcile_unclean_remote_shutdown(started_at=started_at)
        self.start_telegram_listener()
        _atomic(self.remote_supervisor_state,{"status":"RUNNING","pid":os.getpid(),"heartbeat_at":time.time(),**power_state})
        if self.runtime_lifecycle is not None:
            self.runtime_lifecycle.ready("WAITING_SIGNAL")
        heartbeat_at=0.0
        execution = RemoteExecutionCoordinator(self)
        shutdown_reason="SUPERVISOR_EXIT"
        try:
            while True:
                try:
                    self.ensure_telegram_listener_alive()
                    if self._remote_runtime_restart_requested.is_set():
                        self._remote_runtime_restart_requested.clear()
                        self.remote_restart_coordinator.perform(execution)
                    if self._runtime_shutdown_requested():
                        shutdown_reason="RUNTIME_SHUTDOWN_REQUESTED"
                        return 0
                    now=time.time()
                    if now-heartbeat_at>=5.0:
                        _atomic(self.remote_supervisor_state,{"status":"RUNNING","pid":os.getpid(),"heartbeat_at":now,**power_state})
                        heartbeat_at=now
                    if self._remote_close_requested.is_set():
                        self._remote_close_requested.clear()
                        self._close_remote_session(reason="USER_REQUEST")
                        if self.runtime_lifecycle is not None:
                            self.runtime_lifecycle.emit("WAITING_SIGNAL")
                        execution.reset()
                    time.sleep(execution.step(now=now))
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    # One transient store/CDP/supervision failure must not tear
                    # down the persistent browser and Telegram receiver.
                    self.remote_runtime_log.write(
                        "ERROR", component="host_supervisor",
                        stage="REMOTE_LOOP_RECOVERED",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    print(f"[RemoteAgent-0] transient error; receiver remains active: {exc}",flush=True)
                    time.sleep(1.0)
        except KeyboardInterrupt:
            shutdown_reason="USER_INTERRUPT"
            print("\n[RemoteAgent-0] STOPPED",flush=True)
            return 0
        finally:
            _atomic(self.remote_supervisor_state,{"status":"STOPPED","pid":os.getpid(),"heartbeat_at":time.time(),"sleep_prevention_active":False,"sleep_prevention_mode":power_state["sleep_prevention_mode"]})
            self.stop_telegram_listener()
            self._close_remote_session(reason=shutdown_reason)
            self.power_guard.release()


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local-only", action="store_true")
    mode.add_argument("--remote-only", action="store_true")
    mode.add_argument("--dispatcher-only", action="store_true")
    args = parser.parse_args(argv)

    from .protocol_manifest import require_protocol_manifest
    require_protocol_manifest(install_root())
    security_preflight_report = None
    from .windows_security import restricted_executor_required
    if restricted_executor_required():
        from .security_preflight import preflight
        security_preflight_report = preflight("remote" if args.remote_only else "local")
        if security_preflight_report.get("status") != "PASS" and not args.remote_only:
            raise RuntimeError("security_preflight_failed:" + json.dumps(security_preflight_report, ensure_ascii=False))

    if args.local_only:
        lifecycle = RuntimeLifecycle.from_environment(AGENT_PROJECT_ROOT, "local")
        supervisor = HostSupervisor(enable_agent0=False)
        supervisor.runtime_lifecycle = lifecycle
        try:
            return supervisor.run()
        finally:
            if lifecycle is not None:
                lifecycle.close(reason="local_supervisor_exit")

    if args.remote_only:
        from RemoteAgent.local_telegram_config import apply_saved_telegram_environment
        telegram = apply_saved_telegram_environment(root=AGENT_PROJECT_ROOT)
        from .remote_binding import activate
        binding = activate(AGENT_PROJECT_ROOT)
        print(f"[RemoteAgent-0] Binding: workspace={binding['workspace']} URL={binding['gpt_url']}", flush=True)
        if telegram.get("loaded"):
            print(
                f"[RemoteAgent-0] Telegram enabled; workspace={telegram.get('workspace', '')}",
                flush=True,
            )
        elif telegram.get("reason") not in {"not_configured", ""}:
            print(
                f"[RemoteAgent-0] Telegram saved configuration unavailable: {telegram.get('reason')}",
                flush=True,
            )
    # Remote-only is a listener-only resident process. Agent0 remains
    # demand-started by run_remote_only after a durable remote task arrives.
    if args.remote_only:
        supervisor = HostSupervisor(enable_agent0=False, enable_telegram_listener=True)
        supervisor.security_mutation_disabled = bool(
            security_preflight_report is not None
            and security_preflight_report.get("status") != "PASS"
        )
        supervisor.security_preflight_report = security_preflight_report
    else:
        supervisor = HostSupervisor(enable_agent0=False)
    lifecycle = (
        RuntimeLifecycle.from_environment(AGENT_PROJECT_ROOT, "remote")
        if args.remote_only
        else None
    )
    supervisor.runtime_lifecycle = lifecycle
    if args.dispatcher_only:
        from .process_file_lock import exclusive_process_lock
        lock_path=dispatcher_lock_path(AGENT_PROJECT_ROOT)
        try:
            with exclusive_process_lock(lock_path,timeout_sec=0.25,label="Agent0 dispatcher",legacy_kind="agent0-dispatcher-sentinel-v2"):
                return supervisor.run_dispatcher_only()
        except RuntimeError as exc:
            if "lock timeout" in str(exc):
                print("[Agent0 Dispatcher] Dispatcher already running.",flush=True)
                return 0
            raise
    if args.remote_only:
        from .process_file_lock import exclusive_process_lock
        lock_path=remote_supervisor_lock_path(AGENT_PROJECT_ROOT)
        try:
            with exclusive_process_lock(lock_path,timeout_sec=0.25,label="remote supervisor",legacy_kind="remote-supervisor-sentinel-v2"):
                try:
                    return supervisor.run_remote_only(auto_start_local=False)
                finally:
                    if lifecycle is not None:
                        lifecycle.close(reason="remote_supervisor_exit")
        except RuntimeError as exc:
            if "lock timeout" in str(exc):
                print("[HostSupervisor] RemoteAgent supervisor already running.",flush=True)
                if lifecycle is not None:
                    lifecycle.close(reason="remote_supervisor_duplicate")
                return 0
            raise
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
