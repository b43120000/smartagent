#!/usr/bin/env python3
from __future__ import annotations
import hashlib
from pathlib import Path
from .project_sync import inspect_project_scope,save_project_snapshot
from .project_bundle import dependency_evidence,build_source_bundles,build_project_delta

def _valid_bundle(item:dict)->bool:
 p=Path(item.get('path',''))
 return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==item.get('sha256','')

def build_atomic_project_sync(workspace:str|Path,strategy:str='FULL_BUNDLE',base_snapshot_id:str='',max_bytes:int=500000,max_files:int=50)->dict:
 root=Path(workspace).expanduser().resolve();strategy=str(strategy or 'FULL_BUNDLE').upper()
 snapshot=inspect_project_scope(root);evidence=dependency_evidence(root,snapshot.get('files',[]))
 if strategy=='DELTA':
  delta=build_project_delta(root,base_snapshot_id,max_bytes=max_bytes,max_files=max_files)
  if delta.get('status')=='DELTA_NOT_AVAILABLE':return {'schema':'PROJECT_SYNC_MESSAGE_V1','status':'DELTA_NOT_AVAILABLE','strategy':'DELTA','base_snapshot_id':base_snapshot_id}
  payload=delta.get('bundle',{});current_id=delta.get('current_snapshot_id','')
 else:
  out=root/'.agents'/'project_context'/'bundles'/snapshot['snapshot_id']
  payload=build_source_bundles(root,snapshot.get('files',[]),out,max_bytes=max_bytes,max_files=max_files);current_id=snapshot['snapshot_id']
 bundles=payload.get('bundles',[]);hashes_valid=all(_valid_bundle(x) for x in bundles)
 complete=bool(payload.get('complete')) and hashes_valid and sum(x.get('file_count',0) for x in bundles)==payload.get('source_file_count',0)
 status='READY' if complete else 'INCOMPLETE'
 if status=='READY' and strategy!='DELTA':save_project_snapshot(snapshot)
 return {'schema':'PROJECT_SYNC_MESSAGE_V1','status':status,'strategy':strategy,'snapshot_id':current_id,'snapshot_manifest':snapshot,'project_evidence':evidence,'source_bundles':bundles,'bundle_count':len(bundles),'hashes_valid':hashes_valid,'manifest_complete':snapshot.get('file_count',0)==len(snapshot.get('files',[])),'atomic_complete':complete}
