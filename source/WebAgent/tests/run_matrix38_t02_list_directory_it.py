"""T02 sequential UT / real Runtime / live web gates."""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.paths import remote_tasks_path
from agent_core.task_state import TaskStateStore
from agent_core.task_continuation import TaskContinuationStore
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_matrix38_t02_list_directory import invoke
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response

ROOT=Path(__file__).resolve().parents[3]
STATE=ROOT/'localdata/runtime_it/matrix38/t02_list_directory.json'


def load():
    t01=read_json_retry(STATE.with_name('t01_read_file.json'))
    assert t01['status']=='PASS','T01 must pass before T02'
    if STATE.exists():return read_json_retry(STATE)
    fixture=Path(r'E:\workspace\runtime_it\matrix38')/('T02-'+uuid.uuid4().hex[:12])
    target=fixture/'listed';target.mkdir(parents=True,exist_ok=False)
    (target/'alpha.txt').write_bytes(b'T02_ALPHA')
    (target/'中文.txt').write_bytes(b'T02_UTF8_NAME')
    (target/'sub').mkdir();(target/'sub'/'nested_not_listed.txt').write_bytes(b'NO_RECURSE')
    (target/'empty').mkdir()
    value={'tool':'list_directory','status':'IN_PROGRESS','fixture':str(fixture),'target':str(target),'phases':{},
       'rca':[{'before':'missing directory reported generic FAILED/executed=true; scope lacks canonical identity',
         'fix':'stat preflight and typed directory outcomes; authoritative common envelope retains scope denial'}]}
    write_json_atomic(STATE,value);return value


def unit(s):
    p=subprocess.run([sys.executable,'-m','WebAgent.tests.validate_matrix38_t02_list_directory'],cwd=ROOT,
                     capture_output=True,text=True,encoding='utf-8',errors='replace')
    s['phases']['UT']={'status':'PASS' if p.returncode==0 else 'FAIL','output':p.stdout+p.stderr}
    assert p.returncode==0,p.stdout+p.stderr


def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    fixture=Path(s['fixture']);target=Path(s['target']);events=[];turns=[]
    tools=WebAgentToolContext(fixture,interface_name='remote')
    actions=[{'tool':'list_directory','action_id':'T02-R-'+str(i),'path':str(p)} for i,p in enumerate((target,target/'empty'))]
    contract='列舉兩個目錄的第一層與空目錄，結果符合真實 fixture'
    progress={**initial(),'base_evaluation':'T02 fixed legal directory actions', 'next_action':'list_directory',
      'completion_contract':{'success':[contract],'failure':['未正確列舉'],'in_progress':['待列舉'],'interrupted':['環境阻塞']},'matched_condition':'待列舉'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        if len(turns)==1:return response(progress,*actions)
        done={**progress,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
              'steps':[{'step':1,'desc':'列舉目錄','status':'COMPLETED'}],'matched_condition':contract,'evidence_refs':[a['action_id'] for a in actions]}
        return response(done,{'tool':'final_response','action_id':'T02-R-FINAL','content':'T02_RUNTIME_LIST_PASS'})
    loop=WebAgentProtocolLoop(fixture,planner,tool_context=tools,progress_root=ROOT,
                             event_sink=lambda event,**kw:events.append({'event':event,**kw}))
    result=loop.run('列出以下目錄第一層：\n'+str(target)+'\n'+str(target/'empty'))
    assert result=='T02_RUNTIME_LIST_PASS' and loop.terminal_outcome=='SUCCESS'
    rows=[decode_result(loop.action_ledger[a['action_id']]['result']) for a in actions]
    expected={p.name for p in target.iterdir()}
    assert {r['name'] for r in rows[0]['data']['entries']}==expected
    assert rows[1]['data']['entries']==[] and rows[1]['data']['text']=='(空目錄)'
    for p,reason in ((target/'missing','DIRECTORY_NOT_FOUND'),(target/'alpha.txt','NOT_A_DIRECTORY'),(fixture,'TOOL_SCOPE_REJECTED')):
        _,_,row,seen=invoke(tools,p,'R-'+reason)
        assert row['reason']==reason and not row['executed'],row
        assert not seen
    if os.name!='nt':raise RuntimeError('Windows directory access-denied IT unavailable')
    from ctypes import wintypes
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype=wintypes.HANDLE;k.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=k.CreateFileW(str(target),0x1,0,None,3,0x02000000,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:
        _,_,row,_=invoke(tools,target,'R-LOCKED')
        assert row['status']=='REJECTED' and row['reason']=='ACCESS_DENIED' and not row['executed'],row
    finally:k.CloseHandle(handle)
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),
        'rows':rows,'denied':'actual exclusive Windows directory handle; no ACL modification',
        'events':[x for x in events if x['event'] in {'tool_started','tool_completed'}]}


def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):
        s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    target=Path(s['target'])
    prompt='只測試 list_directory；分別列出以下兩個目錄的第一層，不遞迴、不讀取檔案、不上傳、不寫入或刪除。使用真實 Runtime 結果列出所有名稱並說明第二目錄是否為空。\n'+str(target)+'\n'+str(target/'empty')
    sink={};code=send_and_wait(request=prompt,workspace=r'E:\workspace',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']]
    journal=TaskContinuationStore(ROOT).get(sink['task_id']) or {}
    calls=list(journal.get('actions',{}).values());final=task.result_ledger.get('final',{})
    rows={x['action']['path']:decode_result(x['result']) for x in calls}
    expected={p.name for p in target.iterdir()}
    passed=(task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
        and len(calls)==2 and all(x['action']['tool']=='list_directory' for x in calls)
        and set(rows)=={str(target),str(target/'empty')}
        and all(r['status']=='SUCCEEDED' and r['executed'] for r in rows.values())
        and {r['name'] for r in rows[str(target)]['data']['entries']}==expected
        and rows[str(target/'empty')]['data']['entries']==[]
        and all(name in final.get('response','') for name in expected))
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN',
        'V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',
        **sink,'sender_exit_code':code,'rows':rows,'response':final.get('response',''),'task_outcome':final.get('task_outcome'),
        'runtime_error':task.error,'actual_action_count':len(calls),
        'failure_layer':'BOOTSTRAP_BEFORE_ACTION' if not calls and task.error else ''}
    assert passed,s['phases']['IT-W']


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args();s=load()
    try:
        {'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}')
        write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T02_PHASE_PASS',a.phase,'overall='+s['status'])


if __name__=='__main__':main()
