#!/usr/bin/env python3
from __future__ import annotations
import time
from pathlib import Path

from .bounded_process import run_bounded_process
from .command_security import inspect_command

def run_aggregated_verification(workspace:str|Path,commands:list[str],timeout:int=120)->dict:
 root=Path(workspace).expanduser().resolve();results=[];started=time.perf_counter()
 for index,command in enumerate(commands,1):
  t0=time.perf_counter()
  admission=inspect_command(command)
  if not admission.allowed:
   results.append({'index':index,'command':command,'exit_code':None,'timed_out':False,'security_rejected':True,'error':f'SECURITY_COMMAND_REJECTED:{admission.code}:{admission.detail}','elapsed_ms':round((time.perf_counter()-t0)*1000,3)})
   continue
  capture_root=root/'.agents'/'results'/'verification_capture'
  captured=run_bounded_process(command,cwd=root,shell=True,timeout=timeout,capture_root=capture_root)
  item={'index':index,'command':command,**captured,'elapsed_ms':round((time.perf_counter()-t0)*1000,3)}
  results.append(item)
 status='PASS' if results and all(x['exit_code']==0 and not x['timed_out'] for x in results) else ('PASS' if not results else 'FAIL')
 return {'schema':'AGGREGATED_VERIFICATION_V1','status':status,'command_count':len(results),'elapsed_ms':round((time.perf_counter()-started)*1000,3),'results':results}
