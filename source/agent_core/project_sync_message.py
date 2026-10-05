#!/usr/bin/env python3
from __future__ import annotations
import hashlib
from pathlib import Path
from .attachment_policy import AttachmentPolicy,resolve_attachment_policy
from .project_sync import inspect_project_scope,save_project_snapshot
from .project_bundle import dependency_evidence,build_source_bundles,build_project_delta
from .project_sync_transaction import build_project_sync_plan

def _valid_bundle(item:dict)->bool:
 p=Path(item.get('path',''))
 return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==item.get('sha256','')

def build_atomic_project_sync(workspace:str|Path,strategy:str='FULL_BUNDLE',base_snapshot_id:str='',max_bytes:int|None=None,max_files:int|None=None,policy:AttachmentPolicy|None=None)->dict:
 root=Path(workspace).expanduser().resolve();strategy=str(strategy or 'FULL_BUNDLE').upper()
 if strategy not in {'FULL_BUNDLE','DELTA'}: raise ValueError(f'project_sync_strategy_invalid:{strategy}')
 policy=resolve_attachment_policy(policy,max_files=max_files,max_bytes=max_bytes)
 snapshot=inspect_project_scope(root);evidence=dependency_evidence(root,snapshot.get('files',[]))
 if strategy=='DELTA':
  delta=build_project_delta(root,base_snapshot_id,policy=policy)
  if delta.get('status')=='DELTA_NOT_AVAILABLE':return {'schema':'PROJECT_SYNC_MESSAGE_V1','status':'DELTA_NOT_AVAILABLE','strategy':'DELTA','base_snapshot_id':base_snapshot_id}
  payload=delta.get('bundle',{});current_id=delta.get('current_snapshot_id','')
 else:
  out=root/'.agents'/'project_context'/'bundles'/snapshot['snapshot_id']
  payload=build_source_bundles(root,snapshot.get('files',[]),out,policy=policy);current_id=snapshot['snapshot_id']
 bundles=payload.get('bundles',[]);hashes_valid=all(_valid_bundle(x) for x in bundles)
 complete=bool(payload.get('complete')) and hashes_valid and sum(x.get('file_count',0) for x in bundles)==payload.get('bundle_entry_count',payload.get('source_file_count',0))
 status='READY' if complete else 'INCOMPLETE'
 if status=='READY' and strategy!='DELTA':save_project_snapshot(snapshot)
 transaction=build_project_sync_plan(current_id,bundles,policy) if complete else None
 return {'schema':'PROJECT_SYNC_MESSAGE_V1','status':status,'local_status':'LOCAL_READY' if complete else 'INCOMPLETE','sync_status':'LOCAL_READY' if complete else 'INCOMPLETE','strategy':strategy,'snapshot_id':current_id,'snapshot_manifest':snapshot,'project_evidence':evidence,'source_bundles':bundles,'bundle_count':len(bundles),'hashes_valid':hashes_valid,'manifest_complete':snapshot.get('file_count',0)==len(snapshot.get('files',[])),'atomic_complete':complete,'transaction':transaction,'batch_count':transaction['batch_count'] if transaction else 0,'batches':transaction['batches'] if transaction else []}
