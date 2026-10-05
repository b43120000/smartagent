#!/usr/bin/env python3
from __future__ import annotations
import ctypes,os
from ctypes import wintypes
ES_CONTINUOUS=0x80000000
ES_SYSTEM_REQUIRED=0x00000001
ES_AWAYMODE_REQUIRED=0x00000040
PowerRequestSystemRequired=0
PowerRequestAwayModeRequired=2
REASON_CONTEXT_VERSION=0
REASON_CONTEXT_SIMPLE_STRING=1

class REASON_CONTEXT(ctypes.Structure):
    _fields_=[('Version',wintypes.ULONG),('Flags',wintypes.DWORD),('ReasonString',wintypes.LPWSTR)]

class WindowsPowerGuard:
    def __init__(self):
        self.active=False; self.last_error=''; self.handle=None; self.mode=''
        self.away_mode_active=False
    def acquire(self)->bool:
        if self.active: return True
        if os.name!='nt': self.active=True; self.mode='noop'; return True
        try:
            k32=ctypes.WinDLL('kernel32',use_last_error=True)
            k32.PowerCreateRequest.argtypes=(ctypes.POINTER(REASON_CONTEXT),)
            k32.PowerCreateRequest.restype=wintypes.HANDLE
            k32.PowerSetRequest.argtypes=(wintypes.HANDLE,wintypes.DWORD)
            k32.PowerSetRequest.restype=wintypes.BOOL
            k32.PowerClearRequest.argtypes=(wintypes.HANDLE,wintypes.DWORD)
            k32.PowerClearRequest.restype=wintypes.BOOL
            k32.SetThreadExecutionState.argtypes=(wintypes.ULONG,)
            k32.SetThreadExecutionState.restype=wintypes.ULONG
            k32.CloseHandle.argtypes=(wintypes.HANDLE,)
            k32.CloseHandle.restype=wintypes.BOOL
            ctx=REASON_CONTEXT(REASON_CONTEXT_VERSION,REASON_CONTEXT_SIMPLE_STRING,'SmartAgent background execution')
            h=k32.PowerCreateRequest(ctypes.byref(ctx))
            if h and h!=ctypes.c_void_p(-1).value:
                system_ok=bool(k32.PowerSetRequest(h,PowerRequestSystemRequired))
                away_ok=bool(k32.PowerSetRequest(h,PowerRequestAwayModeRequired))
                if system_ok:
                    self.handle=h; self.away_mode_active=away_ok
                    self.active=True; self.mode='power_request'; return True
                k32.CloseHandle(h)
            # SYSTEM_REQUIRED is the portable requirement. Away mode is not
            # supported on every Windows power policy and is not needed to
            # prevent automatic sleep while the listener is resident.
            flags=ES_CONTINUOUS|ES_SYSTEM_REQUIRED
            if not k32.SetThreadExecutionState(flags): raise OSError('SetThreadExecutionState_failed')
            self.active=True; self.mode='execution_state_fallback'; return True
        except Exception as exc:
            self.last_error=f'{type(exc).__name__}: {exc}'; return False
    def release(self)->bool:
        if os.name!='nt': self.active=False; return True
        try:
            k32=ctypes.WinDLL('kernel32',use_last_error=True)
            k32.PowerClearRequest.argtypes=(wintypes.HANDLE,wintypes.DWORD)
            k32.PowerClearRequest.restype=wintypes.BOOL
            k32.SetThreadExecutionState.argtypes=(wintypes.ULONG,)
            k32.SetThreadExecutionState.restype=wintypes.ULONG
            k32.CloseHandle.argtypes=(wintypes.HANDLE,)
            k32.CloseHandle.restype=wintypes.BOOL
            if self.handle:
                k32.PowerClearRequest(self.handle,PowerRequestSystemRequired)
                if self.away_mode_active:
                    k32.PowerClearRequest(self.handle,PowerRequestAwayModeRequired)
                k32.CloseHandle(self.handle); self.handle=None
                self.away_mode_active=False
            k32.SetThreadExecutionState(ES_CONTINUOUS)
            self.active=False; return True
        except Exception as exc:
            self.last_error=f'{type(exc).__name__}: {exc}'; return False
