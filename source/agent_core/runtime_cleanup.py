#!/usr/bin/env python3
"""Tri-One launcher cleanup, runtime generation, and shutdown ownership."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .process_file_lock import _pid_alive, exclusive_process_lock
from .runtime_events import RuntimeEventWriter
from .windows_process_job import ensure_current_process_kill_job
from .paths import (agent_host_state_path, install_root, remote_runtime_state_path, remote_supervisor_state_path, source_root, telegram_listener_state_path, tri_one_runtime_root, tri_one_test_signals_root, webgpt_submit_lock_path)


RUNTIME_PROTOCOL = "TRI_ONE_RUNTIME_V1"
INTERFACES = frozenset({"local", "webdirect", "remote"})
_SAFE_GENERATION = re.compile(r"^[A-Za-z0-9_.-]+$")


def _interface(value: str) -> str:
    result = str(value or "").strip().lower()
    if result not in INTERFACES:
        raise ValueError(f"unsupported_runtime_interface:{result}")
    return result


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.01)


def _load_json(path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except PermissionError:
            if time.monotonic() >= deadline:
                return {}
            time.sleep(0.01)
        except (OSError, ValueError, TypeError):
            return {}


def _valid_generation(value: str) -> str:
    result = str(value or "").strip()
    if not result or not _SAFE_GENERATION.fullmatch(result):
        raise ValueError(f"invalid_runtime_generation:{result}")
    return result


class RuntimeCleanupManager:
    """Own only ephemeral state for one named Tri-One interface."""

    def __init__(self, root: str | Path, interface: str):
        self.root = Path(root).resolve()
        self.interface = _interface(interface)
        self.base = tri_one_runtime_root(self.root) / self.interface
        self.state_path = self.base / "state.json"
        self.cancel_path = self.base / "cancel.json"
        self.monitor_state_path = self.base / "monitor_state.json"
        self.startup_lock_path = self.base / "startup.lock"
        self.test_base = (
            tri_one_test_signals_root(self.root) / self.interface
        )
        self.events = RuntimeEventWriter(self.root)

    def read_state(self) -> dict[str, Any]:
        return _load_json(self.state_path)

    def read_monitor_state(self) -> dict[str, Any]:
        return _load_json(self.monitor_state_path)

    def _remote_owner_is_healthy(self, owner_pid: int, generation_id: str) -> bool:
        """Return true only when the persisted remote owner is actually serving.

        A live PID alone is not sufficient: a detached Python child can survive
        after the receiver has stopped and leave state.json looking RUNNING.
        Startup must distinguish that stale/inconsistent case from a real
        duplicate runtime.
        """
        if self.interface != "remote" or owner_pid <= 0 or not _pid_alive(owner_pid):
            return False
        monitor = self.read_monitor_state()
        monitor_pid = int(monitor.get("pid", 0) or 0)
        monitor_age = time.time() - float(monitor.get("heartbeat_at", 0.0) or 0.0)
        if not (
            str(monitor.get("generation_id", "")) == generation_id
            and str(monitor.get("status", "")).upper() == "RUNNING"
            and monitor_pid > 0
            and _pid_alive(monitor_pid)
            and monitor_age <= 10.0
        ):
            return False
        for name in ("remote_supervisor_state.json", "telegram_listener_state.json"):
            component = _load_json(remote_supervisor_state_path(self.root) if name == "remote_supervisor_state.json" else telegram_listener_state_path(self.root))
            component_pid = int(component.get("pid", component.get("runtime_pid", 0)) or 0)
            component_age = time.time() - float(component.get("heartbeat_at", 0.0) or 0.0)
            if not (
                str(component.get("status", "")).upper() in {"RUNNING", "DEGRADED"}
                and component_pid == owner_pid
                and component_age <= 10.0
            ):
                return False
        return True

    @staticmethod
    def _terminate_stale_owner(pid: int) -> bool:
        """Terminate one known stale runtime owner and its direct children."""
        if pid <= 0 or not _pid_alive(pid):
            return False
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                deadline = time.monotonic() + 3.0
                while _pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                return not _pid_alive(pid)
        except OSError:
            pass
        return False

    @staticmethod
    def _mark_dead_owner_stopped(path: Path, *, reason: str) -> bool:
        payload = _load_json(path)
        if not payload:
            return False
        status = str(payload.get("status", "") or "").upper()
        if status not in {"RUNNING", "READY", "BUSY", "IDLE", "STOPPING", "HUNG"}:
            return False
        pid = int(
            payload.get("runtime_pid", 0)
            or payload.get("host_pid", 0)
            or payload.get("pid", 0)
            or 0
        )
        if pid > 0 and _pid_alive(pid):
            return False
        now = time.time()
        payload.update(
            status="STOPPED",
            heartbeat_at=now,
            stopped_at=now,
            invalidated_at=now,
            shutdown_reason=str(reason or "runtime_owner_lost"),
        )
        _atomic_json(path, payload)
        return True

    def finalize_abandoned_runtime(self, generation_id: str, *, reason: str) -> bool:
        """Converge stale state after the owning CMD/runtime disappears."""
        generation = _valid_generation(generation_id)
        state = self.read_state()
        if str(state.get("generation_id", "")) != generation:
            return False
        owner_pid = int(state.get("pid", 0) or 0)
        if owner_pid > 0 and _pid_alive(owner_pid):
            return False
        now = time.time()
        state.update(
            status="STOPPED",
            heartbeat_at=now,
            stopped_at=now,
            invalidated_at=now,
            shutdown_reason=str(reason or "runtime_owner_lost"),
        )
        _atomic_json(self.state_path, state)
        self._mark_dead_owner_stopped(
            self.monitor_state_path,
            reason=reason,
        )
        if self.interface == "remote":
            for name in (
                "remote_supervisor_state.json",
                "remote_runtime_state.json",
                "telegram_listener_state.json",
            ):
                self._mark_dead_owner_stopped(
                    remote_supervisor_state_path(self.root) if name == "remote_supervisor_state.json" else (remote_runtime_state_path(self.root) if name == "remote_runtime_state.json" else telegram_listener_state_path(self.root)),
                    reason=reason,
                )
        elif self.interface == "local":
            self._mark_dead_owner_stopped(
                agent_host_state_path(self.root),
                reason=reason,
            )
        self.events.emit(
            self.interface,
            generation,
            "STOPPED",
            message=str(reason or "runtime_owner_lost"),
        )
        return True

    def abort_prepared_startup(self, generation_id: str, *, reason: str) -> bool:
        """Close a PREPARED generation whose owner never attached."""
        generation = _valid_generation(generation_id)
        state = self.read_state()
        if (
            str(state.get("generation_id", "")) != generation
            or str(state.get("status", "")).upper() != "PREPARED"
            or int(state.get("pid", 0) or 0) > 0
        ):
            return False
        now = time.time()
        state.update(
            status="STOPPED",
            heartbeat_at=now,
            stopped_at=now,
            invalidated_at=now,
            shutdown_reason=str(reason or "runtime_startup_aborted"),
        )
        _atomic_json(self.state_path, state)
        self.events.emit(self.interface, generation, "STARTUP_ABORTED", message=str(reason))
        return True

    @staticmethod
    def recover_stale_owned_file(path: str | Path, *, grace_sec: float = 30.0) -> bool:
        """Remove only a dead-owner or old malformed transient marker."""
        target = Path(path)
        if not target.exists():
            return False
        payload = _load_json(target)
        pid = int(payload.get("pid", 0) or 0)
        try:
            age = max(0.0, time.time() - target.stat().st_mtime)
        except OSError:
            return False
        stale = (pid > 0 and not _pid_alive(pid)) or (pid <= 0 and age > grace_sec)
        if not stale:
            return False
        try:
            target.unlink()
            return True
        except (FileNotFoundError, OSError):
            return False

    def _abort_test_signal(self, source: Path, reason: str) -> bool:
        payload = _load_json(source)
        request_id = str(payload.get("request_id", "") or "").strip()
        generation_id = str(payload.get("generation_id", "") or "").strip()
        if not request_id or not generation_id:
            source.unlink(missing_ok=True)
            return True
        outbox = self.test_base / "outbox" / f"{request_id}.json"
        _atomic_json(
            outbox,
            {
                "test_protocol": "TRI_ONE_TEST_V1",
                "request_id": request_id,
                "target_interface": self.interface,
                "generation_id": generation_id,
                "status": "ABORTED",
                "completion_text": "",
                "error": str(reason),
                "completed_at": time.time(),
            },
        )
        source.unlink(missing_ok=True)
        return True

    def abort_pending_test_signals(
        self,
        *,
        reason: str,
        generation_id: str = "",
    ) -> int:
        count = 0
        for folder_name in ("inbox", "processing"):
            folder = self.test_base / folder_name
            if not folder.is_dir():
                continue
            for source in list(folder.glob("*.json")):
                payload = _load_json(source)
                if generation_id and str(payload.get("generation_id", "")) != generation_id:
                    continue
                if self._abort_test_signal(source, reason):
                    count += 1
        return count

    def _clean_old_test_results(self) -> int:
        outbox = self.test_base / "outbox"
        if not outbox.is_dir():
            return 0
        count = 0
        for path in list(outbox.glob("*.json")):
            try:
                path.unlink()
                count += 1
            except (FileNotFoundError, OSError):
                continue
        return count

    def prepare(self) -> str:
        # Validate the tracked v8 source tree before publishing PREPARED or
        # launching a monitor.  Otherwise a manifest failure in the child host
        # leaves a healthy monitor guarding a generation that can never attach.
        if (install_root(self.root) / "source" / "agent_core" / "protocol_manifest.py").is_file():
            from .protocol_manifest import require_protocol_manifest

            require_protocol_manifest(install_root(self.root))
        self.base.mkdir(parents=True, exist_ok=True)
        with exclusive_process_lock(
            self.startup_lock_path,
            timeout_sec=2.0,
            label=f"{self.interface} startup",
            legacy_kind="tri-one-startup-v1",
        ):
            previous = self.read_state()
            previous_pid = int(previous.get("pid", 0) or 0)
            if (
                str(previous.get("status", "")).upper() in {"RUNNING", "READY"}
                and previous_pid > 0
                and _pid_alive(previous_pid)
            ):
                previous_generation = str(previous.get("generation_id", "") or "")
                if self.interface != "remote" or self._remote_owner_is_healthy(
                    previous_pid, previous_generation
                ):
                    raise RuntimeError(
                        f"runtime_already_running:{self.interface}:pid={previous_pid}"
                    )
                if self.interface == "remote" and self._terminate_stale_owner(previous_pid):
                    self.finalize_abandoned_runtime(
                        previous_generation,
                        reason="prestart_cleanup_inconsistent_remote_owner",
                    )
                else:
                    raise RuntimeError(
                        f"runtime_owner_not_cleanable:{self.interface}:pid={previous_pid}"
                    )
            if str(previous.get("status", "")).upper() == "PREPARED":
                prepared_age = time.time() - float(previous.get("prepared_at", 0.0) or 0.0)
                monitor = self.read_monitor_state()
                monitor_matches = str(monitor.get("generation_id", "")) == str(
                    previous.get("generation_id", "")
                )
                monitor_pid = int(monitor.get("pid", 0) or 0)
                monitor_age = time.time() - float(monitor.get("heartbeat_at", 0.0) or 0.0)
                monitor_healthy = bool(
                    monitor_matches
                    and str(monitor.get("status", "")).upper() == "RUNNING"
                    and monitor_pid > 0
                    and _pid_alive(monitor_pid)
                    and monitor_age <= 5.0
                )
                if prepared_age < 15.0 and monitor_healthy:
                    raise RuntimeError(f"runtime_startup_in_progress:{self.interface}")
                self.abort_prepared_startup(
                    str(previous.get("generation_id", "")),
                    reason=(
                        "prestart_cleanup_stale_monitor"
                        if prepared_age < 15.0
                        else "prestart_cleanup_stale_prepared"
                    ),
                )

            previous_generation = str(previous.get("generation_id", "") or "")
            previous_status = str(previous.get("status", "") or "").upper()
            if (
                previous_generation
                and previous_status in {"RUNNING", "READY", "FAILED", "STOPPING"}
                and previous_pid > 0
                and not _pid_alive(previous_pid)
            ):
                self.finalize_abandoned_runtime(
                    previous_generation,
                    reason="prestart_cleanup_dead_owner",
                )

            aborted = self.abort_pending_test_signals(
                reason="prestart_cleanup_stale_signal"
            )
            removed_results = self._clean_old_test_results()
            monitor = self.read_monitor_state()
            monitor_pid = int(monitor.get("pid", 0) or 0)
            if monitor_pid <= 0 or not _pid_alive(monitor_pid):
                self.monitor_state_path.unlink(missing_ok=True)

            remote_clean = {"tasks_discarded": 0, "events_paused": 0, "ownerships_discarded": 0}
            if self.interface == "remote":
                from .remote_clean_start import remote_clean_start

                remote_clean = remote_clean_start(self.root, reason="REMOTE_CLEAN_START:PREPARE")
            else:
                try:
                    from .security_approval import SecurityApprovalLedger
                    SecurityApprovalLedger(self.root).clear_pending(reason="RUNTIME_CLEAN_START:PREPARE")
                except (OSError, RuntimeError, ValueError):
                    pass

            # This lock is shared by all interfaces.  Never remove a live
            # owner; process_file_lock .v2 marker files are permanent and are
            # intentionally excluded.
            recovered_submit_lock = self.recover_stale_owned_file(
                webgpt_submit_lock_path(self.root)
            )
            generation = "GEN-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8].upper()
            self.cancel_path.unlink(missing_ok=True)
            _atomic_json(
                self.state_path,
                {
                    "runtime_protocol": RUNTIME_PROTOCOL,
                    "interface": self.interface,
                    "generation_id": generation,
                    "status": "PREPARED",
                    "pid": 0,
                    "prepared_by_pid": os.getpid(),
                    "prepared_at": time.time(),
                    "heartbeat_at": time.time(),
                },
            )
            self.events.emit(
                self.interface,
                generation,
                "PRESTART_CLEANUP",
                aborted_test_signals=aborted,
                removed_test_results=removed_results,
                recovered_submit_lock=recovered_submit_lock,
                remote_clean_start=remote_clean,
            )
            self.events.emit(self.interface, generation, "GENERATION_CREATED")
            return generation

    def finalize_launcher_exit(self, generation_id: str, *, reason: str) -> bool:
        """Converge lifecycle state immediately after the launched host exits."""
        generation = _valid_generation(generation_id)
        state = self.read_state()
        if str(state.get("generation_id", "")) != generation:
            return False
        status = str(state.get("status", "") or "").upper()
        owner_pid = int(state.get("pid", 0) or 0)
        if status == "PREPARED" and owner_pid <= 0:
            return self.abort_prepared_startup(
                generation, reason=str(reason or "launcher_child_failed_before_attach")
            )
        if status in {"RUNNING", "READY", "FAILED", "STOPPING"} and (
            owner_pid <= 0 or not _pid_alive(owner_pid)
        ):
            return self.finalize_abandoned_runtime(
                generation, reason=str(reason or "launcher_child_exited")
            )
        return status == "STOPPED"

    def request_shutdown(self, generation_id: str, *, reason: str) -> bool:
        generation = _valid_generation(generation_id)
        state = self.read_state()
        if str(state.get("generation_id", "")) != generation:
            return False
        _atomic_json(
            self.cancel_path,
            {
                "runtime_protocol": RUNTIME_PROTOCOL,
                "interface": self.interface,
                "generation_id": generation,
                "reason": str(reason or "shutdown_requested"),
                "requested_at": time.time(),
                "pid": os.getpid(),
            },
        )
        state.update(status="STOPPING", shutdown_reason=str(reason), heartbeat_at=time.time())
        _atomic_json(self.state_path, state)
        self.abort_pending_test_signals(
            reason=str(reason or "shutdown_requested"),
            generation_id=generation,
        )
        self.events.emit(
            self.interface,
            generation,
            "SHUTDOWN_CLEANUP",
            message=str(reason or "shutdown_requested"),
        )
        return True

    def cancelled(self, generation_id: str) -> bool:
        payload = _load_json(self.cancel_path)
        return bool(
            str(payload.get("generation_id", "")) == str(generation_id)
            and str(payload.get("interface", "")) == self.interface
        )


class RuntimeLifecycle:
    def __init__(self, root: str | Path, interface: str, generation_id: str):
        self.manager = RuntimeCleanupManager(root, interface)
        self.interface = self.manager.interface
        self.generation_id = _valid_generation(generation_id)
        self.pid = os.getpid()
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._monitor_seen = False
        self.closed = False
        self._process_job = None

    @classmethod
    def from_environment(
        cls,
        root: str | Path,
        interface: str,
    ) -> "RuntimeLifecycle | None":
        generation = str(os.environ.get("SMARTAGENT_RUNTIME_GENERATION", "") or "").strip()
        if not generation:
            return None
        expected_interface = str(
            os.environ.get("SMARTAGENT_RUNTIME_INTERFACE", interface) or interface
        ).strip().lower()
        if expected_interface != _interface(interface):
            raise RuntimeError(
                f"runtime_interface_mismatch:{expected_interface}!={interface}"
            )
        lifecycle = cls(root, interface, generation)
        try:
            lifecycle.attach()
        except Exception as exc:
            lifecycle.manager.abort_prepared_startup(
                generation,
                reason=f"runtime_attach_failed:{type(exc).__name__}",
            )
            raise
        return lifecycle

    def attach(self) -> None:
        state = self.manager.read_state()
        if str(state.get("generation_id", "")) != self.generation_id:
            raise RuntimeError("runtime_generation_not_prepared")
        previous_pid = int(state.get("pid", 0) or 0)
        if previous_pid and previous_pid != self.pid and _pid_alive(previous_pid):
            raise RuntimeError(f"runtime_generation_owned_by_live_pid:{previous_pid}")
        if os.environ.get("SMARTAGENT_RUNTIME_MONITOR_REQUIRED", "0") == "1":
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                monitor = self.manager.read_monitor_state()
                if (
                    str(monitor.get("generation_id", "")) == self.generation_id
                    and str(monitor.get("status", "")).upper() == "RUNNING"
                    and _pid_alive(int(monitor.get("pid", 0) or 0))
                ):
                    self._monitor_seen = True
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("runtime_monitor_not_ready")
        self._process_job = ensure_current_process_kill_job()
        if os.name == "nt" and not self._process_job.active:
            raise RuntimeError(
                f"runtime_process_job_unavailable:{self._process_job.error}"
            )
        state.update(
            runtime_protocol=RUNTIME_PROTOCOL,
            interface=self.interface,
            generation_id=self.generation_id,
            status="RUNNING",
            pid=self.pid,
            started_at=time.time(),
            heartbeat_at=time.time(),
            process_job_active=bool(self._process_job.active),
            process_job_error=str(self._process_job.error or ""),
        )
        _atomic_json(self.manager.state_path, state)
        self.manager.events.emit(self.interface, self.generation_id, "INITIALIZING")
        self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        def beat() -> None:
            while not self._stop.wait(2.0):
                state = self.manager.read_state()
                if str(state.get("generation_id", "")) != self.generation_id:
                    self._stop.set()
                    return
                if str(state.get("status", "")).upper() == "STOPPING":
                    continue
                state.update(status=state.get("status", "RUNNING"), pid=self.pid, heartbeat_at=time.time())
                _atomic_json(self.manager.state_path, state)

        self._heartbeat_thread = threading.Thread(
            target=beat,
            name=f"tri-one-{self.interface}-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def emit(self, state: str, *, request_id: str = "", message: str = "", **detail: Any) -> None:
        self.manager.events.emit(
            self.interface,
            self.generation_id,
            state,
            request_id=request_id,
            message=message,
            **detail,
        )

    def ready(self, waiting_state: str) -> None:
        state = self.manager.read_state()
        if str(state.get("generation_id", "")) != self.generation_id:
            return
        state.update(status="READY", waiting_state=str(waiting_state), heartbeat_at=time.time())
        _atomic_json(self.manager.state_path, state)
        self.emit("READY")
        self.emit(waiting_state)

    def bootstrap_failed(self, reason: str, *, detail: str = "") -> None:
        """Publish a terminal bootstrap result without claiming runtime readiness."""
        state = self.manager.read_state()
        if str(state.get("generation_id", "")) != self.generation_id:
            return
        state.update(
            status="FAILED",
            waiting_state="",
            failure_reason=str(reason),
            failure_detail=str(detail),
            heartbeat_at=time.time(),
        )
        _atomic_json(self.manager.state_path, state)
        self.emit("SELF_REPAIR_STALLED", message=str(reason), detail=str(detail))

    def shutdown_requested(self) -> bool:
        if self.manager.cancelled(self.generation_id):
            return True
        monitor = self.manager.read_monitor_state()
        if str(monitor.get("generation_id", "")) == self.generation_id:
            self._monitor_seen = True
            monitor_pid = int(monitor.get("pid", 0) or 0)
            age = max(0.0, time.time() - float(monitor.get("heartbeat_at", 0.0) or 0.0))
            if (
                str(monitor.get("status", "")).upper() != "RUNNING"
                or monitor_pid <= 0
                or not _pid_alive(monitor_pid)
                or age > 10.0
            ):
                self.manager.request_shutdown(
                    self.generation_id,
                    reason="monitor_closed",
                )
                return True
        return False

    def close(self, *, reason: str = "runtime_exit") -> None:
        if self.closed:
            return
        self.closed = True
        self.manager.request_shutdown(self.generation_id, reason=reason)
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.5)
        state = self.manager.read_state()
        if str(state.get("generation_id", "")) == self.generation_id:
            state.update(
                status="STOPPED",
                pid=self.pid,
                stopped_at=time.time(),
                invalidated_at=time.time(),
                heartbeat_at=time.time(),
            )
            _atomic_json(self.manager.state_path, state)
        self.emit("STOPPED", message=reason)


def runtime_cancel_requested(root: str | Path) -> bool:
    interface = str(os.environ.get("SMARTAGENT_RUNTIME_INTERFACE", "") or "").strip()
    generation = str(os.environ.get("SMARTAGENT_RUNTIME_GENERATION", "") or "").strip()
    if not interface or not generation:
        return False
    try:
        return RuntimeCleanupManager(root, interface).cancelled(generation)
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tri-One runtime lifecycle utility")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--interface", choices=sorted(INTERFACES), required=True)
    prepare.add_argument("--root", default=str(source_root()))
    recover = sub.add_parser("recover-lock")
    recover.add_argument("--path", required=True)
    shutdown = sub.add_parser("shutdown")
    shutdown.add_argument("--interface", choices=sorted(INTERFACES), required=True)
    shutdown.add_argument("--generation", default="")
    shutdown.add_argument("--root", default=str(source_root()))
    finalize = sub.add_parser("finalize-exit")
    finalize.add_argument("--interface", choices=sorted(INTERFACES), required=True)
    finalize.add_argument("--generation", required=True)
    finalize.add_argument("--reason", default="launcher_child_exited")
    finalize.add_argument("--root", default=str(source_root()))
    args = parser.parse_args(argv)
    if args.command == "prepare":
        try:
            print(RuntimeCleanupManager(args.root, args.interface).prepare())
            return 0
        except Exception as exc:
            print(
                f"runtime_prepare_failed:{type(exc).__name__}:{exc}",
                file=sys.stderr,
            )
            return 1
    if args.command == "finalize-exit":
        finalized = RuntimeCleanupManager(args.root, args.interface).finalize_launcher_exit(
            args.generation,
            reason=args.reason,
        )
        print(
            f"runtime_launcher_exit_finalized:{args.interface}:{args.generation}:"
            f"{str(finalized).lower()}"
        )
        return 0 if finalized else 1
    if args.command == "shutdown":
        manager = RuntimeCleanupManager(args.root, args.interface)
        generation = str(args.generation or manager.read_state().get("generation_id", ""))
        if not generation or not manager.request_shutdown(
            generation, reason="test_gate_complete"
        ):
            print(f"runtime_shutdown_not_requested:{args.interface}")
            return 1
        print(f"runtime_shutdown_requested:{args.interface}:{generation}")
        return 0
    RuntimeCleanupManager.recover_stale_owned_file(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INTERFACES",
    "RUNTIME_PROTOCOL",
    "RuntimeCleanupManager",
    "RuntimeLifecycle",
    "runtime_cancel_requested",
]
