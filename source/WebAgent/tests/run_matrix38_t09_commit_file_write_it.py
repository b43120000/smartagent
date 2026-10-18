"""T09: commit means verified actual output bytes, never merely received chunks."""
import argparse,hashlib,json,tempfile,uuid
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.action_dependencies import command_is_proven_observation
from agent_core.chunked_write import ChunkedWriteManager
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT,begin,call,abort,is_readonly_command
from WebAgent.tests.run_matrix38_t08_write_file_chunk_it import chunk
STATE=ROOT/'localdata/runtime_it/matrix38/t09_commit_file_write.json'
TEXT='T09_COMMIT_OK\n原樣 UTF8 Ω\n'

def commit(wid,**extra):
    return {'tool':'commit_file_write','action_id':'T09-'+uuid.uuid4().hex[:12],'write_id':wid,**extra}

def check_commit(ctx,a,p,row,content):
    m=ctx._chunked_write_manager._load_manifest(a['write_id']);data=content.encode()
    assert row['status']=='SUCCEEDED' and row['executed'] and row['data']['file_committed']
    assert p['state']=='COMMITTED' and p['output_sha256']==hashlib.sha256(data).hexdigest()
    assert m['state']=='COMMITTED' and Path(m['destination_path']).read_bytes()==data
    assert m['expected_size']==len(data) and m['expected_sha256']==p['output_sha256']
    assert not Path(m['staging_path']).exists()
    return m

def unit(s):
    assert is_readonly_command({'operation':'VERIFY','command':'Get-Content -Raw -LiteralPath "E:\\card\\probe.txt"'})
    for bad in ('Get-Content -Raw -LiteralPath "$env:TEMP"','Get-Content -Raw -LiteralPath "$(Remove-Item x)"','Get-Content -Raw -LiteralPath "x`;y"','Get-Content -Raw -LiteralPath "x"; Remove-Item x'):
        assert not is_readonly_command({'operation':'VERIFY','command':bad}),bad
    with tempfile.TemporaryDirectory(prefix='matrix38-t09-') as temp:
        root=Path(temp);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T09','隔離 commit 測試\n'+str(root),[str(root)])
        a=begin(root/'output.txt');call(ctx,a)
        _,r,_=call(ctx,commit(a['write_id']));assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and not Path(a['path']).exists()
        call(ctx,chunk(a['write_id'],TEXT))
        for extra in ({'expected_size':999},{'expected_sha256':'0'*64}):
            _,r,_=call(ctx,commit(a['write_id'],**extra));assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and not Path(a['path']).exists()
        c=commit(a['write_id']);p,r,_=call(ctx,c);check_commit(ctx,c,p,r,TEXT)
        before=Path(a['path']).stat();p,r,_=call(ctx,c);assert p['replay'];check_commit(ctx,c,p,r,TEXT)
        assert Path(a['path']).stat().st_mtime_ns==before.st_mtime_ns
        _,r,_=call(ctx,commit(a['write_id']));assert r['status'] in {'FAILED','REJECTED'} and not r['executed']
        _,r,_=abort(ctx,a['write_id']);assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and Path(a['path']).read_bytes()==TEXT.encode()
        # Runtime must not overwrite a destination changed after begin.
        target=root/'cas.txt';target.write_bytes(b'ORIGINAL');a2=begin(target);a2['overwrite']=True;call(ctx,a2);call(ctx,chunk(a2['write_id'],TEXT))
        target.write_bytes(b'EXTERNAL_CHANGE');_,r,_=call(ctx,commit(a2['write_id']))
        assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and target.read_bytes()==b'EXTERNAL_CHANGE';abort(ctx,a2['write_id'])
        a3=begin(root/'declared.txt');data=TEXT.encode();a3.update(expected_size=len(data),expected_sha256=hashlib.sha256(data).hexdigest(),chunk_count=2)
        call(ctx,a3);call(ctx,chunk(a3['write_id'],TEXT));_,r,_=call(ctx,commit(a3['write_id']))
        assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and not Path(a3['path']).exists();abort(ctx,a3['write_id'])
        declared=begin(root/'declared_complete.txt');declared.update(expected_size=len(data),expected_sha256=hashlib.sha256(data).hexdigest(),chunk_count=1)
        call(ctx,declared);call(ctx,chunk(declared['write_id'],TEXT));done=commit(declared['write_id']);p,r,_=call(ctx,done);check_commit(ctx,done,p,r,TEXT)
        invalid=begin(root/'not_a_png.png');call(ctx,invalid);call(ctx,chunk(invalid['write_id'],'NOT_PNG'))
        _,r,_=call(ctx,commit(invalid['write_id']));assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and not Path(invalid['path']).exists();abort(ctx,invalid['write_id'])
        manager_root=ctx._chunked_write_manager.state_root
        for fault_point,expected_state in [('committing_before_replace','FAILED'),('atomic_replace_complete','COMMITTED')]:
            probe=begin(root/(fault_point+'.txt'));call(ctx,probe);call(ctx,chunk(probe['write_id'],TEXT));c2=commit(probe['write_id'])
            def crash(point,manifest):
                if point==fault_point:raise RuntimeError('T09_INJECTED_CRASH_'+point)
            ctx._chunked_write_manager.fault_hook=crash
            try:call(ctx,c2)
            except RuntimeError as exc:assert str(exc)=='T09_INJECTED_CRASH_'+fault_point
            else:raise AssertionError('injected crash must not fabricate commit success')
            ctx._chunked_write_manager=ChunkedWriteManager(manager_root,[root])
            m=ctx._chunked_write_manager._load_manifest(probe['write_id']);assert m['state']==expected_state
            if expected_state=='COMMITTED':
                target=Path(probe['path']);assert target.read_bytes()==data and m['output_sha256']==hashlib.sha256(data).hexdigest()
                stamp=target.stat().st_mtime_ns;_,r,_=call(ctx,c2)
                assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and target.stat().st_mtime_ns==stamp
            else:
                assert not Path(probe['path']).exists();abort(ctx,probe['write_id'])
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':['real UTF8 readback/hash','staging removal','replay no replace','missing chunks','supplied size/hash mismatch','declared incomplete/complete','destination CAS','committed session reject','format validation','pre/post atomic replace crash recovery without replay rewrite']}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture']);ctx=WebAgentToolContext(root,interface_name='remote');turns=[]
    a=begin(root/('runtime_'+uuid.uuid4().hex[:8]+'.txt'));c=commit(a['write_id'])
    verify={'tool':'run_command','action_id':'T09-R-VERIFY-'+uuid.uuid4().hex[:8],'operation':'VERIFY',
            'command':"Get-Content -LiteralPath '"+Path(a['path']).name+"' -Raw",'working_directory':str(root),
            'success_criteria':'committed output exists and contains expected marker','verifies_action_id':c['action_id'],
            'verify':[{'action':'file_exists','path':Path(a['path']).name,'expect':True},{'action':'file_contains','path':Path(a['path']).name,'text':'T09_COMMIT_OK','expect':True}]}
    actions=[a,chunk(a['write_id'],TEXT),c,verify];goal='目的檔真實 commit 並通過內容驗證'
    p={**initial(),'base_evaluation':'commit bytes checked independently','next_action':'begin_file_write',
       'completion_contract':{'success':[goal],'failure':['commit 或內容驗證失敗'],'in_progress':['待完成 commit'],'interrupted':['環境阻塞']},'matched_condition':'待完成 commit'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments;n=len(turns)
        if n==4:
            raw=loop.action_ledger[c['action_id']]['result'];check_commit(ctx,c,json.loads(raw.split('] ',1)[1]),decode_result(raw),TEXT)
        if n<=len(actions):return response({**p,'next_action':actions[n-1]['tool']},actions[n-1])
        assert verify['action_id'] in loop.action_ledger,prompt
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,'steps':[{'step':1,'desc':'commit 及驗證','status':'COMPLETED'}],
                         'matched_condition':goal,'evidence_refs':[c['action_id'],verify['action_id']]},
                        {'tool':'final_response','action_id':'T09-R-FINAL','content':'T09_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('只在指定路徑 commit 測試文字及唯讀驗證。\npath：\n'+a['path']+'\nparent：\n'+str(root))=='T09_RUNTIME_PASS'
    assert Path(a['path']).read_bytes()==TEXT.encode()
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'actions':loop.action_ledger}

def live(s,collect_only=False):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture']);dest=root/('web_'+uuid.uuid4().hex[:8]+'.txt');wid='W-T09-WEB-'+uuid.uuid4().hex[:12]
    if collect_only:
        previous=s['phases']['IT-W'];dest=Path(previous['path']);wid=previous['write_id']
        assert dest.parent==root and dest.name.startswith('web_')
        sink={k:previous[k] for k in ('request_id','task_id')};code=previous.get('sender_exit_code',2)
    request='只測試 commit_file_write。依序 begin_file_write streaming (utf-8, overwrite=false，不提供 expected_size/hash/chunk_count)，write_file_chunk 寫下方 JSON string 的完整原文（由 Runtime 算 bytes/offset），再 commit_file_write 同一 write_id。最後 run_command operation=VERIFY、verifies_action_id 指向實際 commit，working_directory=parent，唯讀 Get-Content -Raw；verify 檔案存在且 contains=T09_COMMIT_OK。不改行尾、不重做 commit、不上傳、不使用其他寫入工具。共 begin/chunk/commit/VERIFY 四項 action，回報 Runtime commit SHA/bytes 與真驗證結果。\npath：\n'+str(dest)+'\nparent：\n'+str(root)+'\nwrite_id：'+wid+'\ncontent JSON string：\n'+json.dumps(TEXT,ensure_ascii=False)
    request+='\n本次要特別測別名相容：verify 的兩項使用 type=file_exists 與 type=file_contains（不填 action）；內容條件使用 contains=T09_COMMIT_OK（不填 text）。兩項 path 都指向上述目的檔、expect=true，Runtime 必須真的比较這兩個條件，不能僅以命令 exit code=0 宣稱驗證 PASS。'
    if not collect_only:
        sink={};code=send_and_wait(request=request,workspace=r'E:\card',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    s['phases']['IT-W']={'status':'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(dest),'write_id':wid,'response':final.get('response',''),'error':task.error}
    if len(calls)==4 and [c['action']['tool'] for c in calls]==['begin_file_write','write_file_chunk','commit_file_write','run_command']:
        assert all(c['action']['write_id']==wid for c in calls[:3]) and Path(calls[0]['action']['path']).resolve()==dest.resolve() and calls[1]['action']['content']==TEXT
        p=json.loads(calls[2]['result'].split('] ',1)[1]);row=decode_result(calls[2]['result'])
        assert p['state']=='COMMITTED' and row['data']['file_committed'] and dest.read_bytes()==TEXT.encode()
        assert p['output_sha256']==hashlib.sha256(TEXT.encode()).hexdigest() and p['received_bytes']==len(TEXT.encode())
        m=json.loads((Path(r'E:\card\.agents\chunked_writes\manifests')/(hashlib.sha256(wid.encode()).hexdigest()+'.json')).read_text(encoding='utf-8'))
        assert m['state']=='COMMITTED' and not Path(m['staging_path']).exists()
        v=calls[-1];assert v['action']['operation']=='VERIFY' and v['action']['verifies_action_id']==calls[-2]['action']['action_id'] and Path(v['action']['working_directory']).resolve()==root.resolve()
        assert is_readonly_command(v['action'])
        checks=v['action']['verify'];assert checks and all(c.get('type') in {'file_exists','file_contains'} for c in checks) and {c.get('action',c.get('type')) for c in checks}=={'file_exists','file_contains'}
        for check in checks:
            path=Path(check['path']);path=path if path.is_absolute() else root/path
            assert path.resolve()==dest.resolve() and check.get('expect',True) is True
            if check.get('command'):assert is_readonly_command({'operation':'VERIFY','command':check['command']})
            if check.get('action',check.get('type'))=='file_contains':assert check.get('text',check.get('contains'))=='T09_COMMIT_OK'
        evidence=decode_result(v['result'])['data']
        assert evidence['verification_status']=='PASS'
        assert {e['action'] for e in evidence['postcondition_evidence']}=={'file_exists','file_contains'},evidence
        assert all(e['passed'] for e in evidence['postcondition_evidence'])
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(dest),'write_id':wid,'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);p.add_argument('--collect-only',action='store_true');a=p.parse_args()
    assert not a.collect_only or a.phase=='IT-W'
    assert read_json_retry(STATE.with_name('t08_write_file_chunk.json'))['status']=='PASS','T08 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\card\runtime_it\matrix38')/('T09-'+uuid.uuid4().hex[:12]);root.mkdir();s={'tool':'commit_file_write','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':lambda v:live(v,a.collect_only)}[a.phase](s)
    except Exception as exc:
        s['status']='FAIL'
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS';write_json_atomic(STATE,s)
    print('T09_PHASE_PASS',a.phase,'overall='+s['status'])

if __name__=='__main__':main()
