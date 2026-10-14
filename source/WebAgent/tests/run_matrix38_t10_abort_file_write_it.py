"""T10: cancel staging only, preserve destination and other transactions."""
import argparse,hashlib,json,tempfile,uuid
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.action_dependencies import command_is_proven_observation
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT,begin,call,is_readonly_command
from WebAgent.tests.run_matrix38_t08_write_file_chunk_it import chunk
from WebAgent.tests.run_matrix38_t09_commit_file_write_it import commit
STATE=ROOT/'localdata/runtime_it/matrix38/t10_abort_file_write.json'
BASE='T10_KEEP_ORIGINAL\n'
PART='T10_STAGING_ONLY\n暫存片段\n'

def cancel(wid):return {'tool':'abort_file_write','action_id':'T10-'+uuid.uuid4().hex[:12],'write_id':wid}

def check_abort(ctx,wid,p,row,dest):
    m=ctx._chunked_write_manager._load_manifest(wid)
    assert p['state']=='ABORTED' and row['status']=='SUCCEEDED' and row['executed']
    assert not row['data']['file_committed'] and m['state']=='ABORTED' and not Path(m['staging_path']).exists()
    assert dest.read_bytes()==BASE.encode()
    return m

def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t10-') as temp:
        root=Path(temp);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T10','隔離 abort 測試\n'+str(root),[str(root)])
        dest=root/'preserved.txt';dest.write_bytes(BASE.encode());a=begin(dest);a['overwrite']=True;call(ctx,a);call(ctx,chunk(a['write_id'],PART))
        other=begin(root/'other.txt');call(ctx,other);call(ctx,chunk(other['write_id'],'OTHER_TRANSACTION'))
        manager=ctx._chunked_write_manager;other_m=manager._load_manifest(other['write_id']);other_before=Path(other_m['staging_path']).read_bytes()
        _,r,_=call(ctx,cancel('W-T10-NOT-FOUND'));assert r['status'] in {'FAILED','REJECTED'} and not r['executed']
        aborted=cancel(a['write_id']);p,r,_=call(ctx,aborted);check_abort(ctx,a['write_id'],p,r,dest)
        p,r,_=call(ctx,aborted);assert p['replay'];check_abort(ctx,a['write_id'],p,r,dest)
        p,r,_=call(ctx,cancel(a['write_id']));check_abort(ctx,a['write_id'],p,r,dest)
        assert manager._load_manifest(other['write_id'])['state']=='OPEN' and Path(other_m['staging_path']).read_bytes()==other_before
        for late in (commit(a['write_id']),chunk(a['write_id'],'LATE')):
            _,r,_=call(ctx,late);assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and dest.read_bytes()==BASE.encode()
        committed=commit(other['write_id']);call(ctx,committed);_,r,_=call(ctx,cancel(other['write_id']))
        assert r['status'] in {'FAILED','REJECTED'} and not r['executed'] and (root/'other.txt').read_bytes()==other_before
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':['partial staging removed','destination preserved','other OPEN transaction untouched','missing write id','same action replay','new repeated abort','late chunk/commit rejected','committed abort rejected']}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture']);dest=root/('runtime_'+uuid.uuid4().hex[:8]+'.txt');dest.write_bytes(BASE.encode())
    ctx=WebAgentToolContext(root,interface_name='remote');turns=[];a=begin(dest);a['overwrite']=True;c=cancel(a['write_id'])
    verify={'tool':'run_command','action_id':'T10-R-VERIFY-'+uuid.uuid4().hex[:8],'operation':'VERIFY',
            'command':"Get-Content -LiteralPath '"+dest.name+"' -Raw",'working_directory':str(root),
            'success_criteria':'abort leaves original destination unchanged','verifies_action_id':c['action_id'],
            'verify':[{'action':'file_exists','path':dest.name,'expect':True},{'action':'file_contains','path':dest.name,'text':'T10_KEEP_ORIGINAL','expect':True}]}
    actions=[a,chunk(a['write_id'],PART),c,verify];goal='交易取消且原目的檔不變'
    p={**initial(),'base_evaluation':'partial staging cancellation preserves baseline','next_action':'begin_file_write',
       'completion_contract':{'success':[goal],'failure':['取消失敗或目的檔變動'],'in_progress':['待取消及驗證'],'interrupted':['環境阻塞']},'matched_condition':'待取消及驗證'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments;n=len(turns)
        if n==4:
            raw=loop.action_ledger[c['action_id']]['result'];check_abort(ctx,a['write_id'],json.loads(raw.split('] ',1)[1]),decode_result(raw),dest)
        if n<=len(actions):return response({**p,'next_action':actions[n-1]['tool']},actions[n-1])
        assert verify['action_id'] in loop.action_ledger,prompt
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,'steps':[{'step':1,'desc':'取消及原檔驗證','status':'COMPLETED'}],
                         'matched_condition':goal,'evidence_refs':[c['action_id'],verify['action_id']]},
                        {'tool':'final_response','action_id':'T10-R-FINAL','content':'T10_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('測試暫存片段後 abort，不 commit，原目的檔不得更動。\npath：\n'+str(dest)+'\nparent：\n'+str(root))=='T10_RUNTIME_PASS'
    assert dest.read_bytes()==BASE.encode()
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'actions':loop.action_ledger}

def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture']);dest=root/('web_'+uuid.uuid4().hex[:8]+'.txt');dest.write_bytes(BASE.encode());wid='W-T10-WEB-'+uuid.uuid4().hex[:12]
    request='只測試 abort_file_write 保護原目的檔。測試器已建立下方 path，其原內容是 JSON string '+json.dumps(BASE)+'。依序 begin_file_write streaming (utf-8, overwrite=true，不提供 expected_size/hash/chunk_count)，write_id 指定如下；用一筆 write_file_chunk 寫下方暫存 content；然後 abort_file_write 同一交易，不可 commit。最後 run_command operation=VERIFY、verifies_action_id 指向實際 abort、working_directory=parent，唯讀 Get-Content -Raw；verify 檔案存在且 file_contains text=T10_KEEP_ORIGINAL expect=true。共 begin/chunk/abort/VERIFY 四項 action，不改原目的檔、不上傳、不使用其他修改工具。回報 ABORTED、file_committed=false 與原檔保存驗證。\npath：\n'+str(dest)+'\nparent：\n'+str(root)+'\nwrite_id：'+wid+'\nstaging content JSON string：\n'+json.dumps(PART,ensure_ascii=False)
    sink={};code=send_and_wait(request=request,workspace=r'E:\workspace',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    s['phases']['IT-W']={'status':'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(dest),'write_id':wid,'response':final.get('response',''),'error':task.error}
    if len(calls)==4 and [c['action']['tool'] for c in calls]==['begin_file_write','write_file_chunk','abort_file_write','run_command']:
        assert all(c['action']['write_id']==wid for c in calls[:3]) and calls[0]['action']['path']==str(dest) and calls[0]['action']['overwrite'] is True and calls[1]['action']['content']==PART
        p=json.loads(calls[2]['result'].split('] ',1)[1]);row=decode_result(calls[2]['result'])
        assert p['state']=='ABORTED' and not row['data']['file_committed'] and dest.read_bytes()==BASE.encode()
        m=json.loads((Path(r'E:\workspace\.agents\chunked_writes\manifests')/(hashlib.sha256(wid.encode()).hexdigest()+'.json')).read_text(encoding='utf-8'))
        assert m['state']=='ABORTED' and m['next_offset']==len(PART.encode()) and not Path(m['staging_path']).exists()
        v=calls[-1];assert v['action']['operation']=='VERIFY' and v['action']['verifies_action_id']==calls[-2]['action']['action_id'] and v['action']['working_directory']==str(root)
        assert is_readonly_command(v['action'])
        checks=v['action']['verify'];assert checks and {c.get('action',c.get('type')) for c in checks}=={'file_exists','file_contains'}
        for check in checks:
            path=Path(check['path']);path=path if path.is_absolute() else root/path
            assert path.resolve()==dest.resolve() and check.get('expect',True) is True
            if check.get('command'):assert is_readonly_command({'operation':'VERIFY','command':check['command']})
            if check.get('action',check.get('type'))=='file_contains':assert check.get('text',check.get('contains'))=='T10_KEEP_ORIGINAL'
        evidence=decode_result(v['result'])['data'];assert evidence['verification_status']=='PASS'
        assert {e['action'] for e in evidence['postcondition_evidence']}=={'file_exists','file_contains'}
        assert all(e['passed'] for e in evidence['postcondition_evidence'])
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W'].update(status='PASS' if passed else 'FAIL',E='PASS' if passed else 'UNKNOWN',V='PASS' if passed else 'UNKNOWN',R='PASS' if passed else 'UNKNOWN')
    assert passed,s['phases']['IT-W']

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args()
    assert read_json_retry(STATE.with_name('t09_commit_file_write.json'))['status']=='PASS','T09 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\workspace\runtime_it\matrix38')/('T10-'+uuid.uuid4().hex[:12]);root.mkdir();s={'tool':'abort_file_write','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}'[:2000]);write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS';write_json_atomic(STATE,s)
    print('T10_PHASE_PASS',a.phase,'overall='+s['status'])

if __name__=='__main__':main()
