"""Sequential T01 gates; real local IO and opt-in live web, synthetic data only."""
import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from agent_core.json_state_io import read_json_retry, write_json_atomic
from agent_core.paths import remote_tasks_path
from agent_core.task_state import TaskStateStore
from agent_core.task_continuation import TaskContinuationStore
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_matrix38_t01_read_file import read_action
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tool_context import WebAgentToolContext

ROOT=Path(__file__).resolve().parents[3]
STATE=ROOT/'localdata/runtime_it/matrix38/t01_read_file.json'


def state():
    if STATE.exists():
        return read_json_retry(STATE)
    fixture=Path(r'E:\workspace\runtime_it\matrix38')/('T01-'+uuid.uuid4().hex[:12])
    fixture.mkdir(parents=True,exist_ok=False)
    small='T01_SMALL_RUNTIME_READ_OK\n中文與海鷗\n'
    (fixture/'small.txt').write_bytes(small.encode('utf-8'))
    (fixture/'empty.txt').write_bytes(b'')
    (fixture/'boundary.txt').write_bytes(b'X'*1_000_000)
    start='T01_LARGE_UPLOAD_START_'+uuid.uuid4().hex[:12]
    end='T01_LARGE_UPLOAD_END_'+uuid.uuid4().hex[:12]
    lines=''.join('Synthetic_Row_%06d: local fixture data, no personal information.\n'%i for i in range(17000))
    big=(start+'\n'+lines+end+'\n').encode('utf-8')
    assert len(big)>1_000_000
    (fixture/'large.txt').write_bytes(big)
    value={'schema':'MATRIX38_T01_V1','tool':'read_file','status':'IN_PROGRESS','fixture':str(fixture),
           'small_text':small,'large_start':start,'large_end':end,'large_bytes':len(big),
           'large_sha256':hashlib.sha256(big).hexdigest(),'phases':{},'rca':[
           {'reason':'WEB_ATTACHMENT_FIRST_FOR_SMALL_TEXT','before':'QUEUED/content_read=false for a small UTF-8 file',
            'fix':'bounded UTF-8 file reads return content; binary/large keep real attachment staging; no false delivered claim'}]}
    write_json_atomic(STATE,value);return value


def unit(s):
    result=subprocess.run([sys.executable,'-m','WebAgent.tests.validate_matrix38_t01_read_file'],
        cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
    s['phases']['UT']={'status':'PASS' if result.returncode==0 else 'FAIL','exit_code':result.returncode,
                       'E':'PASS' if result.returncode==0 else 'UNKNOWN',
                       'V':'PASS' if result.returncode==0 else 'UNKNOWN',
                       'R':'PASS' if result.returncode==0 else 'UNKNOWN','output':result.stdout+result.stderr}
    write_json_atomic(STATE,s);assert result.returncode==0,result.stdout+result.stderr


def runtime_it(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS','UT gate not passed'
    fixture=Path(s['fixture']); names=['small.txt','empty.txt','boundary.txt']
    actions=[{'tool':'read_file','action_id':'T01-R-'+name.split('.')[0],'path':str(fixture/name)} for name in names]
    success='三個檔案真正讀取、結果與 fixture 一致，無附件上傳'
    prog={**initial(),'base_evaluation':'fixed valid T01 file read actions','next_action':'read_file',
          'completion_contract':{'success':[success],'failure':['檔案未讀取或內容錯誤'],
              'in_progress':['仍需讀取檔案'],'interrupted':['環境阻塞']},'matched_condition':'仍需讀取檔案'}
    turns=[]; events=[]
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments,'small/plain bounded reads must not upload'
        if len(turns)==1:
            return response(prog,*actions)
        final={**prog,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
               'steps':[{'step':1,'desc':'讀取檔案','status':'COMPLETED'}],
               'matched_condition':success,'evidence_refs':[a['action_id'] for a in actions]}
        return response(final,{'tool':'final_response','action_id':'T01-R-FINAL','content':'T01_RUNTIME_READ_PASS'})
    loop=WebAgentProtocolLoop(fixture,planner,progress_root=ROOT,
         event_sink=lambda event,**data:events.append({'event':event,**data}))
    request='讀取以下獨立檔案，不做專案索引：\n'+'\n'.join(a['path'] for a in actions)
    result=loop.run(request,request_id='RR-T01-FIXED-'+uuid.uuid4().hex[:12],task_id='TASK-T01-FIXED-'+uuid.uuid4().hex[:12])
    assert result=='T01_RUNTIME_READ_PASS' and loop.terminal_outcome=='SUCCESS'
    for a in actions:
        row=loop.action_result_ledger[a['action_id']]
        assert row['execution_status']=='SUCCEEDED' and row['terminal_eligible'],row
        if a['path'].endswith('boundary.txt'):
            assert 'LOCAL_REF' in loop.action_ledger[a['action_id']]['result']
        else:
            out=decode_result(loop.action_ledger[a['action_id']]['result'])
            assert out['data']['text']==Path(a['path']).read_bytes().decode('utf-8')
    context=WebAgentToolContext(fixture,interface_name='remote')
    for name,reason in [('missing.txt','FILE_NOT_FOUND')]:
        _,_,row,calls=read_action(context,fixture/name,'R-'+reason)
        assert row['reason']==reason and not row['executed'] and not calls,row
    if os.name!='nt':
        raise RuntimeError('Windows exclusive-handle permission IT unavailable')
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.CreateFileW(str(fixture/'small.txt'),0x80000000,0,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _,_,denied,_=read_action(context,fixture/'small.txt','R-LOCKED')
        assert denied['status']=='REJECTED' and denied['reason']=='ACCESS_DENIED' and not denied['executed'],denied
    finally:
        kernel.CloseHandle(handle)
    _,_,queue,_=read_action(context,fixture/'large.txt','R-LARGE')
    assert queue['status']=='QUEUED' and not queue['data']['content_read']
    staged=context.take_pending_attachments();assert len(staged)==1
    assert hashlib.sha256(Path(staged[0]).read_bytes()).hexdigest()==s['large_sha256']
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),
        'permission_check':'actual Windows exclusive file handle; no ACL edits',
        'large_route':'real staging/hash check; external delivery NOT_TESTED here',
        'events':[e for e in events if e['event'] in {'tool_started','action_evidence_recorded','protocol_rejected'}]}
    write_json_atomic(STATE,s)


def live(s,large):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS','IT-R gate not passed'
    if large:
        assert s['phases'].get('IT-W-SMALL',{}).get('status')=='PASS','small web read gate not passed'
    from RemoteAgent.local_telegram_sender import send_and_wait
    fixture=Path(s['fixture'])
    if large:
        # Each web attempt needs new unseen markers; earlier conversation
        # content must not be able to satisfy a new upload's acceptance check.
        prior=s['phases'].get('IT-W-LARGE')
        if prior:
            s.setdefault('attempt_history',[]).append(dict(prior))
        s['large_start']='T01_LARGE_UPLOAD_START_'+uuid.uuid4().hex[:12]
        s['large_end']='T01_LARGE_UPLOAD_END_'+uuid.uuid4().hex[:12]
        large_path=fixture/('large_'+uuid.uuid4().hex[:12]+'.txt')
        middle=(fixture/'large.txt').read_text(encoding='utf-8').splitlines()[1:-1]
        data=('\n'.join([s['large_start'],*middle,s['large_end']])+'\n').encode('utf-8')
        large_path.write_bytes(data)
        s.update(large_path=str(large_path),large_bytes=len(data),large_sha256=hashlib.sha256(data).hexdigest())
        write_json_atomic(STATE,s)
        request=('只做 T01 read_file 大文字檔測試。只能使用一次 read_file，不使用 upload_file、query_project 或 run_command。'
                 '此檔大於 1,000,000 bytes，預期 Runtime 會改走附件上傳；不得把 QUEUED 當已送達。'
                 '收到附件後真正閱讀，僅回報第一行與最後一行的完整標記，不要貼整份內容、猜測內容或計算 SHA。'
                 '確認附件 ready／已送出且標記可讀後如實結案；若 Runtime 尚未確認交付則明確回報。\n檔案：\n'+str(large_path))
        phase='IT-W-LARGE'
    else:
        request=('只做 T01 read_file 一般文字檔讀取測試。只使用 read_file 分別讀以下兩個檔案；'
                 '不要上傳附件、不要 query_project 或 run_command，不寫入或刪除。'
                 '原樣回報第一檔兩行文字，以及第二檔是否為空、bytes_read 是否為0；以實際 Runtime 結果結案。\n檔案：\n'
                 +str(fixture/'small.txt')+'\n'+str(fixture/'empty.txt'))
        phase='IT-W-SMALL'
    sink={};code=send_and_wait(request=request,workspace=r'E:\workspace',root=ROOT,timeout_sec=700,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']]
    journal=TaskContinuationStore(ROOT).get(sink['task_id']) or {}
    actual=[c.get('action',{}) for c in journal.get('actions',{}).values()]
    ids=[a['action_id'] for a in actual if a.get('tool')=='read_file']
    expected_count=1 if large else 2
    assert len(ids)==expected_count and all(a.get('tool')=='read_file' for a in actual),actual
    final=task.result_ledger.get('final',{})
    text=final.get('response','')
    passed=False
    if not large:
        outcomes=[decode_result(journal['actions'][aid]['result']) for aid in ids]
        expected={str(fixture/'small.txt'):s['small_text'],str(fixture/'empty.txt'):''}
        passed=(task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS'
            and not final.get('uploaded_attachments') and all(r and r['status']=='SUCCEEDED' and
                r['data'].get('content_read') and r['data']['text']==expected[r['requested_path']] for r in outcomes))
    else:
        outcome=decode_result(journal['actions'][ids[0]]['result']) or {}
        receipts=(outcome.get('data') or {}).get('delivery_receipts') or []
        passed=(task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS'
            and len(final.get('uploaded_attachments') or [])==1 and s['large_start'] in text and s['large_end'] in text
            and outcome.get('status')=='SUCCEEDED' and len(receipts)==1
            and receipts[0]['state']=='CONSUMED' and receipts[0]['sha256']==s['large_sha256']
            and receipts[0]['request_id']==sink['request_id'] and receipts[0]['size_bytes']==s['large_bytes'])
    s['phases'][phase]={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN',
        'V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN','sender_exit_code':code,
        'request_id':sink['request_id'],'task_id':sink['task_id'],'terminal_state':task.state,
        'task_outcome':final.get('task_outcome'),'actual_actions':actual,
        'response':text,'error':task.error,'attachments':final.get('uploaded_attachments',[])}
    s['status']='PASS' if all(s['phases'].get(p,{}).get('status')=='PASS' for p in ('UT','IT-R','IT-W-SMALL','IT-W-LARGE')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s)
    assert passed,json.dumps(s['phases'][phase],ensure_ascii=False)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--phase',choices=['UT','IT-R','IT-W-SMALL','IT-W-LARGE'],required=True)
    args=parser.parse_args();s=state()
    try:
        {'UT':unit,'IT-R':runtime_it,'IT-W-SMALL':lambda v:live(v,False),'IT-W-LARGE':lambda v:live(v,True)}[args.phase](s)
    except Exception as exc:
        s['phases'].setdefault(args.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}')
        write_json_atomic(STATE,s);raise
    print('T01_PHASE_PASS',args.phase,'overall='+s['status'],'evidence='+str(STATE))


if __name__=='__main__':
    main()
