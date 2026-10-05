#!/usr/bin/env python3
"""Launcher preflight for software-only and OS-enforced security modes."""
from __future__ import annotations
import argparse,json,os
from pathlib import Path
from .windows_security import attest,restricted_executor_required

def resolve_workspace(interface:str,explicit:str='')->Path:
 if explicit:return Path(explicit).resolve()
 if interface=='remote':
  from .remote_binding import active,load
  binding=active() or load();return Path(binding['workspace']).resolve()
 from .startup_preferences import load_startup_preferences
 selected=load_startup_preferences();value=str(selected.get('workspace','') or '')
 if not value:raise RuntimeError('security_preflight_workspace_missing')
 return Path(value).resolve()

def preflight(interface:str,workspace:str='')->dict:
 root=resolve_workspace(interface,workspace);required=restricted_executor_required()
 if not required:return {'status':'SOFTWARE_GUARD_ONLY','workspace':str(root),'restricted_executor_required':False}
 report=attest(root,require_executor=True)
 return {'status':'PASS' if report['passed'] else 'FAIL','workspace':str(root),'restricted_executor_required':True,'attestation':report}

def main(argv=None):
 p=argparse.ArgumentParser();p.add_argument('--interface',choices=['local','remote'],required=True);p.add_argument('--workspace',default='');a=p.parse_args(argv);report=preflight(a.interface,a.workspace);print(json.dumps(report,ensure_ascii=False,separators=(',',':')));return 0 if report['status']!='FAIL' else 2
if __name__=='__main__':raise SystemExit(main())
