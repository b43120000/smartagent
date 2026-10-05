#!/usr/bin/env python3
"""Windows kill-on-close process ownership for one Agent runtime."""
from __future__ import annotations

import os
import threading


class WindowsProcessJob:
    """Keep the runtime and all descendants in an OS-owned process group."""

    def __init__(self) -> None:
        self.handle = None
        self.active = False
        self.error = ""

    def activate(self) -> bool:
        if os.name != "nt":
            self.error = "not_windows"
            return False
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = (
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            )
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.IsProcessInJob.argtypes = (
                wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)
            )
            kernel32.IsProcessInJob.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

            in_job = wintypes.BOOL(False)
            if not kernel32.IsProcessInJob(
                kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)
            ):
                error = ctypes.get_last_error()
                raise OSError(error, "IsProcessInJob failed")
            if in_job.value:
                # The launcher may itself already be owned by a Windows Job.
                # A nested AssignProcessToJobObject is rejected on common
                # Windows configurations; the inherited job still provides
                # the required descendant lifetime boundary.
                self.active = True
                self.error = "inherited_job"
                return True

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # Kill normal descendants with the launcher.  The explicit
            # software-restart helper is the only child allowed to request
            # CREATE_BREAKAWAY_FROM_JOB and survive long enough to relaunch.
            info.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000800
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise OSError(error, "SetInformationJobObject failed")
            if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise OSError(error, "AssignProcessToJobObject failed")
            self.handle = handle
            self.active = True
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False


_JOB_LOCK = threading.Lock()
_PROCESS_JOB: WindowsProcessJob | None = None


def ensure_current_process_kill_job() -> WindowsProcessJob:
    """Create at most one Job Object for the current Python process."""
    global _PROCESS_JOB
    with _JOB_LOCK:
        if _PROCESS_JOB is None:
            _PROCESS_JOB = WindowsProcessJob()
            _PROCESS_JOB.activate()
        return _PROCESS_JOB


__all__ = ["WindowsProcessJob", "ensure_current_process_kill_job"]
