#!/usr/bin/env python3
"""Crash-safe cross-process file locks with a permanent kernel-lock inode."""
from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover
    import fcntl

_UPGRADE_KIND = "smartagent-kernel-lock-v2"


def _pid_alive(pid: int) -> bool:
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
                    and code.value == 259
                )
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _try_lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _fallback_kernel_lock_path(marker: Path) -> Path:
    """Return one shared writable lock path when the marker directory ACL is stale."""
    key = hashlib.sha256(str(marker.resolve()).encode("utf-8", errors="replace")).hexdigest()
    # Keep the fallback beside the workspace, rather than under the affected
    # runtime directory or the user temp tree whose inherited ACL may carry
    # the same deny entry.
    workspace = marker.resolve()
    parts = list(workspace.parts)
    try:
        agents_index = parts.index(".agents")
    except ValueError:
        agents_index = -1
    if agents_index > 0:
        workspace = Path(*parts[:agents_index])
    else:
        workspace = workspace.parent
    fallback_dir = workspace / ".smartagent_kernel_locks"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    return fallback_dir / f"smartagent-kernel-lock-{key}.v2"


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        fcntl.flock(fd, fcntl.LOCK_UN)


def _ensure_upgrade_marker(marker: Path, deadline: float, label: str, legacy_kind: str) -> None:
    # The immediately preceding implementation recognizes legacy_kind + a
    # live PID. PID 4 (Windows System) / PID 1 (POSIX init) intentionally
    # blocks that in-memory code from deleting this permanent upgrade marker.
    guard_pid=4 if os.name=="nt" else 1
    payload=json.dumps({"lock_kind":legacy_kind,"upgrade_kind":_UPGRADE_KIND,"pid":guard_pid,"created_at":time.time()},separators=(",",":"))
    while True:
        try:
            raw=marker.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raw=""
            current=None
        except (OSError,UnicodeError):
            raw=""
            current={}
        else:
            try:
                current=json.loads(raw)
            except json.JSONDecodeError:
                current={}
            # TaskStateStore v1 wrote only the owning PID. A raw integer is
            # valid JSON, so normalize both parsed integers and plain numeric
            # text into the legacy-PID shape before checking liveness.
            if isinstance(current,int):
                current={"pid":int(current),"raw_pid_marker":True}
            elif current == {} and raw:
                try:
                    current={"pid":int(raw),"raw_pid_marker":True}
                except ValueError:
                    pass
        if isinstance(current,dict) and current.get("upgrade_kind")==_UPGRADE_KIND:
            return
        if isinstance(current,dict) and int(current.get("pid",0) or 0)>0:
            legacy_pid=int(current["pid"])
            if _pid_alive(legacy_pid):
                if time.time()>=deadline:
                    raise RuntimeError(
                        f"{label} legacy lock is still owned by PID {legacy_pid}: {marker}"
                    )
                time.sleep(0.05)
                continue
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                if time.time()>=deadline:
                    raise RuntimeError(f"{label} stale legacy lock cannot be removed: {marker}")
                time.sleep(0.05)
            continue
        if current == {} and marker.exists():
            if time.time()>=deadline:
                raise RuntimeError(f"{label} malformed legacy lock requires inspection: {marker}")
            time.sleep(0.05)
            continue
        try:
            fd=os.open(str(marker),os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except (FileExistsError,PermissionError):
            if time.time()>=deadline:
                raise RuntimeError(f"{label} legacy lock requires all old Agent processes to stop: {marker}")
            time.sleep(0.05)
            continue
        try:
            try:
                os.write(fd,payload.encode("utf-8")); os.fsync(fd)
            finally:
                os.close(fd)
            return
        except Exception:
            try: marker.unlink()
            except OSError: pass
            raise


@contextmanager
def exclusive_process_lock(marker_path: str|Path, *, timeout_sec: float=10.0, label: str="process", legacy_kind: str="smartagent-legacy-lock-v1"):
    """Acquire a non-deleting OS lock; hard process exit releases it safely."""
    marker=Path(marker_path); marker.parent.mkdir(parents=True,exist_ok=True)
    deadline=time.time()+max(0.1,float(timeout_sec))
    _ensure_upgrade_marker(marker,deadline,label,legacy_kind)
    kernel_path=marker.with_name(marker.name+".v2")
    try:
        fd=os.open(str(kernel_path),os.O_CREAT|os.O_RDWR)
    except PermissionError:
        # A legacy lock may retain an ACL deny after its owner exited. Use a
        # fresh generation lock so startup can recover without deleting user data.
        kernel_path = marker.with_name(marker.name + ".v3")
        try:
            fd=os.open(str(kernel_path),os.O_CREAT|os.O_RDWR)
        except PermissionError:
            # Some Windows launchers inherit a sandbox ACL that denies all new
            # files under the old runtime directory. Keep the lock identity
            # deterministic across processes, but place only the kernel lock
            # in the shared user temp directory. The legacy marker remains the
            # audit/upgrade record and is never deleted here.
            kernel_path = _fallback_kernel_lock_path(marker)
            fd=os.open(str(kernel_path),os.O_CREAT|os.O_RDWR)
    try:
        if os.fstat(fd).st_size==0:
            os.write(fd,b" ")
        while not _try_lock(fd):
            if time.time()>=deadline:
                raise RuntimeError(f"{label} lock timeout: {kernel_path}")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
