#!/usr/bin/env python3
from __future__ import annotations
import copy,hashlib,json,os,threading,time,uuid
from contextlib import contextmanager
from dataclasses import asdict,dataclass
from pathlib import Path
from typing import Any
from .process_file_lock import exclusive_process_lock
from .json_state_io import read_json_retry, write_json_atomic

EVENT_TYPES={"TASK_ACCEPTED","TASK_STARTED","TASK_PROGRESS","TASK_COMPLETED","TASK_FAILED","TASK_INTERRUPTED","SECURITY_CONFIRMATION_REQUIRED"}
READY="READY"; DELIVERING="DELIVERING"; DELIVERED="DELIVERED"
DELIVERY_RECONCILE_MIN_AGE_SEC=30.0

def _process_started_at(pid:int)->float:
    if pid<=0: return 0.0
    if os.name=="nt":
        try:
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_=[("dwLowDateTime",wintypes.DWORD),("dwHighDateTime",wintypes.DWORD)]
            kernel32=ctypes.windll.kernel32
            handle=kernel32.OpenProcess(0x1000,False,pid)
            if not handle: return 0.0
            try:
                created=FILETIME(); exited=FILETIME(); kernel=FILETIME(); user=FILETIME()
                if not kernel32.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel),ctypes.byref(user)): return 0.0
                ticks=(int(created.dwHighDateTime)<<32)|int(created.dwLowDateTime)
                return ticks/10_000_000.0-11_644_473_600.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception: return 0.0
    return 0.0

def _delivery_owner_alive(pid:int,expected_started_at:float=0.0)->bool:
    if pid<=0: return False
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    except PermissionError: return True
    except OSError as exc:
        return getattr(exc,"winerror",None)!=87
    actual=_process_started_at(pid)
    if expected_started_at>0 and actual>0 and abs(actual-expected_started_at)>1.0: return False
    return True

@dataclass
class RemoteEvent:
    event_id:str
    event_type:str
    request_id:str
    task_id:str
    status:str
    reply_route:dict
    payload:dict
    delivery_state:str=READY
    created_at:float=0.0
    delivered_at:float=0.0
    attempts:int=0
    last_error:str=""
    delivery_owner:str=""
    delivery_owner_pid:int=0
    delivery_owner_started_at:float=0.0
    delivery_started_at:float=0.0

class RemoteEventStore:
    def __init__(self,path:Path): self.path=Path(path); self.events={}; self._lock=threading.RLock(); self.load()
    @contextmanager
    def process_lock(self,timeout_sec:float=10.0):
        lock_path=self.path.with_name(self.path.name+'.lock')
        with exclusive_process_lock(lock_path,timeout_sec=timeout_sec,label="remote event",legacy_kind="remote-event-sentinel-v2"):
            yield
    def load(self):
        if not self.path.exists(): self.events={}; return
        raw=read_json_retry(self.path); rows=raw.get('events',{}) if isinstance(raw,dict) else {}
        self.events={k:RemoteEvent(**v) for k,v in rows.items() if isinstance(v,dict)}
    def save(self):
        write_json_atomic(self.path,{'version':1,'events':{k:asdict(v) for k,v in self.events.items()}})
    def emit(self,event_type:str,task:Any,*,status:str,payload:dict|None=None)->RemoteEvent:
        if event_type not in EVENT_TYPES: raise ValueError(event_type)
        with self._lock,self.process_lock():
            self.load(); discriminator=str((payload or {}).get('approval_id','')) if event_type=='SECURITY_CONFIRMATION_REQUIRED' else ''; raw=f"{event_type}|{task.task_id}|{discriminator}"; eid='EVT-'+hashlib.sha256(raw.encode()).hexdigest()[:20].upper()
            if eid in self.events: return self.events[eid]
            route=dict(getattr(task,'reply_route',{}) or getattr(task,'metadata',{}).get('reply_route',{}) or {})
            ev=RemoteEvent(eid,event_type,str(task.request_id),str(task.task_id),str(status),route,dict(payload or {}),created_at=time.time())
            self.events[eid]=ev; self.save(); return ev
    def ready(self):
        with self._lock:
            self.load(); return [e for e in self.events.values() if e.delivery_state==READY]
    def delivering(self):
        with self._lock:
            self.load(); return [copy.deepcopy(e) for e in self.events.values() if e.delivery_state==DELIVERING]
    def claim_delivery(self,event_id:str,owner_token:str):
        with self._lock,self.process_lock():
            self.load(); ev=self.events[event_id]
            if ev.delivery_state!=READY: return None
            ev.delivery_state=DELIVERING; ev.delivery_owner=str(owner_token); ev.delivery_owner_pid=os.getpid(); ev.delivery_owner_started_at=_process_started_at(os.getpid()); ev.delivery_started_at=time.time(); ev.attempts+=1; ev.last_error=''; self.save(); return copy.deepcopy(ev)
    def mark_delivered(self,event_id:str):
        with self._lock,self.process_lock():
            self.load(); ev=self.events[event_id]; ev.delivery_state=DELIVERED; ev.delivered_at=time.time(); ev.last_error=''; ev.delivery_owner=''; ev.delivery_owner_pid=0; ev.delivery_owner_started_at=0.0; self.save(); return ev
    def mark_failed_attempt(self,event_id:str,error:str,owner_token:str=""):
        with self._lock,self.process_lock():
            self.load(); ev=self.events[event_id]
            if owner_token and ev.delivery_owner!=owner_token: return copy.deepcopy(ev)
            ev.delivery_state=READY; ev.delivery_owner=''; ev.delivery_owner_pid=0; ev.delivery_owner_started_at=0.0; ev.last_error=str(error or '')[:2000]; self.save(); return ev

    def pause_for_tasks(self, task_ids:set[str], *, reason:str) -> int:
        """Stop old task events from being retried after a runtime reset."""
        wanted = {str(value or "") for value in task_ids if str(value or "")}
        if not wanted:
            return 0
        changed = 0
        with self._lock, self.process_lock():
            self.load()
            for event in self.events.values():
                if event.task_id not in wanted or event.delivery_state == DELIVERED:
                    continue
                event.delivery_state = "PAUSED"
                event.delivery_owner = ""
                event.delivery_owner_pid = 0
                event.delivery_owner_started_at = 0.0
                event.last_error = str(reason or "remote_restart_discarded")[:2000]
                changed += 1
            if changed:
                self.save()
        return changed

class DeliveryManager:
    def __init__(self,store:RemoteEventStore,adapters:dict[str,Any]): self.store=store; self.adapters=dict(adapters); self.owner_token=uuid.uuid4().hex
    @staticmethod
    def _wire(event:RemoteEvent)->dict:
        return {'event_id':event.event_id,'event_type':event.event_type,'request_id':event.request_id,'task_id':event.task_id,'status':event.status,'payload':dict(event.payload or {}),'delivery_started_at':event.delivery_started_at}
    def deliver(self,event:RemoteEvent)->dict:
        claimed=self.store.claim_delivery(event.event_id,self.owner_token)
        if claimed is None: return {'delivered':False,'reason':'delivery_already_claimed','skipped':True}
        event=claimed
        transport=str(event.reply_route.get('transport','')).upper(); adapter=self.adapters.get(transport)
        if adapter is None:
            self.store.mark_failed_attempt(event.event_id,'missing_transport_adapter',self.owner_token); return {'delivered':False,'reason':'missing_transport_adapter'}
        try:
            result=adapter.deliver_event(event.reply_route,self._wire(event))
        except Exception as exc:
            self.store.mark_failed_attempt(event.event_id,f'{type(exc).__name__}: {exc}',self.owner_token)
            return {'delivered':False,'reason':f'{type(exc).__name__}: {exc}'}
        if result.get('delivered'): self.store.mark_delivered(event.event_id)
        else: self.store.mark_failed_attempt(event.event_id,str(result.get('reason','delivery_failed')),self.owner_token)
        return result
    def reconcile_delivering(self,transports:set[str]|None=None,should_continue=None):
        outcomes=[]
        for event in self.store.delivering():
            if callable(should_continue) and not should_continue(): break
            transport=str(event.reply_route.get('transport','')).upper()
            if transports is not None and transport not in transports: continue
            age=max(0.0,time.time()-float(event.delivery_started_at or 0.0))
            if age<DELIVERY_RECONCILE_MIN_AGE_SEC:
                outcomes.append((event.event_id,{'delivered':False,'reason':'delivery_claim_fresh','uncertain':True})); continue
            if _delivery_owner_alive(int(event.delivery_owner_pid or 0),float(event.delivery_owner_started_at or 0.0)):
                outcomes.append((event.event_id,{'delivered':False,'reason':'delivery_owner_alive','uncertain':True})); continue
            adapter=self.adapters.get(str(event.reply_route.get('transport','')).upper())
            reconcile=getattr(adapter,'reconcile_event',None) if adapter is not None else None
            if not callable(reconcile):
                outcomes.append((event.event_id,{'delivered':False,'reason':'delivery_outcome_unknown','uncertain':True})); continue
            result=reconcile(event.reply_route,self._wire(event))
            if result.get('delivered'):
                self.store.mark_delivered(event.event_id)
            elif result.get('definitive_not_delivered'):
                self.store.mark_failed_attempt(event.event_id,'reconciled_not_delivered')
            elif result.get('retry_allowed'):
                self.store.mark_failed_attempt(
                    event.event_id,
                    str(result.get('reason','delivery_outcome_unknown_retry_allowed')),
                )
            outcomes.append((event.event_id,result))
        return outcomes
    def retry_ready(self,transports:set[str]|None=None,*,should_continue=None,max_events:int|None=None):
        # Historical browser routes must never override the operator binding.
        from .remote_binding import active
        from .conversation_identity import same_conversation
        binding = active()
        if binding:
            with self.store._lock,self.store.process_lock():
                self.store.load()
                changed = False
                for event in self.store.events.values():
                    transport = str(event.reply_route.get('transport','')).upper()
                    if transport not in {'WEBGPT','WEBGPT_COPILOT'} or event.delivery_state not in {READY, DELIVERING}:
                        continue
                    if event.delivery_state == DELIVERING and _delivery_owner_alive(event.delivery_owner_pid, event.delivery_owner_started_at):
                        continue
                    reason = ''
                    if not same_conversation(binding['gpt_url'], event.reply_route.get('conversation_url','')):
                        reason = 'remote_binding_changed: historical delivery paused'
                    elif event.attempts >= 3:
                        reason = 'browser_delivery_retry_limit'
                    if reason:
                        event.delivery_state = 'PAUSED'
                        event.last_error = reason
                        changed = True
                if changed:
                    self.store.save()
        allowed={str(value).upper() for value in transports} if transports is not None else None
        outcomes=self.reconcile_delivering(allowed,should_continue=should_continue)
        delivered_count=0
        limit=None if max_events is None else max(0,int(max_events))
        for event in list(self.store.ready()):
            if limit is not None and delivered_count>=limit: break
            if callable(should_continue) and not should_continue(): break
            transport=str(event.reply_route.get('transport','')).upper()
            if allowed is not None and transport not in allowed: continue
            if (binding and transport in {'WEBGPT','WEBGPT_COPILOT'}
                    and event.attempts > 0 and time.time() - event.delivery_started_at < 30):
                continue
            outcomes.append((event.event_id,self.deliver(event)))
            delivered_count+=1
        return outcomes
