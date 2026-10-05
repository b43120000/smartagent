#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from RemoteAgent.remote_runtime import RemoteAgentRuntime
from agent_core.remote_runtime_log import RemoteRuntimeLog
from agent_core.paths import remote_runtime_log_path

def run_hidden_supervisor_self_tests():
    source=Path(__file__).read_text(encoding='utf-8').lower()
    runtime=(ROOT/'RemoteAgent'/'remote_runtime.py').read_text(encoding='utf-8').lower()
    forbidden='backend'+'-api'
    checks={
        'no_private_history_http_routes':forbidden not in source and forbidden not in runtime,
        'browser_ui_runtime':'connect_over_cdp' in runtime and 'page.goto' in runtime,
        'independent_scheduler':'scheduler.due()' in runtime,
        'live_active_page_poll':'navigate=false' in runtime,
        'durable_ingress_observer':'remoteingressobserver' in runtime,
        'durable_runtime_logging':'remoteruntimelog' in runtime and 'request_detected' in (ROOT/'RemoteAgent'/'remote_ingress.py').read_text(encoding='utf-8').lower(),
        'request_scoped_workers':'remoteworkerlauncher' in runtime and 'dispatch_available' in runtime,
    }
    checks['all_passed']=all(checks.values()); return checks

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--cdp',default='http://127.0.0.1:1272'); ap.add_argument('--parent-pid',type=int,default=0); ap.add_argument('--poll',type=float,default=2.0); ap.add_argument('--self-test',action='store_true')
    args=ap.parse_args(argv)
    if args.self_test:
        out=run_hidden_supervisor_self_tests(); print(json.dumps(out,ensure_ascii=False,indent=2)); return 0 if out['all_passed'] else 1
    try:
        RemoteAgentRuntime(cdp=args.cdp,poll=args.poll,parent_pid=args.parent_pid).run()
    except Exception as exc:
        RemoteRuntimeLog(remote_runtime_log_path()).write(
            'ERROR', component='hidden_supervisor', stage='AGENT0_BOOTSTRAP_FAILED',
            error=f'{type(exc).__name__}: {exc}', parent_pid=args.parent_pid,
        )
        raise
    return 0

if __name__=='__main__': raise SystemExit(main())
