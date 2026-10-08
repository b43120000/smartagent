#!/usr/bin/env python3
from __future__ import annotations
import hashlib
from pathlib import Path
from .project_sync import inspect_project_scope

REQUIRED_PLAN_FIELDS=('base_snapshot_id','files_to_modify','verification_commands','expected_observable_result','rollback_condition')

def _inside(root:Path,path:Path)->bool:
 try:path.resolve().relative_to(root.resolve());return True
 except ValueError:return False

def _sha(path:Path)->str:
 h=hashlib.sha256();h.update(path.read_bytes());return h.hexdigest()

def validate_edit_plan(workspace:str|Path,plan:dict)->dict:
 root=Path(workspace).expanduser().resolve()
 missing=[k for k in REQUIRED_PLAN_FIELDS if k not in plan]
 if missing:return {'status':'INVALID_PLAN','reason':'MISSING_FIELDS','missing_fields':missing}
 current=inspect_project_scope(root);base=str(plan.get('base_snapshot_id',''))
 if base!=current.get('snapshot_id'):return {'status':'PLAN_BASE_MISMATCH','base_snapshot_id':base,'current_snapshot_id':current.get('snapshot_id','')}
 files=plan.get('files_to_modify',[])
 if not isinstance(files,list) or not files:return {'status':'INVALID_PLAN','reason':'EMPTY_FILES_TO_MODIFY'}
 normalized=[]
 for item in files:
  if not isinstance(item,dict) or not isinstance(item.get('path'),str):return {'status':'INVALID_PLAN','reason':'INVALID_FILE_ENTRY'}
  rel=item['path'].replace('\\','/').lstrip('/');target=root/rel
  if not _inside(root,target):return {'status':'INVALID_PATH','path':item['path']}
  create=bool(item.get('create',False))
  if not create and not target.is_file():return {'status':'MISSING_TARGET','path':rel}
  if create and target.exists():return {'status':'CREATE_TARGET_EXISTS','path':rel}
  expected=item.get('base_sha256','')
  if expected and target.is_file() and _sha(target)!=expected:return {'status':'BASE_HASH_MISMATCH','path':rel}
  if not isinstance(item.get('modification_intent',''),str):return {'status':'INVALID_PLAN','reason':'INVALID_MODIFICATION_INTENT','path':rel}
  mode=str(item.get('mode','whole_file'))
  if mode=='whole_file' and not isinstance(item.get('content'),str):return {'status':'INVALID_PLAN','reason':'MISSING_CONTENT','path':rel,'error':f'MISSING_CONTENT:{rel}'}
  if mode=='exact_replace' and (not isinstance(item.get('old'),str) or not isinstance(item.get('new'),str)):return {'status':'INVALID_PLAN','reason':'INVALID_REPLACEMENT','path':rel,'error':f'INVALID_REPLACEMENT:{rel}'}
  if mode not in {'whole_file','exact_replace'}:return {'status':'INVALID_PLAN','reason':'UNSUPPORTED_MODE','path':rel,'error':f'UNSUPPORTED_MODE:{mode}'}
  normalized.append({**item,'path':rel,'create':create})
 return {'status':'VALID','base_snapshot_id':base,'current_snapshot_id':current['snapshot_id'],'files_to_modify':normalized,'verification_commands':plan.get('verification_commands',[]),'expected_observable_result':plan.get('expected_observable_result'),'rollback_condition':plan.get('rollback_condition')}
