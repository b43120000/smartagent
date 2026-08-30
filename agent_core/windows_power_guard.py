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
    def __init__(self): self.active=False; self.last_error=''; self.handle=None; self.mode=''
    def acquire(self)->bool:
        if os.name!='nt': self.active=True; self.mode='noop'; return True
        try:
            k32=ctypes.windll.kernel32
            ctx=REASON_CONTEXT(REASON_CONTEXT_VERSION,REASON_CONTEXT_SIMPLE_STRING,'SmartAgent background execution')
            h=k32.PowerCreateRequest(ctypes.byref(ctx))
            if h and h!=wintypes.HANDLE(-1).value:
                system_ok=bool(k32.PowerSetRequest(h,PowerRequestSystemRequired))
                away_ok=bool(k32.PowerSetRequest(h,PowerRequestAwayModeRequired))
                if system_ok:
                    self.handle=h; self.active=True; self.mode='power_request'; return True
                k32.CloseHandle(h)
            flags=ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED
            if not k32.SetThreadExecutionState(flags): raise OSError('SetThreadExecutionState_failed')
            self.active=True; self.mode='execution_state_fallback'; return True
        except Exception as exc:
            self.last_error=f'{type(exc).__name__}: {exc}'; return False
    def release(self)->bool:
        if os.name!='nt': self.active=False; return True
        try:
            k32=ctypes.windll.kernel32
            if self.handle:
                k32.PowerClearRequest(self.handle,PowerRequestSystemRequired)
                k32.PowerClearRequest(self.handle,PowerRequestAwayModeRequired)
                k32.CloseHandle(self.handle); self.handle=None
            k32.SetThreadExecutionState(ES_CONTINUOUS)
            self.active=False; return True
        except Exception as exc:
            self.last_error=f'{type(exc).__name__}: {exc}'; return False
