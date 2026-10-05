#!/usr/bin/env python3
"""Filesystem transport for opt-in Tri-One user-signal tests.

This module replaces only the human input source.  Interface runtimes remain
responsible for routing the received message through their canonical planner,
protocol, tool execution, and completion paths.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


TEST_PROTOCOL = "TRI_ONE_TEST_V1"
TARGETS = frozenset({"local", "webdirect", "remote"})
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "ABORTED", "REJECTED"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class TestSignalError(RuntimeError):
    pass


class TestSignalTimeout(TestSignalError):
    def __init__(self, request_id: str, target_interface: str, last_runtime_state: str):
        self.request_id = str(request_id)
        self.target_interface = str(target_interface)
        self.last_runtime_state = str(last_runtime_state or "UNKNOWN")
        super().__init__(
            f"TEST_TIMEOUT request_id={self.request_id} "
            f"target_interface={self.target_interface} "
            f"last_runtime_state={self.last_runtime_state}"
        )


def _target(value: str) -> str:
    target = str(value or "").strip().lower()
    if target not in TARGETS:
        raise ValueError(f"unsupported_test_target:{target}")
    return target


def _safe_id(value: str, label: str) -> str:
    result = str(value or "").strip()
    if not result or not _SAFE_ID.fullmatch(result):
        raise ValueError(f"invalid_{label}:{result}")
    return result


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.01)


def _load_json(path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise TestSignalError(
                    f"invalid_test_signal_json:{path}:{type(exc).__name__}"
                ) from exc
            time.sleep(0.01)
        except (OSError, ValueError, TypeError) as exc:
            raise TestSignalError(f"invalid_test_signal_json:{path}:{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise TestSignalError(f"invalid_test_signal_payload:{path}")
    return value


def _process_alive(pid: int) -> bool:
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
                return bool(
                    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                ) and code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class TestEnvelope:
    test_protocol: str
    request_id: str
    target_interface: str
    generation_id: str
    message: str
    expected_completion_text: str
    timeout_seconds: float
    created_at: float
    attachment_paths: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        target_interface: str,
        generation_id: str,
        message: str,
        expected_completion_text: str = "已完成",
        timeout_seconds: float = 180.0,
        request_id: str = "",
        attachment_paths: list[str] | tuple[str, ...] | None = None,
    ) -> "TestEnvelope":
        request = _safe_id(
            request_id or ("TEST-" + uuid.uuid4().hex[:16].upper()),
            "request_id",
        )
        generation = _safe_id(generation_id, "generation_id")
        content = str(message or "").strip()
        if not content:
            raise ValueError("empty_test_message")
        timeout = max(1.0, float(timeout_seconds))
        attachments = []
        for raw in attachment_paths or ():
            path = Path(str(raw)).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"test_attachment_not_file:{path}")
            value = str(path)
            if value not in attachments:
                attachments.append(value)
        return cls(
            test_protocol=TEST_PROTOCOL,
            request_id=request,
            target_interface=_target(target_interface),
            generation_id=generation,
            message=content,
            expected_completion_text=str(expected_completion_text or "已完成"),
            timeout_seconds=timeout,
            created_at=time.time(),
            attachment_paths=tuple(attachments),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TestEnvelope":
        if str(payload.get("test_protocol", "")) != TEST_PROTOCOL:
            raise TestSignalError("unsupported_test_protocol")
        return cls.create(
            target_interface=str(payload.get("target_interface", "")),
            generation_id=str(payload.get("generation_id", "")),
            message=str(payload.get("message", "")),
            expected_completion_text=str(
                payload.get("expected_completion_text", "已完成")
            ),
            timeout_seconds=float(payload.get("timeout_seconds", 180.0)),
            request_id=str(payload.get("request_id", "")),
            attachment_paths=list(payload.get("attachment_paths") or []),
        )._replace_created_at(float(payload.get("created_at", 0.0) or 0.0))

    def _replace_created_at(self, value: float) -> "TestEnvelope":
        return TestEnvelope(**{**asdict(self), "created_at": value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TestSignalPaths:
    def __init__(self, root: str | Path, target_interface: str):
        self.root = Path(root).resolve()
        self.target = _target(target_interface)
        from .paths import tri_one_test_signals_root
        self.base = tri_one_test_signals_root(self.root) / self.target
        self.inbox = self.base / "inbox"
        self.processing = self.base / "processing"
        self.outbox = self.base / "outbox"
        self.state = self.base / "receiver_state.json"

    def ensure(self) -> None:
        for path in (self.inbox, self.processing, self.outbox):
            path.mkdir(parents=True, exist_ok=True)


class TestSignalReceiver:
    def __init__(
        self,
        root: str | Path,
        target_interface: str,
        *,
        generation_id: str = "",
    ):
        self.paths = TestSignalPaths(root, target_interface)
        self.target_interface = self.paths.target
        self.generation_id = _safe_id(
            generation_id or ("GEN-" + uuid.uuid4().hex[:16].upper()),
            "generation_id",
        )
        self.pid = os.getpid()
        self.started = False

    def _state(self, status: str) -> dict[str, Any]:
        return {
            "test_protocol": TEST_PROTOCOL,
            "target_interface": self.target_interface,
            "generation_id": self.generation_id,
            "status": str(status).upper(),
            "pid": self.pid,
            "heartbeat_at": time.time(),
        }

    def start(self) -> str:
        self.paths.ensure()
        if self.paths.state.is_file():
            previous = _load_json(self.paths.state)
            owner = int(previous.get("pid", 0) or 0)
            if (
                str(previous.get("status", "")).upper() == "RUNNING"
                and owner != self.pid
                and _process_alive(owner)
            ):
                raise TestSignalError(
                    f"test_receiver_already_running:{self.target_interface}:pid={owner}"
                )
        _atomic_json(self.paths.state, self._state("RUNNING"))
        self.started = True
        return self.generation_id

    def heartbeat(self) -> None:
        if self.started:
            _atomic_json(self.paths.state, self._state("RUNNING"))

    def _result(
        self,
        envelope: TestEnvelope,
        *,
        status: str,
        completion_text: str = "",
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "test_protocol": TEST_PROTOCOL,
            "request_id": envelope.request_id,
            "target_interface": envelope.target_interface,
            "generation_id": envelope.generation_id,
            "status": str(status).upper(),
            "completion_text": str(completion_text or ""),
            "error": str(error or ""),
            "metadata": dict(metadata or {}),
            "completed_at": time.time(),
        }
        _atomic_json(self.paths.outbox / f"{envelope.request_id}.json", result)
        processing = self.paths.processing / f"{envelope.request_id}.json"
        processing.unlink(missing_ok=True)
        return result

    def poll_one(self) -> TestEnvelope | None:
        if not self.started:
            raise TestSignalError("test_receiver_not_started")
        self.heartbeat()
        for incoming in sorted(self.paths.inbox.glob("*.json")):
            claimed = self.paths.processing / incoming.name
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    os.replace(incoming, claimed)
                    break
                except FileNotFoundError:
                    claimed = None
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        claimed = None
                        break
                    time.sleep(0.01)
            if claimed is None:
                continue
            try:
                envelope = TestEnvelope.from_dict(_load_json(claimed))
            except Exception as exc:
                claimed.unlink(missing_ok=True)
                raise TestSignalError(f"test_envelope_rejected:{type(exc).__name__}:{exc}") from exc
            if envelope.target_interface != self.target_interface:
                self._result(envelope, status="REJECTED", error="target_interface_mismatch")
                continue
            if envelope.generation_id != self.generation_id:
                self._result(envelope, status="REJECTED", error="generation_id_mismatch")
                continue
            return envelope
        return None

    def complete(
        self,
        envelope: TestEnvelope,
        *,
        status: str,
        completion_text: str = "",
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = str(status or "").upper()
        if normalized not in TERMINAL_STATUSES:
            raise ValueError(f"invalid_test_terminal_status:{normalized}")
        return self._result(
            envelope,
            status=normalized,
            completion_text=completion_text,
            error=error,
            metadata=metadata,
        )

    def close(self) -> None:
        if not self.started:
            return
        current = _load_json(self.paths.state) if self.paths.state.is_file() else {}
        if (
            int(current.get("pid", 0) or 0) == self.pid
            and str(current.get("generation_id", "")) == self.generation_id
        ):
            _atomic_json(self.paths.state, self._state("STOPPED"))
        self.started = False


class TestSignalClient:
    def __init__(self, root: str | Path, target_interface: str):
        self.paths = TestSignalPaths(root, target_interface)
        self.target_interface = self.paths.target

    def receiver_state(self) -> dict[str, Any]:
        return _load_json(self.paths.state) if self.paths.state.is_file() else {}

    def submit(
        self,
        message: str,
        *,
        expected_completion_text: str = "已完成",
        timeout_seconds: float = 180.0,
        request_id: str = "",
        attachment_paths: list[str] | tuple[str, ...] | None = None,
    ) -> TestEnvelope:
        state = self.receiver_state()
        pid = int(state.get("pid", 0) or 0)
        if str(state.get("status", "")).upper() != "RUNNING" or not _process_alive(pid):
            raise TestSignalError(
                f"test_receiver_not_running:{self.target_interface}:pid={pid}"
            )
        envelope = TestEnvelope.create(
            target_interface=self.target_interface,
            generation_id=str(state.get("generation_id", "")),
            message=message,
            expected_completion_text=expected_completion_text,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
            attachment_paths=attachment_paths,
        )
        self.paths.ensure()
        target = self.paths.inbox / f"{envelope.request_id}.json"
        if target.exists() or (self.paths.processing / target.name).exists():
            raise TestSignalError(f"duplicate_test_request_id:{envelope.request_id}")
        _atomic_json(target, envelope.to_dict())
        return envelope

    def wait(self, envelope: TestEnvelope, *, poll_interval: float = 0.1) -> dict[str, Any]:
        deadline = time.monotonic() + envelope.timeout_seconds
        result_path = self.paths.outbox / f"{envelope.request_id}.json"
        while time.monotonic() < deadline:
            if result_path.is_file():
                result = _load_json(result_path)
                identity = (
                    str(result.get("test_protocol", "")),
                    str(result.get("request_id", "")),
                    str(result.get("target_interface", "")),
                    str(result.get("generation_id", "")),
                )
                expected = (
                    TEST_PROTOCOL,
                    envelope.request_id,
                    envelope.target_interface,
                    envelope.generation_id,
                )
                if identity != expected:
                    raise TestSignalError(f"test_result_identity_mismatch:{identity!r}")
                if str(result.get("status", "")).upper() in TERMINAL_STATUSES:
                    return result
            time.sleep(max(0.01, float(poll_interval)))
        state = self.receiver_state()
        raise TestSignalTimeout(
            envelope.request_id,
            envelope.target_interface,
            str(state.get("status", "UNKNOWN")),
        )


__all__ = [
    "TEST_PROTOCOL",
    "TARGETS",
    "TERMINAL_STATUSES",
    "TestEnvelope",
    "TestSignalClient",
    "TestSignalError",
    "TestSignalPaths",
    "TestSignalReceiver",
    "TestSignalTimeout",
]
