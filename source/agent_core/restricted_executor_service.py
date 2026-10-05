#!/usr/bin/env python3
"""Queue worker that must be launched as the configured non-admin identity."""
from __future__ import annotations
import argparse,getpass,json,os,signal,subprocess,time,uuid
from pathlib import Path
from .json_state_io import read_json_retry,write_json_atomic
from .path_security import authorize_path
from .command_security import require_command_allowed
from .security_approval import SecurityApprovalLedger
from .protocol_v8 import sha256_digest
from .windows_security import acl_mode,authorized_workspace_roots,default_profile_path,is_admin,load_security_profile,workspace_traverse_roots
from .windows_process_job import ensure_current_process_kill_job

def _probe_write(root:Path,*,expect_allowed:bool)->tuple[bool,str]:
 root=root.resolve()
 if not root.is_dir():return False,f'probe_root_unavailable:{root}'
 marker=root/f'.smartagent_acl_probe_{uuid.uuid4().hex}.tmp'
 wrote=False;cleanup_error=''
 try:
  marker.write_bytes(b'probe');wrote=True
 except (PermissionError,OSError):
  wrote=False
 finally:
  if wrote:
   try:marker.unlink(missing_ok=True)
   except OSError:cleanup_error=f'probe_cleanup_failed:{marker}'
 if cleanup_error:return False,cleanup_error
 if wrote!=expect_allowed:return False,f'write_expectation_failed:{root}:expected={expect_allowed}:actual={wrote}'
 return True,''

def _probe_file_write(path:Path,*,expect_allowed:bool)->tuple[bool,str]:
 path=path.resolve()
 if not path.is_file():return False,f'probe_file_unavailable:{path}'
 opened=False
 try:
  handle=os.open(path,os.O_WRONLY);os.close(handle);opened=True
 except (PermissionError,OSError):opened=False
 if opened!=expect_allowed:return False,f'file_write_expectation_failed:{path}:expected={expect_allowed}:actual={opened}'
 return True,''

def _probe_traverse(root:Path)->tuple[bool,str]:
 root=root.resolve()
 if not root.is_dir():return False,f'traverse_root_unavailable:{root}'
 try:
  iterator=os.scandir(root)
  try:next(iterator,None)
  finally:iterator.close()
 except (PermissionError,OSError):return False,f'traverse_denied:{root}'
 return _probe_write(root,expect_allowed=False)

def _security_self_test(profile:dict)->dict:
 mode=acl_mode().casefold()
 if mode=='off':return {'passed':True,'reasons':[],'checked_at':time.time(),'mode':mode}
 checks=[]
 for root in authorized_workspace_roots(profile):checks.append(_probe_write(root,expect_allowed=True))
 for root in workspace_traverse_roots(profile):checks.append(_probe_traverse(root))
 for value in (*tuple(profile.get('read_only_roots') or ()),*tuple(profile.get('denied_write_roots') or ())):
  checks.append(_probe_write(Path(value),expect_allowed=False))
 for value in tuple(profile.get('root_create_denied') or ()):
  checks.append(_probe_write(Path(value),expect_allowed=False))
 checks.append(_probe_file_write(default_profile_path(),expect_allowed=False))
 reasons=[reason for passed,reason in checks if not passed]
 return {'passed':not reasons,'reasons':reasons,'checked_at':time.time(),'mode':mode}

def _run(req:dict,profile:dict)->dict:
 supplied=str(req.get('request_digest',''));unsigned={key:value for key,value in req.items() if key!='request_digest'}
 if supplied!=sha256_digest(unsigned):raise RuntimeError('restricted_executor_request_digest_mismatch')
 command=req.get('command')
 command_text=command if isinstance(command,str) else subprocess.list2cmdline([str(x) for x in command])
 workspace_roots=authorized_workspace_roots(profile)
 cwd=authorize_path(req.get('cwd',''),workspace_roots,require_absolute=True)
 if not any(os.path.normcase(str(cwd)).casefold()==os.path.normcase(str(root)).casefold() for root in workspace_roots):raise RuntimeError('restricted_executor_cwd_not_authorized_workspace')
 security_text=str(req.get('security_command') or command_text)
 if security_text!=command_text:require_command_allowed(command_text)
 approval_manifest=req.get('approval_manifest') or None
 require_command_allowed(security_text,workspace=cwd,approval_manifest=approval_manifest)
 if approval_manifest:
  approval_id=str(approval_manifest.get('_approval_id','') or '')
  binding=approval_manifest.get('_approval_binding')
  if not approval_id or not isinstance(binding,dict):raise RuntimeError('execution_approval_evidence_missing')
  if str(binding.get('approval_kind','')).upper()!='EXECUTION':raise RuntimeError('execution_approval_kind_invalid')
  if str(binding.get('manifest_digest',''))!=str(approval_manifest.get('manifest_digest','')):raise RuntimeError('execution_approval_manifest_binding_mismatch')
  SecurityApprovalLedger(cwd).consume(approval_id,binding)
 env=dict(os.environ);env['SMARTAGENT_RESTRICTED_EXECUTOR_SERVICE']='1';temp=Path(profile['runtime_root']).resolve();temp.mkdir(parents=True,exist_ok=True);env.update(TEMP=str(temp),TMP=str(temp))
 flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0
 cp=subprocess.Popen(req['command'],cwd=str(cwd),shell=bool(req.get('shell')),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,creationflags=flags)
 timed_out=False
 try:stdout,stderr=cp.communicate(timeout=int(req.get('timeout',30)))
 except subprocess.TimeoutExpired:
  timed_out=True
  if os.name=='nt':subprocess.run(['taskkill','/PID',str(cp.pid),'/T','/F'],capture_output=True,check=False,timeout=10)
  else:os.killpg(os.getpgid(cp.pid),signal.SIGKILL)
  stdout,stderr=cp.communicate(timeout=5)
 limit=int(req.get('max_stream_bytes',5*1024*1024));result={'exit_code':cp.returncode,'stdout':stdout[:limit].decode('utf-8',errors='replace').strip(),'stderr':stderr[:limit].decode('utf-8',errors='replace').strip(),'stdout_bytes':len(stdout),'stderr_bytes':len(stderr),'stdout_truncated':len(stdout)>limit,'stderr_truncated':len(stderr)>limit,'stdout_ref':'','stderr_ref':'','timed_out':timed_out}
 if timed_out:result['error']=f'command exceeded {req.get("timeout",30)} seconds; process tree terminated'
 return result

def serve(*,once=False,poll=.2):
 profile=load_security_profile();expected=str(profile.get('executor_user','')).casefold()
 if is_admin():raise RuntimeError('restricted_executor_must_not_be_admin')
 if expected and getpass.getuser().casefold()!=expected:raise RuntimeError('restricted_executor_identity_mismatch')
 if os.name=='nt':
  job=ensure_current_process_kill_job()
  if not job.active:raise RuntimeError(f'restricted_executor_job_unavailable:{job.error}')
 endpoint=Path(profile['executor_endpoint']).resolve();requests=endpoint/'requests';results=endpoint/'results';requests.mkdir(parents=True,exist_ok=True);results.mkdir(parents=True,exist_ok=True)
 self_test={'passed':False,'reasons':['not_run'],'checked_at':0.0}
 while True:
  if time.time()-float(self_test.get('checked_at',0) or 0)>30:self_test=_security_self_test(profile)
  write_json_atomic(endpoint/'service_state.json',{'schema':'RESTRICTED_EXECUTOR_SERVICE_STATE_V1','pid':os.getpid(),'user':getpass.getuser(),'is_admin':False,'heartbeat_at':time.time(),'profile_digest':sha256_digest(profile),'self_test':self_test})
  found=False
  for path in sorted(requests.glob('EXEC-*.json')):
   found=True
   try:
    if not self_test.get('passed'):raise RuntimeError('restricted_executor_acl_self_test_failed:'+','.join(self_test.get('reasons') or []))
    req=read_json_retry(path);result=_run(req,profile);wire={'request_id':req['request_id'],'result':result}
   except Exception as exc:wire={'request_id':path.stem,'result':{'exit_code':None,'stdout':'','stderr':'','timed_out':False,'error':f'{type(exc).__name__}: {exc}'}}
   write_json_atomic(results/f'{path.stem}.json',wire);path.unlink(missing_ok=True)
  if once:return 0
  if not found:time.sleep(poll)

def main(argv=None):
 p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');a=p.parse_args(argv);return serve(once=a.once)
if __name__=='__main__':raise SystemExit(main())
