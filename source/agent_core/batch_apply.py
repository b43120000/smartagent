#!/usr/bin/env python3
from __future__ import annotations
import hashlib,os,tempfile
from pathlib import Path
from .edit_plan_contract import validate_edit_plan
from .path_security import authorize_path,reject_reparse_components

def _sha_bytes(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def _write_atomic(root:Path,target:Path,data:bytes,*,create=False,published=None,expected=None)->None:
 # Replace the entry, never truncate a hard-link target in place.
 reject_reparse_components(target,root)
 fd,temporary=tempfile.mkstemp(prefix=target.name+'.',suffix='.edit.tmp',dir=target.parent)
 try:
  with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
  reject_reparse_components(target,root)
  if expected is not None and target.read_bytes()!=expected:raise ValueError('TARGET_CHANGED_BEFORE_WRITE')
  if create:
   # Windows rename refuses an existing destination, including on FAT/exFAT.
   if os.name=='nt':os.rename(temporary,target)
   else:os.link(temporary,target)
  else:os.replace(temporary,target)
  if published:published()
 finally:Path(temporary).unlink(missing_ok=True)

def apply_edit_plan(workspace:str|Path,plan:dict,*,authorize_target=None)->dict:
 root=Path(workspace).expanduser().resolve();validation=validate_edit_plan(root,plan)
 if validation.get('status')!='VALID':return {'status':validation.get('status'),'validation':validation,'applied':[],'rolled_back':False}
 targets={item['path']:authorize_path(item['path'],[root],base=root,forbid_glob=True) for item in validation['files_to_modify']}
 if authorize_target:
  for target in targets.values():authorize_target(target)
 backups={};created=[];created_dirs=[];touched=[];applied=[];readbacks=[]
 try:
  for item in validation['files_to_modify']:
   rel=item['path'];target=targets[rel]
   create=bool(item.get('create',False));mode=str(item.get('mode','whole_file'))
   original=b'' if create else target.read_bytes()
   expected=item.get('base_sha256','')
   if expected and not create and _sha_bytes(original)!=expected:raise ValueError(f'BASE_HASH_MISMATCH:{rel}')
   if mode=='whole_file':
    content=item.get('content')
    if not isinstance(content,str):raise ValueError(f'MISSING_CONTENT:{rel}')
    output=content.encode(item.get('encoding','utf-8'))
   elif mode=='exact_replace':
    old=item.get('old');new=item.get('new')
    if not isinstance(old,str) or not isinstance(new,str):raise ValueError(f'INVALID_REPLACEMENT:{rel}')
    text=original.decode(item.get('encoding','utf-8'))
    if text.count(old)!=1:raise ValueError(f'PRECONDITION_FAILED:{rel}')
    output=text.replace(old,new,1).encode(item.get('encoding','utf-8'))
   else:raise ValueError(f'UNSUPPORTED_MODE:{mode}')
   if create:created.append(target)
   else:backups[target]=original
   missing=[];cursor=target.parent
   while cursor!=root and not cursor.exists():missing.append(cursor);cursor=cursor.parent
   for directory in reversed(missing):directory.mkdir();created_dirs.append(directory)
   _write_atomic(root,target,output,create=create,expected=None if create else original,published=lambda:touched.append(target))
   actual=target.read_bytes()
   if actual!=output:raise ValueError(f'WRITE_READBACK_MISMATCH:{rel}')
   post=item.get('post_sha256','')
   if post and _sha_bytes(actual)!=post:raise ValueError(f'POSTCONDITION_FAILED:{rel}')
   readbacks.append({'path':rel,'bytes':len(actual),'sha256':_sha_bytes(actual),'status':'PASS'})
   applied.append(rel)
  return {'status':'APPLIED','applied':applied,'rolled_back':False,'readback_checks':readbacks}
 except Exception as e:
  errors=[]
  for p in reversed(touched):
   try:
    reject_reparse_components(p,root)
    if authorize_target:authorize_target(p)
    if p in backups:
     data=backups[p];_write_atomic(root,p,data)
     if p.read_bytes()!=data:raise ValueError('ROLLBACK_READBACK_MISMATCH')
    elif p in created and p.exists():p.unlink()
   except Exception as exc:errors.append({'path':p.relative_to(root).as_posix(),'error':type(exc).__name__+':'+str(exc)})
  for directory in reversed(created_dirs):
   try:reject_reparse_components(directory,root);directory.rmdir()
   except Exception as exc:errors.append({'path':directory.relative_to(root).as_posix(),'error':type(exc).__name__+':'+str(exc)})
  return {'status':'ROLLBACK_INCOMPLETE' if errors else 'ROLLED_BACK','error':str(e),'applied':applied,
          'rolled_back':not errors,'rollback_errors':errors,'possible_partial_execution':bool(errors)}
