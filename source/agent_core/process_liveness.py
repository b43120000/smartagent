"""Runtime-owned liveness evidence for an OS process that we launched.

A silent stdout/stderr stream never implies failure.  RUNNING is emitted only
while the Supervisor still owns the original Popen process handle (Windows) or
waitpid-backed Popen instance (POSIX).  A PID or stale snapshot alone cannot
promote an UNKNOWN execution into RUNNING.  These primitives do not detach,
increase timeouts, change ACLs, or grant new execution authority.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

SCHEMA = "SMARTAGENT_PROCESS_LIVENESS_V1"
WAIT_TIMEOUT = 0x102
WAIT_OBJECT_0 = 0


def observe_owned_process(process: subprocess.Popen, *, execution_id: str = "") -> dict[str, Any]:
    """Take an OS-backed observation using a handle owned by this Supervisor."""
    observed_at = time.time()
    state = "UNKNOWN"
    source = "UNAVAILABLE"
    exit_code = None
    try:
        if os.name == "nt":
            # A PID may be recycled.  The original Popen handle always points
            # to the exact kernel process launched by this Supervisor.
            import ctypes
            handle = getattr(process, "_handle", None)
            if not handle:
                raise OSError("owned_process_handle_missing")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            result = kernel32.WaitForSingleObject(int(handle), 0)
            source = "WIN32_WAIT_FOR_SINGLE_OBJECT"
            if result == WAIT_TIMEOUT:
                state = "RUNNING"
            elif result == WAIT_OBJECT_0:
                state = "EXITED"
                exit_code = process.poll()
            else:
                source = "WIN32_WAIT_FAILED"
        else:
            # Popen.poll() performs an OS waitpid(WNOHANG) for our own child;
            # a bare os.kill(pid,0) is intentionally NOT sufficient evidence.
            exit_code = process.poll()
            state = "RUNNING" if exit_code is None else "EXITED"
            source = "OWNED_POPEN_WAITPID"
    except (OSError, ValueError, AttributeError):
        source = "OS_LIVENESS_PROBE_FAILED"
    return {
        "schema": SCHEMA,
        "execution_id": str(execution_id)[:128],
        "process_state": state,
        "liveness_verified": state == "RUNNING",
        "process_identity_verified": state in {"RUNNING", "EXITED"},
        "evidence_source": source,
        "pid": int(process.pid) if isinstance(getattr(process, "pid", None), int) else None,
        "monitor_pid": os.getpid(),
        "observed_at": observed_at,
        "exit_code": exit_code,
        "terminal_eligible": False,
    }


def wait_with_liveness(
    process: subprocess.Popen, timeout: float,
    *, on_running: Callable[[dict], None] | None = None,
    execution_id: str = "", interval: float = 2.0,
) -> dict:
    """Bounded wait with periodic genuine liveness receipts, not an async job."""
    deadline = time.monotonic() + float(timeout)
    while True:
        observation = observe_owned_process(process, execution_id=execution_id)
        if observation["process_state"] == "EXITED":
            return observation
        # If an OS probe is inconclusive, continue the bounded wait without
        # emitting RUNNING. Never abandon a started child on probe failure.
        if observation["liveness_verified"] and on_running is not None:
            on_running(observation)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            process.wait(timeout=min(remaining, max(0.1, float(interval))))
        except subprocess.TimeoutExpired:
            continue


def communicate_with_liveness(
    process: subprocess.Popen, timeout: float,
    *, on_running: Callable[[dict], None] | None = None,
    execution_id: str = "", interval: float = 2.0,
) -> tuple[bytes, bytes]:
    """Drain both pipes without deadlocking, with intermittent handle probes.

    Repeating communicate() after TimeoutExpired is supported by subprocess.
    Output is returned once, when the direct process has finished.
    """
    deadline = time.monotonic() + float(timeout)
    while True:
        observation = observe_owned_process(process, execution_id=execution_id)
        # No RUNNING claim without OS proof; pipe draining still continues.
        if observation["liveness_verified"] and on_running:
            on_running(observation)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            out, err = process.communicate(timeout=min(remaining, max(0.1, float(interval))))
            return out or b"", err or b""
        except subprocess.TimeoutExpired:
            continue


__all__ = [
    "SCHEMA", "observe_owned_process", "wait_with_liveness",
    "communicate_with_liveness",
]
