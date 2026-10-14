"""T03 sequential metadata inspection gates; no source or user data fixtures."""
import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.paths import remote_tasks_path
from agent_core.task_state import TaskStateStore
from agent_core.task_continuation import TaskContinuationStore
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response

ROOT=Path(__file__).resolve().parents[3]
STATE=ROOT/'localdata/runtime_it/matrix38/t03_inspect_directory.json'


def fixture(root):
    target=root/'data';target.mkdir()
    (target/'alpha.txt').write_bytes(b'abc');(target/'beta.bin').write_bytes(b'1234567')
    (target/'empty').mkdir();(target/'sub').mkdir();(target/'sub'/'nested.txt').write_bytes(b'NESTED_FILE')
    return target


def call(ctx,paths,recursive=True,limit=20):
    action={'tool':'inspect_directory','action_id':'T03-'+uuid.uuid4().hex[:8],
            'paths':[str(p) for p in paths],'recursive':recursive,'sample_limit':limit}
    valid,detail=validate_tool_envelope(action);assert valid,detail
    scans=[]
    import os
    real=os.scandir
    def scan(p):scans.append(str(p));return real(p)
    with patch('agent_core.tools.os.scandir',side_effect=scan):raw=ctx.execute(action)
    return action,json.loads(raw),decode_result(raw),scans


def check(payload,recursive):
    expected={'file_count':3 if recursive else 2,'directory_count':2,
              'total_bytes':21 if recursive else 10,'inaccessible_count':0}
    assert payload['aggregate']==expected,payload
    row=payload['paths'][0]
    assert row['exists'] and row['authorized'] and not row['errors']
    assert row['extensions']==({'.txt':2,'.bin':1} if recursive else {'.txt':1,'.bin':1})
    assert len(row['samples'])<=payload['sample_limit_per_path']


def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t03-') as temp:
        root=Path(temp);target=fixture(root);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T03','統計目錄 metadata：\n'+str(target),[str(target)])
        for recursive in (False,True):
            action,payload,row,scans=call(ctx,[target],recursive,1)
            check(payload,recursive);assert row['status']=='SUCCEEDED' and row['executed']
            assert len(scans)==(3 if recursive else 1),scans
        _,payload,row,_=call(ctx,[target,target/'empty'],True,0)
        assert len(payload['paths'])==2 and payload['paths'][1]['file_count']==0
        assert not any(p['samples'] for p in payload['paths']) and not ctx.take_pending_attachments()
        for bad,error in ((target/'missing','path_not_found'),(target/'alpha.txt','not_a_directory')):
            _,payload,row,scans=call(ctx,[bad]);assert row['status']=='FAILED' and not row['executed']
            assert error in payload['paths'][0]['errors'] and not scans
        action={'tool':'inspect_directory','action_id':'T03-SCOPE','paths':[str(root)]}
        with patch('agent_core.tools.os.scandir',side_effect=AssertionError('scope must not scan')):
            assert decode_result(ctx.execute(action))['status']=='REJECTED'
        with patch('agent_core.tools.os.scandir',side_effect=PermissionError('T03 scan denied')):
            row=decode_result(ctx.execute({'tool':'inspect_directory','action_id':'T03-DENIED','paths':[str(target)]}))
        assert row['status']=='PARTIAL' and row['executed'],row
        # Root metadata denial must not masquerade as nonexistence.
        from agent_core.tools import tool_inspect_directory
        import os
        real_stat=os.stat
        def stat(p,*args,**kw):
            if Path(p)==target:raise PermissionError('T03 metadata denied')
            return real_stat(p,*args,**kw)
        real_exists=Path.exists
        # Windows exists() uses a native boolean probe that can hide errno;
        # model the denied false probe alongside its explicit stat failure.
        with patch('os.stat',stat),patch.object(Path,'exists',lambda p,**kw:False if p==target else real_exists(p,**kw)):
            payload=json.loads(tool_inspect_directory([str(target)],allowed_roots=[str(root)]))
        assert any('PermissionError' in e for e in payload['paths'][0]['errors']),payload
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS',
        'cases':['recursive','nonrecursive','empty','multi-path','sample-zero/one','missing','file','scope','scan-denied','metadata-denied']}


def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    target=Path(s['target']);ctx=WebAgentToolContext(Path(s['fixture']),interface_name='remote');turns=[]
    actions=[{'tool':'inspect_directory','action_id':'T03-R-'+str(i),'paths':[str(target)],'recursive':v,'sample_limit':1} for i,v in enumerate((False,True))]
    goal='完成非遞迴與遞迴目錄統計並與 fixture 相符'
    p={**initial(),'base_evaluation':'T03 metadata inspection','next_action':'inspect_directory',
       'completion_contract':{'success':[goal],'failure':['統計錯誤'],'in_progress':['待統計'],'interrupted':['環境阻塞']},'matched_condition':'待統計'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        if len(turns)==1:return response(p,*actions)
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
           'steps':[{'step':1,'desc':'目錄統計','status':'COMPLETED'}],'matched_condition':goal,'evidence_refs':[a['action_id'] for a in actions]},
           {'tool':'final_response','action_id':'T03-R-FINAL','content':'T03_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(Path(s['fixture']),planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('統計目錄 metadata：\n'+str(target))=='T03_RUNTIME_PASS'
    rows=[]
    for a in actions:
        raw=loop.action_ledger[a['action_id']]['result'];check(json.loads(raw),a['recursive'])
        row=decode_result(raw);assert row['status']=='SUCCEEDED';rows.append(row)
    from ctypes import WinDLL,c_void_p,get_last_error,WinError,wintypes
    k=WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype=wintypes.HANDLE;k.CloseHandle.argtypes=[wintypes.HANDLE]
    h=k.CreateFileW(str(target),0x1,0,None,3,0x02000000,None)
    if h==c_void_p(-1).value:raise WinError(get_last_error())
    try:
        _,payload,denied,_=call(ctx,[target],False)
        assert denied['status']=='PARTIAL' and payload['aggregate']['inaccessible_count']==1 and payload['aggregate']['file_count']==0
    finally:k.CloseHandle(h)
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'rows':rows,'denied':'actual exclusive directory handle; no ACL changes'}


def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    prompt='只測試 inspect_directory 目錄 metadata 統計。對下方目錄執行兩個 inspect_directory action：第一個 recursive=false，第二個 recursive=true；兩者 sample_limit=1。回報各自 file_count、directory_count、total_bytes、inaccessible_count。禁止讀檔內容、上傳、寫入、刪除或其他工具。\n'+s['target']
    sink={};code=send_and_wait(request=prompt,workspace=r'E:\workspace',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']]
    journal=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(journal.get('actions',{}).values())
    final=task.result_ledger.get('final',{});passed=False
    if len(calls)==2 and all(c['action']['tool']=='inspect_directory' for c in calls):
        assert {c['action'].get('recursive') for c in calls}=={False,True}
        for c in calls:
            assert c['action']['paths']==[s['target']];check(json.loads(c['result']),c['action']['recursive'])
            assert decode_result(c['result'])['status']=='SUCCEEDED'
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args()
    assert read_json_retry(STATE.with_name('t02_list_directory.json'))['status']=='PASS','T02 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\workspace\runtime_it\matrix38')/('T03-'+uuid.uuid4().hex[:12]);root.mkdir();target=fixture(root)
        s={'tool':'inspect_directory','status':'IN_PROGRESS','fixture':str(root),'target':str(target),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T03_PHASE_PASS',a.phase,'overall='+s['status'])


if __name__=='__main__':main()
