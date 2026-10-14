"""T04 targeted filename lookup: truthful scope, completeness, and matches."""
import argparse,json,tempfile,uuid
from pathlib import Path
from unittest.mock import patch
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t03_inspect_directory_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t04_find_file.json'


def fixture(root):
    target=root/'search';target.mkdir();(target/'sub').mkdir();(target/'build').mkdir()
    for p in (target/'needle.txt',target/'needle.txt.bak',target/'sub'/'NEEDLE.TXT',target/'build'/'needle.txt'):
        p.write_bytes(b'SYNTHETIC_FIND_FIXTURE')
    return target


def call(ctx,target,name='needle.txt',limit=20):
    a={'tool':'find_file','action_id':'T04-'+uuid.uuid4().hex[:8],'name':name,'root':str(target),'max_results':limit}
    raw=ctx.execute(a);return a,raw,decode_result(raw)


def check(row,target,name):
    assert row['status']=='SUCCEEDED' and row['executed'],row
    matches=row['data']['matches']
    expected={str(target/'needle.txt'),str(target/'sub'/'NEEDLE.TXT'),str(target/'needle.txt.bak')} if name=='needle.txt' else set()
    assert set(matches)==expected,row
    assert row['data']['search_complete'] is True and row['resolved_path']==str(target)
    if matches:assert matches[-1]==str(target/'needle.txt.bak'), 'exact matches must precede substring matches'


def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t04-') as temp:
        root=Path(temp);target=fixture(root);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T04','定位檔名，僅搜尋：\n'+str(target),[str(target)])
        for name in ('needle.txt','absent_xyz.txt'):
            _,_,r=call(ctx,target,name);check(r,target,name)
        from agent_core.progress_recovery import read_fact_digest
        a,first,_=call(ctx,target);_,second,_=call(ctx,target)
        assert read_fact_digest(a,first)==read_fact_digest(a,second), 'action identity is not new search evidence'
        _,_,limited=call(ctx,target,limit=1)
        assert len(limited['data']['matches'])==1 and limited['data']['truncated'] is True
        assert limited['status']=='SUCCEEDED' and limited['data']['result_limit']==1
        _,_,glob=call(ctx,target,'*.txt')
        assert glob['status']=='REJECTED' and glob['reason']=='UNSUPPORTED_GLOB_PATTERN' and not glob['executed']
        import os
        with patch('agent_core.tools.os.walk',side_effect=AssertionError('scope must not walk')):
            _,_,outside=call(ctx,root)
        assert outside['status']=='REJECTED' and not outside['executed'],outside
        real_scandir=os.scandir
        def deny(p):
            if Path(p)==target:raise PermissionError('T04 scan denied')
            return real_scandir(p)
        with patch('os.scandir',side_effect=deny):_,_,denied=call(ctx,target)
        assert denied['status'] in {'REJECTED','FAILED'} and denied['reason']=='SEARCH_ACCESS_DENIED',denied
        assert denied['data']['search_complete'] is False
        for p in (target/'missing',target/'needle.txt'):
            _,_,bad=call(ctx,p);assert bad['status'] in {'FAILED','REJECTED'} and not bad['executed']
        if os.name=='nt':
            import subprocess
            outside=root/'outside';outside.mkdir();(outside/'needle.txt').write_bytes(b'OUTSIDE_SYNTHETIC')
            for link,destination in ((target/'outward_junction',outside),(target/'cycle_junction',target)):
                assert root in link.resolve().parents and (destination==target or root in destination.resolve().parents)
                p=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(destination)],capture_output=True)
                assert p.returncode==0, 'T04 junction fixture unavailable: '+repr(p.stderr)
            scanned=[];real_scandir=os.scandir
            def scan(p):scanned.append(str(p));return real_scandir(p)
            with patch('os.scandir',side_effect=scan):_,_,bounded=call(ctx,target)
            check(bounded,target,'needle.txt')
            assert bounded['data']['excluded_link_count']==2 and len(bounded['data']['scope_exclusions'])==2
            assert str(outside) not in scanned and len(scanned)==2,scanned
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':['exact-before-substring','case-insensitive','no-match','skip-build','limit/truncated','scope','missing/file','denied/no-false-not-found']}


def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    target=Path(s['target']);ctx=WebAgentToolContext(Path(s['fixture']),interface_name='remote');turns=[]
    actions=[{'tool':'find_file','action_id':'T04-R-'+str(i),'root':str(target),'name':n} for i,n in enumerate(('needle.txt','absent_xyz.txt'))]
    goal='命中與無命中搜尋皆取得完整真實結果'
    p={**initial(),'base_evaluation':'T04 scoped filename lookup','next_action':'find_file',
       'completion_contract':{'success':[goal],'failure':['搜尋失敗'],'in_progress':['待搜尋'],'interrupted':['環境阻塞']},'matched_condition':'待搜尋'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        if len(turns)==1:return response(p,*actions)
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
          'steps':[{'step':1,'desc':'搜尋檔名','status':'COMPLETED'}],'matched_condition':goal,'evidence_refs':[a['action_id'] for a in actions]},
          {'tool':'final_response','action_id':'T04-R-FINAL','content':'T04_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(Path(s['fixture']),planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('定位檔名，僅搜尋：\n'+str(target))=='T04_RUNTIME_PASS'
    for a in actions:check(decode_result(loop.action_ledger[a['action_id']]['result']),target,a['name'])
    from ctypes import WinDLL,c_void_p,get_last_error,WinError,wintypes
    k=WinDLL('kernel32',use_last_error=True);k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype=wintypes.HANDLE;k.CloseHandle.argtypes=[wintypes.HANDLE]
    h=k.CreateFileW(str(target),0x1,0,None,3,0x02000000,None)
    if h==c_void_p(-1).value:raise WinError(get_last_error())
    try:
        _,_,denied=call(ctx,target);assert denied['reason']=='SEARCH_ACCESS_DENIED' and denied['status']!='SUCCEEDED',denied
    finally:k.CloseHandle(h)
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'denied':'actual exclusive directory handle'}


def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    request='只測試 find_file；只在下方 root 搜尋 needle.txt 與 absent_xyz.txt，分別使用一個 find_file action。回報完整 matches、是否完整搜尋、無命中狀態。使用預設 max_results；不讀檔內容、不上傳、不寫入或刪除，禁止其他工具。\nroot：\n'+s['target']
    sink={};code=send_and_wait(request=request,workspace=r'E:\workspace',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {}
    calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    if len(calls)==2 and all(c['action']['tool']=='find_file' for c in calls):
        assert {c['action']['name'] for c in calls}=={'needle.txt','absent_xyz.txt'}
        for c in calls:
            assert c['action']['root']==s['target'];check(decode_result(c['result']),Path(s['target']),c['action']['name'])
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args()
    assert read_json_retry(STATE.with_name('t03_inspect_directory.json'))['status']=='PASS','T03 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\workspace\runtime_it\matrix38')/('T04-'+uuid.uuid4().hex[:12]);root.mkdir();target=fixture(root)
        s={'tool':'find_file','status':'IN_PROGRESS','fixture':str(root),'target':str(target),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T04_PHASE_PASS',a.phase,'overall='+s['status'])


if __name__=='__main__':main()
