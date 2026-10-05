#!/usr/bin/env python3
from __future__ import annotations
import hashlib
from pathlib import Path
from .edit_plan_contract import validate_edit_plan

def _sha_bytes(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def _inside(root:Path,p:Path)->bool:
 try:p.resolve().relative_to(root.resolve());return True
 except ValueError:return False

def apply_edit_plan(workspace:str|Path,plan:dict)->dict:
 root=Path(workspace).expanduser().resolve();validation=validate_edit_plan(root,plan)
 if validation.get('status')!='VALID':return {'status':validation.get('status'),'validation':validation,'applied':[],'rolled_back':False}
 backups={};created=[];applied=[]
 try:
  for item in validation['files_to_modify']:
   rel=item['path'];target=root/rel
   if not _inside(root,target):raise ValueError(f'INVALID_PATH:{rel}')
   create=bool(item.get('create',False));mode=str(item.get('mode','whole_file'))
   if create:created.append(target)
   else:backups[target]=target.read_bytes()
   expected=item.get('base_sha256','')
   if expected and target.is_file() and _sha_bytes(target.read_bytes())!=expected:raise ValueError(f'BASE_HASH_MISMATCH:{rel}')
   if mode=='whole_file':
    content=item.get('content')
    if not isinstance(content,str):raise ValueError(f'MISSING_CONTENT:{rel}')
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content,encoding=item.get('encoding','utf-8'))
   elif mode=='exact_replace':
    old=item.get('old');new=item.get('new')
    if not isinstance(old,str) or not isinstance(new,str):raise ValueError(f'INVALID_REPLACEMENT:{rel}')
    text=target.read_text(encoding=item.get('encoding','utf-8'))
    if text.count(old)!=1:raise ValueError(f'PRECONDITION_FAILED:{rel}')
    target.write_text(text.replace(old,new,1),encoding=item.get('encoding','utf-8'))
   else:raise ValueError(f'UNSUPPORTED_MODE:{mode}')
   post=item.get('post_sha256','')
   if post and _sha_bytes(target.read_bytes())!=post:raise ValueError(f'POSTCONDITION_FAILED:{rel}')
   applied.append(rel)
  return {'status':'APPLIED','applied':applied,'rolled_back':False}
 except Exception as e:
  for p,data in backups.items():p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  for p in created:
   if p.exists():p.unlink()
  return {'status':'ROLLED_BACK','error':str(e),'applied':applied,'rolled_back':True}
