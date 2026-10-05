#!/usr/bin/env python3
"""Fail-closed client for a separately running low-privilege executor."""
from __future__ import annotations
import json,os,time,uuid
from pathlib import Path
from .json_state_io import read_json_retry,write_json_atomic
from .windows_security import load_security_profile
from .protocol_v8 import sha256_digest

class RestrictedExecutorUnavailable(RuntimeError):pass

def execute_restricted(command,*,cwd=None,timeout=30,shell=False,max_stream_bytes=5*1024*1024,approval_manifest=None,security_command=None)->dict:
 profile=load_security_profile();endpoint=Path(profile['executor_endpoint']).resolve();requests=endpoint/'requests';results=endpoint/'results'
 if not endpoint.is_dir():raise RestrictedExecutorUnavailable(f'restricted_executor_endpoint_unavailable:{endpoint}')
 request_id='EXEC-'+uuid.uuid4().hex.upper();payload={'schema':'RESTRICTED_EXECUTOR_REQUEST_V1','request_id':request_id,'command':command,'cwd':str(Path(cwd).resolve()) if cwd else '','timeout':int(timeout),'shell':bool(shell),'max_stream_bytes':int(max_stream_bytes),'created_at':time.time(),'approval_manifest':dict(approval_manifest or {}),'security_command':str(security_command or '')};payload['request_digest']=sha256_digest(payload)
 requests.mkdir(parents=True,exist_ok=True);write_json_atomic(requests/f'{request_id}.json',payload);deadline=time.time()+max(5,int(timeout)+10)
 result_path=results/f'{request_id}.json'
 while time.time()<deadline:
  if result_path.is_file():
   result=read_json_retry(result_path);result_path.unlink(missing_ok=True);return dict(result.get('result') or {})
  time.sleep(.1)
 raise RestrictedExecutorUnavailable(f'restricted_executor_timeout:{request_id}')

__all__=['RestrictedExecutorUnavailable','execute_restricted']
