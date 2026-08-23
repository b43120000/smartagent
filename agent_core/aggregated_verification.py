#!/usr/bin/env python3
from __future__ import annotations
import subprocess,time
from pathlib import Path

def run_aggregated_verification(workspace:str|Path,commands:list[str],timeout:int=120)->dict:
 root=Path(workspace).expanduser().resolve();results=[];started=time.perf_counter()
 for index,command in enumerate(commands,1):
  t0=time.perf_counter()
  try:
   r=subprocess.run(command,cwd=str(root),shell=True,capture_output=True,text=True,timeout=timeout,encoding='utf-8',errors='replace')
   item={'index':index,'command':command,'exit_code':r.returncode,'stdout':r.stdout or '','stderr':r.stderr or '','timed_out':False,'elapsed_ms':round((time.perf_counter()-t0)*1000,3)}
  except subprocess.TimeoutExpired as e:
   item={'index':index,'command':command,'exit_code':None,'stdout':e.stdout or '','stderr':e.stderr or '','timed_out':True,'elapsed_ms':round((time.perf_counter()-t0)*1000,3)}
  results.append(item)
 status='PASS' if results and all(x['exit_code']==0 and not x['timed_out'] for x in results) else ('PASS' if not results else 'FAIL')
 return {'schema':'AGGREGATED_VERIFICATION_V1','status':status,'command_count':len(results),'elapsed_ms':round((time.perf_counter()-started)*1000,3),'results':results}
