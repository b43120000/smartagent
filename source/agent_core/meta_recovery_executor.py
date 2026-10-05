#!/usr/bin/env python3
from __future__ import annotations
import json,subprocess,time,sys
from pathlib import Path
from .repair_workspace import RepairTransaction
from .self_repair_protocol import validate
from .workspace import AGENT_PROJECT_ROOT

def _load(path:Path)->dict:
    try:
        v=json.loads(path.read_text(encoding='utf-8'))
        return v if isinstance(v,dict) else {}
    except Exception:
        return {}

def _dirty_paths(repo:Path)->set[str]:
    cp=subprocess.run(['git','status','--porcelain=v1','-z'],cwd=str(repo),capture_output=True,check=False)
    if cp.returncode!=0:
        raise RuntimeError('git_status_failed: '+cp.stderr.decode('utf-8',errors='replace')[-2000:])
    fields=cp.stdout.split(b'\0'); dirty=set(); i=0
    while i < len(fields):
        entry=fields[i]; i+=1
        if not entry: continue
        if len(entry)<4: raise RuntimeError('invalid_git_status_record')
        xy=entry[:2].decode('ascii',errors='replace')
        path=entry[3:].decode('utf-8',errors='surrogateescape').replace('\\','/')
        if path: dirty.add(path)
        if ('R' in xy or 'C' in xy):
            if i>=len(fields) or not fields[i]: raise RuntimeError('invalid_git_rename_record')
            other=fields[i].decode('utf-8',errors='surrogateescape').replace('\\','/'); i+=1
            if other: dirty.add(other)
    return dirty

def execute_meta_plan(plan_path:Path|str,repo:Path|str=AGENT_PROJECT_ROOT)->dict:
    doc=_load(Path(plan_path)); plan=doc.get('plan',{}) if isinstance(doc,dict) else {}
    ok,reason=validate(plan)
    if not ok or plan.get('type')!='SELF_REPAIR_PLAN':
        return {'status':'PLAN_REJECTED','reason':reason}
    files=plan.get('files',[]) or []
    if not files:
        return {'status':'NO_CHANGES','issue_id':plan.get('issue_id','')}
    repo=Path(repo).resolve(); tx=RepairTransaction(repo)
    paths=sorted(set(plan.get('modified_paths') or []))
    dirty_paths=_dirty_paths(repo)
    overlap=sorted(set(paths)&dirty_paths)
    if overlap:return {'status':'TARGET_DIRTY_NEEDS_HUMAN','paths':overlap}
    try:
        tx.begin(allow_dirty=True)
        for item in files:
            tx.write(item['path'],item['content'])
        validation=tx.validate([[sys.executable,'-m','py_compile',*paths]])
        if not validation.get('passed'):
            tx.rollback(); return {'status':'VALIDATION_FAILED','validation':validation}
        candidate=tx.commit_candidate('meta-recovery candidate')
        rev=str(candidate.get('candidate_revision','') or '')
        cp=subprocess.run(['git','cherry-pick',rev],cwd=str(repo),capture_output=True,text=True,check=False,timeout=60)
        if cp.returncode!=0:
            subprocess.run(['git','cherry-pick','--abort'],cwd=str(repo),capture_output=True,text=True,check=False)
            tx.rollback(); return {'status':'PROMOTION_FAILED','stderr':cp.stderr[-4000:]}
        tx.rollback()
        return {'status':'APPLIED','issue_id':plan.get('issue_id',''),'candidate_revision':rev,'completed_at':time.time()}
    except Exception as exc:
        try: tx.rollback()
        except Exception: pass
        return {'status':'EXECUTOR_ERROR','error':f'{type(exc).__name__}: {exc}'}
