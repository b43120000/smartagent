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
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT
from .windows_power_guard import WindowsPowerGuard
from .remote_runtime_log import RemoteRuntimeLog

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
        process_alive=_process_is_alive,
    ):
        self.root = Path(root)
        self.python = python
        self._popen = popen
        self.enable_agent0 = bool(enable_agent0)
        self._process_alive = process_alive
        self.token = uuid.uuid4().hex
        self.agent1 = None
        self.agent0 = None
        self.telegram_listener = None
        self._remote_browser_scraper = None
        self._remote_browser_state: dict[str, Any] = {}
        self._remote_close_requested = threading.Event()
        self._agent0_state: dict[str, Any] = {}
        self._agent0_exit_pid = None
        self._agent0_restart_count = 0
        self._agent0_next_restart_at = 0.0
        self._agent0_started_at = 0.0
        self._agent0_hang_pid = None
        self._external_agent1_pid = 0
        self.generation = 0
        base = self.root / ".agents"
        repair = base / "self_repair"
        self.host_state = base / "agent_host_state.json"
        self.status_state = base / "status_metadata.json"
        self.active_repair = repair / "active_repair.json"
        self.restart_request = repair / "restart_request.json"
        self.restart_result = repair / "restart_result.json"
        self.meta_state = repair / "meta_recovery.json"
        self.issue_store = repair / "issues.jsonl"
        self.checkpoint_dir = repair / "checkpoints"
        self.remote_runtime_log = RemoteRuntimeLog(base / "remote_runtime.jsonl")
        self.remote_runtime_state = base / "remote_runtime_state.json"
        self.remote_supervisor_state = base / "remote_supervisor_state.json"
        self.telegram_listener_state = base / "telegram_listener_state.json"
        self.agent0_heartbeat_timeout = max(5.0, float(os.environ.get("SMARTAGENT_AGENT0_HEARTBEAT_TIMEOUT_SEC", "30")))
        self.heartbeat_timeout = float(os.environ.get("SMARTAGENT_SELF_REPAIR_HEARTBEAT_TIMEOUT_SEC", "30"))
        self.stage_timeout = float(os.environ.get("SMARTAGENT_SELF_REPAIR_STAGE_STALL_SEC", "90"))
        self.meta_budget = max(1, int(os.environ.get("SMARTAGENT_META_REPAIR_BUDGET", "2")))
        self.restart_budget = max(1, int(os.environ.get("SMARTAGENT_META_RESTART_BUDGET", "3")))
        self.power_guard = WindowsPowerGuard()

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
        env["SMARTAGENT_SELF_REPAIR_ROOT"] = str(self.root / ".agents" / "self_repair")
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
            [self.python, str(self.root / "smart_agent.py"), *(extra_args or [])],
            env=env,
            creationflags=creationflags,
        )
        return self.agent1

    def start_agent0(self, state: dict, *, new_console: bool = True):
        script = self.root / "RemoteAgent" / "hidden_supervisor.py"
        cmd = [self.python, str(script), "--cdp", str(state["cdp_endpoint"]), "--parent-pid", str(os.getpid()), "--poll", "10.0"]
        env = os.environ.copy()
        env["SMARTAGENT_SUPERVISOR_TOKEN"] = self.token
        if state.get("startup_mode") == "TELEGRAM_INGRESS_FIRST":
            env["SMARTAGENT_REMOTE_INGRESS_FIRST"] = "1"
        if self.telegram_listener is not None or self.external_telegram_listener_active():
            # HostSupervisor keeps the only Telegram getUpdates loop. Agent0
            # still configures Telegram delivery and authorization, but must
            # not compete for the same Bot API offset.
            env["SMARTAGENT_TELEGRAM_RECEIVER_DISABLED"] = "1"
        env.setdefault("SMARTAGENT_REMOTE_CONSOLE", "1")
        visible_console = os.name == "nt" and env.get("SMARTAGENT_REMOTE_CONSOLE") == "1"
        if visible_console and new_console:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        elif os.name == "nt" and not visible_console:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            # remote-only mode intentionally inherits launch_remote_agent.bat's
            # console so Agent0 status is visible without a third CMD window.
            flags = 0
        streams = {"stdin": subprocess.DEVNULL}
        if not visible_console:
            streams.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._agent0_state = dict(state)
        self._agent0_started_at = time.time()
        self.agent0 = self._spawn(
            cmd,
            **streams,
            creationflags=flags,
            env=env,
        )
        self.remote_runtime_log.write("CONNECT", component="host_supervisor", stage="AGENT0_STARTED", agent0_pid=getattr(self.agent0,"pid",None), cdp=str(state["cdp_endpoint"]))
        return self.agent0

    def external_telegram_listener_active(self, *, now: float | None = None) -> bool:
        """Detect the listener owned by the integrated LocalAgent supervisor."""
        state = _load(self.telegram_listener_state)
        pid = int(state.get("pid", 0) or 0)
        heartbeat = float(state.get("heartbeat_at", 0.0) or 0.0)
        now = float(time.time() if now is None else now)
        return bool(
            str(state.get("status", "")).upper() == "RUNNING"
            and pid != os.getpid()
            and self._process_alive(pid)
            and heartbeat > 0
            and now - heartbeat <= 15.0
        )

    def start_telegram_listener(self) -> bool:
        """Start durable Telegram ingress without creating Agent0 or a page."""
        if not self.enable_agent0:
            return False
        try:
            from RemoteAgent.telegram_listener import TelegramIngressListener

            listener = TelegramIngressListener(
                root=self.root,
                runtime_log=self.remote_runtime_log,
                control_handler=self._request_remote_session_close,
            )
            if not listener.start():
                return False
            self.telegram_listener = listener
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

    def stop_telegram_listener(self) -> None:
        listener = self.telegram_listener
        self.telegram_listener = None
        if listener is not None:
            listener.stop()

    def _request_remote_session_close(self, _message=None) -> str:
        self._remote_close_requested.set()
        return "RemoteAgent 已收到關閉任務指令；目前遠端工作階段將關閉。"

    def _reconcile_unclean_remote_shutdown(self, *, started_at: float) -> list[Any]:
        """Do not replay Telegram work left by a forcibly closed launcher."""
        previous = _load(self.remote_supervisor_state)
        previous_pid = int(previous.get("pid", 0) or 0)
        if (
            str(previous.get("status", "")).upper() != "RUNNING"
            or previous_pid <= 0
            or previous_pid == os.getpid()
            or self._process_alive(previous_pid)
        ):
            return []

        from .remote_events import RemoteEventStore
        from .task_state import RemoteTaskQueue, TaskStateStore, TASK_INTERRUPTED

        queue = RemoteTaskQueue(TaskStateStore(self.root / ".agents" / "remote_tasks.json"))
        changed = queue.abandon_incomplete(
            transports={"TELEGRAM", "LOCAL_TEST"},
            reason="remote_supervisor_unclean_shutdown",
            created_before=started_at,
        )
        events = RemoteEventStore(self.root / ".agents" / "remote_events.json")
        for task in changed:
            event_type = "TASK_INTERRUPTED" if task.state == TASK_INTERRUPTED else "TASK_FAILED"
            events.emit(event_type, task, status=task.state, payload={"error": task.error})
        if changed:
            self.remote_runtime_log.write(
                "ERROR",
                component="host_supervisor",
                stage="UNCLEAN_SHUTDOWN_TASKS_ABANDONED",
                previous_pid=previous_pid,
                task_ids=[task.task_id for task in changed],
            )
        return changed

    def _remote_binding(self) -> dict:
        from .conversation_registry import ConversationRegistry
        target = str(os.environ.get("SMARTAGENT_TELEGRAM_WORKSPACE", "") or "").strip()
        if not target:
            return {}
        target_path = Path(target).resolve()
        rows = []
        for row in ConversationRegistry().list_remote_conversations(enabled_only=True):
            try:
                if Path(str(row.get("workspace", "") or "")).resolve() != target_path:
                    continue
            except Exception:
                continue
            if str(row.get("purpose", "general") or "general") != "general":
                continue
            url = str(row.get("gpt_url", "") or "").strip()
            if "/c/" not in url:
                continue
            rows.append(row)
        rows.sort(key=lambda row: float(row.get("updated_at", 0.0) or 0.0), reverse=True)
        return dict(rows[0]) if rows else {}

    def _ensure_remote_browser_host(self) -> dict:
        if self._remote_browser_scraper is not None and self._remote_browser_state:
            return dict(self._remote_browser_state)
        binding = self._remote_binding()
        if not binding:
            raise RuntimeError("remote_linked_conversation_missing")
        url = str(binding["gpt_url"])
        workspace = str(binding["workspace"])
        from .web_runtime import WebLLMScraper
        scraper = WebLLMScraper("chatgpt")
        scraper.cfg = dict(scraper.cfg)
        scraper.cfg["url"] = url
        previous_attach = os.environ.get("SMARTAGENT_ATTACH_CDP")
        previous_cdp = os.environ.get("SMARTAGENT_CHATGPT_CDP")
        shared = self.live_local_agent_state()
        try:
            if shared:
                os.environ["SMARTAGENT_ATTACH_CDP"] = "1"
                os.environ["SMARTAGENT_CHATGPT_CDP"] = str(shared["cdp_endpoint"])
            else:
                os.environ.pop("SMARTAGENT_ATTACH_CDP", None)
            scraper.start()
        finally:
            if previous_attach is None: os.environ.pop("SMARTAGENT_ATTACH_CDP", None)
            else: os.environ["SMARTAGENT_ATTACH_CDP"] = previous_attach
            if previous_cdp is None: os.environ.pop("SMARTAGENT_CHATGPT_CDP", None)
            else: os.environ["SMARTAGENT_CHATGPT_CDP"] = previous_cdp
        endpoint = scraper.get_cdp_endpoint()
        self._remote_browser_scraper = scraper
        self._remote_browser_state = {
            "status": "ready", "host_pid": os.getpid(), "cdp_endpoint": endpoint,
            "workspace": workspace, "conversation_url": url,
            "supervisor_token": self.token, "startup_mode": "TASK_DEMAND",
        }
        print(f"[RemoteAgent-0] SESSION_READY {url}", flush=True)
        return dict(self._remote_browser_state)

    def _close_remote_session(self, *, reason: str = "") -> None:
        self.remote_runtime_log.write(
            "CONNECT", component="host_supervisor", stage="CLOSING_SESSION",
            reason=str(reason or "UNSPECIFIED"),
        )
        if self.agent0 is not None and self.agent0.poll() is None:
            self._terminate_agent0_tree(self.agent0)
            try: self.agent0.wait(timeout=2)
            except Exception: pass
        self.agent0 = None
        scraper = self._remote_browser_scraper
        conversation_url = str(
            self._remote_browser_state.get("conversation_url", "") or ""
        )
        self._remote_browser_scraper = None
        self._remote_browser_state = {}
        if scraper is not None:
            try:
                if conversation_url:
                    scraper.close_conversation_page(conversation_url)
            except Exception:
                pass
            try:
                scraper.close()
            except Exception:
                pass
        print("[RemoteAgent-0] STOPPED remote session; WAITING_SIGNAL",flush=True)

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
            store=RemoteTaskQueue(TaskStateStore(self.root / ".agents" / "remote_tasks.json")).store
            with store.process_lock():
                store.load()
                tasks=store.list_by_state({TASK_QUEUED, TASK_RUNNING})
            event_store=RemoteEventStore(self.root / ".agents" / "remote_events.json")
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
        if self.agent0 is not None and self.agent0.poll() is None:
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
        self.start_agent0(current)
        self.remote_runtime_log.write(
            "REQUEST_DETECTED",
            component="host_supervisor",
            stage="AGENT0_DEMAND_START",
            supervised_tasks=supervised_tasks,
        )
        return True

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

    def supervise_agent0(self, *, now: float | None = None) -> dict:
        """Restart an unexpectedly exited Agent0 with bounded exponential backoff."""
        now = float(time.time() if now is None else now)
        proc = self.agent0
        if proc is not None and proc.poll() is None:
            runtime_state = _load(self.remote_runtime_state)
            heartbeat = float(runtime_state.get("heartbeat_at", 0.0) or 0.0)
            runtime_healthy = bool(
                str(runtime_state.get("status", "")).upper() == "RUNNING"
                and int(runtime_state.get("parent_pid", 0) or 0) == os.getpid()
                and heartbeat >= self._agent0_started_at - 1.0
                and now - heartbeat <= self.agent0_heartbeat_timeout
            )
            if runtime_healthy:
                self._agent0_hang_pid = None
                return {"status": "RUNNING", "pid": runtime_state.get("runtime_pid") or getattr(proc, "pid", None)}
            if now - self._agent0_started_at <= self.agent0_heartbeat_timeout:
                return {"status": "STARTING", "pid": getattr(proc, "pid", None)}
            pid = getattr(proc, "pid", None)
            if pid != self._agent0_hang_pid:
                self._agent0_hang_pid = pid
                self.remote_runtime_log.write(
                    "ERROR", component="host_supervisor", stage="AGENT0_HEARTBEAT_TIMEOUT",
                    agent0_pid=pid, heartbeat_at=heartbeat,
                    heartbeat_timeout_sec=self.agent0_heartbeat_timeout,
                )
            stale = dict(runtime_state)
            stale.update(status="HUNG", detected_at=now)
            _atomic(self.remote_runtime_state, stale)
            self._terminate_agent0_tree(proc)
            try:
                proc.wait(timeout=2)
            except Exception:
                return {"status": "STOPPING_HUNG", "pid": pid}
        runtime_state = _load(self.remote_runtime_state)
        heartbeat = float(runtime_state.get("heartbeat_at", 0.0) or 0.0)
        if (
            str(runtime_state.get("status", "")).upper() == "RUNNING"
            and int(runtime_state.get("parent_pid", 0) or 0) == os.getpid()
            and heartbeat > 0
            and now - heartbeat <= self.agent0_heartbeat_timeout
        ):
            return {
                "status": "RUNNING_DETACHED",
                "pid": runtime_state.get("runtime_pid"),
                "launcher_exit_code": proc.poll() if proc else None,
            }
        if not self._agent0_state.get("cdp_endpoint"):
            return {"status": "NOT_STARTED"}
        if not self._agent0_dependency_available():
            return {"status": "UNSAFE_TO_RESTART", "reason": "agent1_unavailable"}

        exited_pid = getattr(proc, "pid", None)
        if exited_pid != self._agent0_exit_pid:
            self._agent0_exit_pid = exited_pid
            self._agent0_restart_count += 1
            delay = min(60.0, float(2 ** min(self._agent0_restart_count - 1, 5)))
            self._agent0_next_restart_at = now + delay
            self.remote_runtime_log.write(
                "ERROR", component="host_supervisor", stage="AGENT0_EXIT",
                agent0_pid=exited_pid, exit_code=proc.poll() if proc else None,
                restart_count=self._agent0_restart_count, restart_delay_sec=delay,
            )
        if now < self._agent0_next_restart_at:
            return {"status": "BACKOFF", "retry_at": self._agent0_next_restart_at}

        self.remote_runtime_log.write("RECONNECT", component="host_supervisor", stage="AGENT0_RESTART", restart_count=self._agent0_restart_count)
        restarted = self.start_agent0(self._agent0_state)
        return {"status": "RESTARTED", "pid": getattr(restarted, "pid", None)}

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
        if cur.get("state") in active_states:
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
            SelfRepairCoordinator(self.root / ".agents" / "self_repair", project_root=self.root).sync_meta_resume_state()
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
                    coordinator=SelfRepairCoordinator(self.root / ".agents" / "self_repair", project_root=self.root)
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

        # Bootstrap failure is already persisted as SELF_REPAIR_STALLED. Keep
        # Host alive so an external/meta repair controller can act instead of
        # collapsing the supervisor process immediately.
        runtime_exit_seen_at = 0.0
        try:
            while True:
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
            if self.agent0 and self.agent0.poll() is None:
                self.agent0.terminate()

    def run_dispatcher_only(self) -> int:
        """Shared visible Agent0 dispatcher for WebCopilot without Telegram ownership."""
        self.power_guard.acquire()
        state_path=self.root / ".agents" / "dispatcher_state.json"
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
                if pending>0 and host and (self.agent0 is None or self.agent0.poll() is not None):
                    remote_state=dict(host)
                    remote_state["startup_mode"]="TASK_DEMAND"
                    print(f"[Agent0 Dispatcher] Queued work detected: {pending}; starting Agent0.",flush=True)
                    self.start_agent0(remote_state,new_console=True)
                if self.agent0 is not None:
                    self.supervise_agent0()
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\n[Agent0 Dispatcher] Stopped.",flush=True)
            return 0
        finally:
            _atomic(state_path,{"status":"STOPPED","pid":os.getpid(),"heartbeat_at":time.time()})
            self.power_guard.release()
            if self.agent0 is not None and self.agent0.poll() is None:
                self._terminate_agent0_tree(self.agent0)

    def run_remote_only(self, *, auto_start_local: bool = False) -> int:
        """Own Telegram ingress and the RemoteAgent browser lifecycle independently."""
        started_at = time.time()
        self.power_guard.acquire()
        self._reconcile_unclean_remote_shutdown(started_at=started_at)
        self.start_telegram_listener()
        _atomic(self.remote_supervisor_state,{"status":"RUNNING","pid":os.getpid(),"heartbeat_at":time.time()})
        heartbeat_at=0.0
        waiting_logged=False
        task_cycle_active=False
        shutdown_reason="SUPERVISOR_EXIT"
        try:
            while True:
                try:
                    now=time.time()
                    if now-heartbeat_at>=5.0:
                        _atomic(self.remote_supervisor_state,{"status":"RUNNING","pid":os.getpid(),"heartbeat_at":now})
                        heartbeat_at=now
                    if self._remote_close_requested.is_set():
                        self._remote_close_requested.clear()
                        self._close_remote_session(reason="USER_REQUEST")
                        waiting_logged=False
                        task_cycle_active=False
                    pending=self.pending_remote_task_count()
                    if pending<=0:
                        if self.agent0 is not None:
                            self.supervise_agent0()
                        if task_cycle_active and self._remote_browser_state:
                            self.remote_runtime_log.write(
                                "CONNECT", component="host_supervisor",
                                stage="SESSION_RETAINED",
                                conversation_url=str(self._remote_browser_state.get("conversation_url", "") or ""),
                            )
                            task_cycle_active=False
                        if not waiting_logged:
                            print("[RemoteAgent-0] WAITING_SIGNAL",flush=True)
                            waiting_logged=True
                        time.sleep(0.25)
                        continue
                    waiting_logged=False
                    task_cycle_active=True
                    try:
                        state=self._ensure_remote_browser_host()
                    except Exception as exc:
                        self.remote_runtime_log.write("ERROR",component="host_supervisor",stage="REMOTE_BROWSER_START",error=f"{type(exc).__name__}: {exc}")
                        print(f"[RemoteAgent-0] ERROR remote browser: {exc}",flush=True)
                        time.sleep(1.0)
                        continue
                    endpoint=str(state.get("cdp_endpoint","") or "")
                    changed=bool(self._agent0_state.get("cdp_endpoint") and str(self._agent0_state.get("cdp_endpoint"))!=endpoint)
                    if changed and self.agent0 is not None and self.agent0.poll() is None:
                        self._terminate_agent0_tree(self.agent0)
                        try: self.agent0.wait(timeout=2)
                        except Exception: pass
                        self.agent0=None
                    if self.agent0 is None or self.agent0.poll() is not None:
                        state=dict(state); state["startup_mode"]="TASK_DEMAND"
                        self.start_agent0(state,new_console=False)
                    self.supervise_agent0()
                    time.sleep(0.25)
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
            _atomic(self.remote_supervisor_state,{"status":"STOPPED","pid":os.getpid(),"heartbeat_at":time.time()})
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

    if args.local_only:
        return HostSupervisor(enable_agent0=False).run()

    if args.remote_only:
        from RemoteAgent.local_telegram_config import apply_saved_telegram_environment
        telegram = apply_saved_telegram_environment(root=AGENT_PROJECT_ROOT)
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
    supervisor = HostSupervisor()
    if args.dispatcher_only:
        from .process_file_lock import exclusive_process_lock
        lock_path=AGENT_PROJECT_ROOT / ".agents" / "dispatcher.lock"
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
        lock_path=AGENT_PROJECT_ROOT / ".agents" / "remote_supervisor.lock"
        try:
            with exclusive_process_lock(lock_path,timeout_sec=0.25,label="remote supervisor",legacy_kind="remote-supervisor-sentinel-v2"):
                return supervisor.run_remote_only(auto_start_local=False)
        except RuntimeError as exc:
            if "lock timeout" in str(exc):
                print("[HostSupervisor] RemoteAgent supervisor already running.",flush=True)
                return 0
            raise
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
