"""T08: actual staging bytes, UTF-8 offsets, replay, and truthful uncommitted state."""
import argparse,base64,hashlib,json,tempfile,uuid,threading,os
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.chunked_write import ChunkedWriteManager
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT,begin,call,abort,check_begin,check_absence_verify

STATE=ROOT/'localdata/runtime_it/matrix38/t08_write_file_chunk.json'
TEXTS=['T08 第一段\n','第二段 Ω\n']

def shared_read(path):
    """Windows observer must allow the writer's atomic replace/delete."""
    if os.name!='nt':return Path(path).read_bytes()
    import ctypes,msvcrt
    from ctypes import wintypes
    create=ctypes.WinDLL('kernel32',use_last_error=True).CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    handle=create(str(path),0x80000000,7,None,3,128,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except Exception:
        close=ctypes.WinDLL('kernel32',use_last_error=True).CloseHandle
        close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL;close(handle);raise
    with os.fdopen(fd,'rb') as stream:return stream.read()

def observe_staging(wid,dest,stop,observations):
    """Read only this test's staging, before abort; never instantiate a manager."""
    staging=Path(r'E:\card\.agents\chunked_writes\staging')/(hashlib.sha256(wid.encode()).hexdigest()+'.part')
    expected=[TEXTS[0].encode(),''.join(TEXTS).encode()]
    while not stop.is_set():
        try:
            # Never poll the atomically-replaced manifest: even share-delete
            # readers can race MoveFileEx on Windows. Read the append-only
            # staging only, and stop before abort once full bytes are observed.
            data=shared_read(staging)
            if data in expected:
                count=expected.index(data)+1
                record={'received_chunks':count,'bytes':len(data),'hex':data.hex(),'sha256':hashlib.sha256(data).hexdigest(),'source':str(staging)}
                if record not in observations:observations.append(record)
                if count==2:return
        except (OSError,ValueError,KeyError):pass  # atomic manifest replacement / absent before begin
        stop.wait(.02)

def chunk(wid,text,**extra):
    return {'tool':'write_file_chunk','action_id':'T08-'+uuid.uuid4().hex[:12],
            'write_id':wid,'content':text,'content_encoding':'utf-8',**extra}

def check_chunk(ctx,wid,payload,row,expected):
    manifest=ctx._chunked_write_manager._load_manifest(wid)
    assert payload['state']=='OPEN' and payload['next_offset']==len(expected)
    assert row['status']=='SUCCEEDED' and row['executed'] and row['data']['chunk_accepted']
    assert not row['data']['file_committed'] and not row['data']['terminal_eligible']
    assert manifest['next_offset']==len(expected) and Path(manifest['staging_path']).read_bytes()==expected
    assert not Path(manifest['destination_path']).exists()
    return manifest

def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t08-') as temp:
        root=Path(temp);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T08','隔離交易測試\n'+str(root),[str(root)])
        a=begin(root/'stream.txt');p,r,_=call(ctx,a);check_begin(ctx,a,p,r)
        first=chunk(a['write_id'],TEXTS[0]);p,r,_=call(ctx,first)
        expected=TEXTS[0].encode();check_chunk(ctx,a['write_id'],p,r,expected)
        p,r,_=call(ctx,first);assert p['replay'];check_chunk(ctx,a['write_id'],p,r,expected)
        for extra in ({'offset':len(expected)+1},{'offset':0},{'chunk_index':0},
                      {'chunk_size':999},{'chunk_sha256':'0'*64}):
            _,r,_=call(ctx,chunk(a['write_id'],'X',**extra))
            assert r['status'] in {'FAILED','REJECTED'} and not r['executed'],r
            m=ctx._chunked_write_manager._load_manifest(a['write_id'])
            assert Path(m['staging_path']).read_bytes()==expected and m['next_offset']==len(expected)
        second=chunk(a['write_id'],TEXTS[1]);p,r,_=call(ctx,second)
        expected+=TEXTS[1].encode();check_chunk(ctx,a['write_id'],p,r,expected)
        encoded=chunk(a['write_id'],base64.b64encode(b'T08_BINARY\x00').decode(),content_encoding='base64')
        p,r,_=call(ctx,encoded);expected+=b'T08_BINARY\x00';check_chunk(ctx,a['write_id'],p,r,expected)
        _,r,_=call(ctx,chunk(a['write_id'],'!!!',content_encoding='base64'))
        assert r['status'] in {'FAILED','REJECTED'} and not r['executed']
        _,r,_=call(ctx,{**first,'content':'DIFFERENT'});assert r['status']=='REJECTED' and not r['executed']
        abort(ctx,a['write_id'])
        _,r,_=call(ctx,chunk(a['write_id'],'AFTER_ABORT'));assert r['status'] in {'FAILED','REJECTED'} and not r['executed']
        assert not (root/'stream.txt').exists() and not ctx.take_pending_attachments()
        # Restart only the fixture manager, not the running RemoteAgent.
        a2=begin(root/'restart.txt');call(ctx,a2);call(ctx,chunk(a2['write_id'],TEXTS[0]))
        manager_root=ctx._chunked_write_manager.state_root
        ctx._chunked_write_manager=ChunkedWriteManager(manager_root,[root])
        p,r,_=call(ctx,chunk(a2['write_id'],TEXTS[1]));check_chunk(ctx,a2['write_id'],p,r,''.join(TEXTS).encode());abort(ctx,a2['write_id'])
        a3=begin(root/'crash.txt');call(ctx,a3)
        def fail_after_append(point,manifest):
            if point=='chunk_bytes_written':raise RuntimeError('T08_INJECTED_CRASH_AFTER_BYTES')
        ctx._chunked_write_manager.fault_hook=fail_after_append
        pending=chunk(a3['write_id'],TEXTS[0])
        try:call(ctx,pending)
        except RuntimeError as exc:assert str(exc)=='T08_INJECTED_CRASH_AFTER_BYTES'
        else:raise AssertionError('injected crash must not return fabricated success')
        m=ctx._chunked_write_manager._load_manifest(a3['write_id'])
        assert m['next_offset']==0 and Path(m['staging_path']).read_bytes()==TEXTS[0].encode()
        ctx._chunked_write_manager=ChunkedWriteManager(manager_root,[root])
        assert Path(m['staging_path']).read_bytes()==b''
        p,r,_=call(ctx,chunk(a3['write_id'],TEXTS[0]));check_chunk(ctx,a3['write_id'],p,r,TEXTS[0].encode());abort(ctx,a3['write_id'])
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':['UTF8 byte offsets','auto-offset/index','replay no append','gap/overlap/index/size/hash reject unchanged','base64 bytes','invalid base64','identity change','closed transaction reject','not committed','fixture-manager restart continuation','injected crash raises without fabricated success; proven rollback before retry']}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture']);ctx=WebAgentToolContext(root,interface_name='remote');turns=[]
    a=begin(root/('runtime_'+uuid.uuid4().hex[:8]+'.txt'))
    chunks=[chunk(a['write_id'],t) for t in TEXTS]
    cleanup={'tool':'abort_file_write','action_id':'T08-R-ABORT-'+uuid.uuid4().hex[:8],'write_id':a['write_id']}
    verify={'tool':'run_command','action_id':'T08-R-VERIFY-'+uuid.uuid4().hex[:8],'operation':'VERIFY',
            'command':"Test-Path -LiteralPath '"+Path(a['path']).name+"'",'working_directory':str(root),
            'success_criteria':'destination absent after test transaction abort','verifies_action_id':cleanup['action_id'],
            'verify':[{'action':'file_exists','path':Path(a['path']).name,'expect':False}]}
    actions=[a,*chunks,cleanup,verify];goal='兩塊 UTF8 staging bytes 相符並取消，目的檔不存在'
    p={**initial(),'base_evaluation':'Actual chunk staging, not committed file','next_action':'begin_file_write',
       'completion_contract':{'success':[goal],'failure':['實際 staging 不符或取消失敗'],'in_progress':['待完成分塊測試'],'interrupted':['環境阻塞']},'matched_condition':'待完成分塊測試'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        n=len(turns)
        if n in (3,4):
            previous=chunks[n-3];raw=loop.action_ledger[previous['action_id']]['result']
            check_chunk(ctx,a['write_id'],json.loads(raw.split('] ',1)[1]),decode_result(raw),''.join(TEXTS[:n-2]).encode())
        if n<=len(actions):return response({**p,'next_action':actions[n-1]['tool']},actions[n-1])
        assert verify['action_id'] in loop.action_ledger,prompt
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
                         'steps':[{'step':1,'desc':'分塊及取消驗證','status':'COMPLETED'}],
                         'matched_condition':goal,'evidence_refs':[cleanup['action_id'],verify['action_id']]},
                        {'tool':'final_response','action_id':'T08-R-FINAL','content':'T08_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('只測分塊 staging，最後取消並唯讀驗證；不 commit。\npath：\n'+a['path']+'\nparent：\n'+str(root))=='T08_RUNTIME_PASS'
    m=ctx._chunked_write_manager._load_manifest(a['write_id'])
    assert m['state']=='ABORTED' and not Path(m['staging_path']).exists() and not Path(a['path']).exists()
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'actions':loop.action_ledger}

def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture']);dest=root/('web_'+uuid.uuid4().hex[:8]+'.txt');wid='W-T08-WEB-'+uuid.uuid4().hex[:12]
    request='只驗證 write_file_chunk，不產生正式檔案。先 begin_file_write streaming mode (encoding=utf-8, overwrite=false，不提供 expected_size/hash/chunk_count)，write_id 使用指定值。再按順序用兩筆 write_file_chunk 寫下方 JSON array 的兩段文字，每段各一次，不更改文字、不重播已收 chunk；省略 offset/chunk_index/size/hash 由 Runtime 計算。回報兩次 next_offset 與 received_chunks，狀態仍 OPEN，不算檔案完成。最後 abort_file_write 同一交易，以 run_command operation=VERIFY、verifies_action_id 指向該 abort、working_directory=parent，唯讀確認 path 不存在，verify file_exists expect=false。禁止 commit、其他寫入、上傳；共 begin/兩塊/abort/唯讀verify 五項 action。\npath：\n'+str(dest)+'\nparent：\n'+str(root)+'\nwrite_id：'+wid+'\ncontent array：\n'+json.dumps(TEXTS,ensure_ascii=False)
    sink={};observations=[];stop=threading.Event();watcher=threading.Thread(target=observe_staging,args=(wid,dest,stop,observations),daemon=True);watcher.start()
    try:code=send_and_wait(request=request,workspace=r'E:\card',root=ROOT,timeout_sec=600,result_sink=sink)
    finally:stop.set();watcher.join(timeout=2)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    # Persist raw evidence even if a strict ground-truth assertion rejects it.
    s['phases']['IT-W']={'status':'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(dest),'write_id':wid,
                       'staging_readback':observations,'response':final.get('response',''),'error':task.error}
    if len(calls)==5 and [c['action']['tool'] for c in calls]==['begin_file_write','write_file_chunk','write_file_chunk','abort_file_write','run_command']:
        assert all(c['action']['write_id']==wid for c in calls[:4]) and calls[0]['action']['path']==str(dest)
        cumulative=b''
        for c,text in zip(calls[1:3],TEXTS):
            assert c['action']['content']==text
            cumulative+=text.encode();body=json.loads(c['result'].split('] ',1)[1]);row=decode_result(c['result'])
            assert body['state']=='OPEN' and body['next_offset']==len(cumulative) and row['data']['chunk_accepted'] and not row['data']['file_committed']
        m=json.loads((Path(r'E:\card\.agents\chunked_writes\manifests')/(hashlib.sha256(wid.encode()).hexdigest()+'.json')).read_text(encoding='utf-8'))
        assert m['state']=='ABORTED' and m['next_offset']==len(cumulative) and len(m['received_chunks'])==2
        assert [c['sha256'] for c in m['received_chunks']]==[hashlib.sha256(t.encode()).hexdigest() for t in TEXTS]
        for count in (1,2):
            expected=''.join(TEXTS[:count]).encode()
            assert any(o['received_chunks']==count and o['hex']==expected.hex() for o in observations),observations
        assert not Path(m['staging_path']).exists() and not dest.exists()
        v=calls[-1];check_absence_verify(v['action'],v['result'],dest,root,calls[-2]['action']['action_id'])
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(dest),'write_id':wid,'staging_readback':observations,'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args()
    assert read_json_retry(STATE.with_name('t07_begin_file_write.json'))['status']=='PASS','T07 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\card\runtime_it\matrix38')/('T08-'+uuid.uuid4().hex[:12]);root.mkdir();s={'tool':'write_file_chunk','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS';write_json_atomic(STATE,s)
    print('T08_PHASE_PASS',a.phase,'overall='+s['status'])

if __name__=='__main__':main()
