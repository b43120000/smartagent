"""T05 write_file real bytes, checks, and truthful status gates."""
import argparse,json,tempfile,uuid,hashlib
from pathlib import Path
from unittest.mock import patch
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t03_inspect_directory_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t05_write_file.json'
TEXT='T05_WRITE_OK\n中文與海鷗🐦\n'


def call(ctx,p,content):
    a={'tool':'write_file','action_id':'T05-'+uuid.uuid4().hex[:8],'path':str(p),'content':content}
    valid,detail=validate_tool_envelope(a);assert valid,detail
    raw=ctx.execute(a);return a,raw,decode_result(raw)


def check(row,p,content):
    data=content.encode('utf-8');actual=p.read_bytes()
    assert actual==data, 'written UTF-8 bytes must match content exactly (including newline)'
    assert row['status']=='SUCCEEDED' and row['executed'],row
    assert row['data']['verification_status']=='PASS' and row['data']['bytes_written']==len(data),row
    assert row['data']['sha256']==hashlib.sha256(actual).hexdigest() and row['resolved_path']==str(p)


def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t05-') as temp:
        root=Path(temp);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T05','寫入指定測試資料夾：\n'+str(root),[str(root)])
        for name,content in (('unicode.txt',TEXT),('empty.txt',''),('boundary.txt','X'*4096)):
            _,_,row=call(ctx,root/name,content);check(row,root/name,content)
        _,_,row=call(ctx,root/'unicode.txt','T05_OVERWRITE');check(row,root/'unicode.txt','T05_OVERWRITE')
        _,_,row=call(ctx,root/'new_parent'/'probe.txt',TEXT);check(row,root/'new_parent'/'probe.txt',TEXT)
        invalid={'tool':'write_file','action_id':'T05-OVER','path':str(root/'must_not_write.txt'),'content':'X'*4097}
        ok,detail=validate_tool_envelope(invalid);assert not ok and not Path(invalid['path']).exists(),detail
        real_open=Path.open
        def denied(p,*args,**kw):
            if p==root/'denied.txt' and args and 'w' in args[0]:raise PermissionError('T05 write denied')
            return real_open(p,*args,**kw)
        with patch.object(Path,'open',denied):_,_,row=call(ctx,root/'denied.txt','NO_WRITE')
        assert row['status']=='REJECTED' and not row['executed'] and row['reason']=='WRITE_ACCESS_DENIED' and not (root/'denied.txt').exists(),row
        from io import BytesIO
        def mismatched(p,*args,**kw):
            if p==root/'mismatch.txt' and args and args[0]=='rb':return BytesIO(b'WRONG_READBACK')
            return real_open(p,*args,**kw)
        with patch.object(Path,'open',mismatched):_,_,row=call(ctx,root/'mismatch.txt','T05_CHECK')
        assert row['status']=='PARTIAL' and row['executed'] and row['data']['verification_status']=='FAIL'
        assert not row['data']['terminal_eligible'] and row['data']['sha256']=='',row
        _,_,row=call(ctx,root,'NOT_A_FILE')
        assert row['status'] in {'REJECTED','FAILED'} and not row['executed']
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':['UTF8/newlines','empty','4096/4097 boundary','overwrite','new-parent','denied/no-effect','directory/no-effect']}


def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture']);ctx=WebAgentToolContext(root,interface_name='remote');turns=[]
    actions=[{'tool':'write_file','action_id':'T05-R-'+str(i),'path':str(root/n),'content':c} for i,(n,c) in enumerate((('runtime_unicode.txt',TEXT),('runtime_empty.txt','')))]
    goal='指定檔案已寫入且 bytes 與內容驗證相符'
    p={**initial(),'base_evaluation':'T05 bounded file writes','next_action':'write_file',
       'completion_contract':{'success':[goal],'failure':['寫入失敗'],'in_progress':['待寫入'],'interrupted':['環境阻塞']},'matched_condition':'待寫入'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        if len(turns)==1:return response(p,*actions)
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
          'steps':[{'step':1,'desc':'檔案寫入','status':'COMPLETED'}],'matched_condition':goal,'evidence_refs':[a['action_id'] for a in actions]},
          {'tool':'final_response','action_id':'T05-R-FINAL','content':'T05_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('只寫入以下指定檔案：\n'+'\n'.join(a['path'] for a in actions))=='T05_RUNTIME_PASS'
    for a in actions:check(decode_result(loop.action_ledger[a['action_id']]['result']),Path(a['path']),a['content'])
    from ctypes import WinDLL,c_void_p,get_last_error,WinError,wintypes
    locked=root/'locked.txt';locked.write_bytes(b'T05_KEEP_EXISTING')
    k=WinDLL('kernel32',use_last_error=True);k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype=wintypes.HANDLE;k.CloseHandle.argtypes=[wintypes.HANDLE]
    h=k.CreateFileW(str(locked),0x80000000,0,None,3,0x80,None)
    if h==c_void_p(-1).value:raise WinError(get_last_error())
    try:
        _,_,row=call(ctx,locked,'MUST_NOT_OVERWRITE')
        assert row['status']=='REJECTED' and row['reason']=='WRITE_ACCESS_DENIED' and not row['executed'],row
    finally:k.CloseHandle(h)
    assert locked.read_bytes()==b'T05_KEEP_EXISTING'
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'denied':'actual file handle lock; original bytes unchanged'}


def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture']);output=root/('web_'+uuid.uuid4().hex[:8]+'.txt');content='T05_WEB_WRITE_'+uuid.uuid4().hex[:12]+'\n中文與海鷗🐦\n'
    request='只測試 write_file。用一個 write_file action 將下方 content 原樣寫入指定 path（UTF-8、LF 換行，末尾包含一個 LF）。不能用命令、分塊工具或其他工具。根據 Runtime 的寫入／readback 檢查回報 bytes、SHA256、verification_status。不得上傳、刪除或寫其他路徑。\npath：\n'+str(output)+'\ncontent JSON 字串：\n'+json.dumps(content,ensure_ascii=False)
    sink={};code=send_and_wait(request=request,workspace=r'E:\workspace',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    if len(calls)==1 and calls[0]['action']['tool']=='write_file':
        c=calls[0];assert c['action']['path']==str(output) and c['action']['content']==content
        check(decode_result(c['result']),output,content)
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(output),'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args()
    assert read_json_retry(STATE.with_name('t04_find_file.json'))['status']=='PASS','T04 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\workspace\runtime_it\matrix38')/('T05-'+uuid.uuid4().hex[:12]);root.mkdir()
        s={'tool':'write_file','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T05_PHASE_PASS',a.phase,'overall='+s['status'])


if __name__=='__main__':main()
