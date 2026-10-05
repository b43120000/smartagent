#!/usr/bin/env python3
"""Durable exactly-once checkpoints for SmartAgent task/tool boundaries."""
from __future__ import annotations
import json, os, threading, time, hashlib
from pathlib import Path
from typing import Any
from .workspace import AGENT_PROJECT_ROOT
from .paths import self_repair_root

NOT_STARTED="NOT_STARTED"; STARTED_UNCONFIRMED="STARTED_UNCONFIRMED"; COMMITTED="COMMITTED"
DEFAULT_DIR=self_repair_root()/"checkpoints"

class TaskCheckpointStore:
    def __init__(self, root: str|Path=DEFAULT_DIR): self.root=Path(root); self._lock=threading.RLock()
    def path_for(self, run_id: str)->Path: return self.root/f"{run_id}.json"
    def load(self, run_id: str)->dict:
        try:
            data=json.loads(self.path_for(run_id).read_text(encoding="utf-8")); return data if isinstance(data,dict) else {}
        except Exception: return {}
    def _save(self, data:dict)->dict:
        self.root.mkdir(parents=True,exist_ok=True); p=self.path_for(data["run_id"]); tmp=p.with_name(p.name+f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8"); tmp.replace(p); return dict(data)
    def begin_task(self, run_id:str, *, goal:str="", workspace:str="", route_context:dict|None=None)->dict:
        with self._lock:
            data={"version":1,"run_id":run_id,"goal":goal,"workspace":workspace,"route_context":route_context or {},"iteration":0,"task_state":"RUNNING","pause_reason":"","actions":{},"updated_at":time.time()}; return self._save(data)
    def prepare_action(self, run_id:str, *, iteration:int, action_id:str, tool:str, signature:str)->dict:
        with self._lock:
            d=self.load(run_id); d.update(iteration=iteration,updated_at=time.time()); actions=d.setdefault("actions",{}); existing=actions.get(action_id)
            if existing:
                if existing.get("signature") != signature: raise ValueError(f"action signature conflict: {action_id}")
                return dict(d)
            actions[action_id]={"tool":tool,"signature":signature,"boundary":NOT_STARTED,"result":None,"updated_at":time.time()}; return self._save(d)
    def mark_started(self,run_id:str,action_id:str)->dict: return self._mark(run_id,action_id,STARTED_UNCONFIRMED)
    def mark_committed(self,run_id:str,action_id:str,result:Any)->dict: return self._mark(run_id,action_id,COMMITTED,result)
    def _mark(self,run_id,action_id,boundary,result=None):
        with self._lock:
            d=self.load(run_id); a=d.setdefault("actions",{}).get(action_id)
            if not a: raise KeyError(action_id)
            a.update(boundary=boundary,updated_at=time.time())
            if boundary==COMMITTED: a["result"]=result
            d["updated_at"]=time.time(); return self._save(d)
    def request_pause(self,run_id:str,reason:str)->dict:
        with self._lock:
            d=self.load(run_id); d.update(task_state="PAUSED",pause_reason=str(reason),updated_at=time.time()); return self._save(d)
    def resume_policy(self,run_id:str,action_id:str)->str:
        a=self.load(run_id).get("actions",{}).get(action_id,{})
        return {COMMITTED:"REUSE_RESULT",NOT_STARTED:"EXECUTE",STARTED_UNCONFIRMED:"RECONCILE"}.get(a.get("boundary"),"EXECUTE")
    def admit_stage(self, run_id:str, manifest:dict, actions:list[dict])->dict:
        """Durably bind stage identity/sequence/action signatures."""
        encoded=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        signature=hashlib.sha256(encoded.encode()).hexdigest(); stage_id=str(manifest.get("stage_id", "")); seq=int(manifest.get("seq",0))
        with self._lock:
            d=self.load(run_id); stages=d.setdefault("stages",{}); existing=stages.get(stage_id)
            if existing and existing.get("manifest_sha256")!=signature: raise ValueError("stage_id_reused_with_changed_manifest")
            last=max((int(x.get("seq",0)) for x in stages.values()),default=0)
            if not existing and seq<=last: raise ValueError("stage_seq_regression")
            if not existing:
                stages[stage_id]={"seq":seq,"manifest_sha256":signature,"actions":{str(a.get("action_id", "")):self._sig(a) for a in actions},"outcomes":{}}
            d["updated_at"]=time.time(); return self._save(d)
    def stage_resume_policy(self, run_id:str, stage_id:str, action:dict)->tuple[str,object]:
        """Never replay an uncertain mutation after a restart."""
        d=self.load(run_id); stage=dict(d.get("stages",{}).get(stage_id) or {}); action_id=str(action.get("action_id", ""))
        if stage and stage.get("actions",{}).get(action_id) not in {None,self._sig(action)}: raise ValueError("stage_action_signature_conflict")
        item=dict(d.get("actions",{}).get(action_id) or {})
        boundary=item.get("boundary")
        if boundary==COMMITTED: return "REUSE_RESULT",item.get("result")
        if boundary==STARTED_UNCONFIRMED: return "RECONCILE_REQUIRED",None
        return "EXECUTE",None
    @staticmethod
    def _sig(action:dict)->str:
        return hashlib.sha256(json.dumps(action,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
