"""T07 begin transaction is not committed-file delivery. Abort is cleanup."""
import argparse,json,tempfile,uuid,hashlib,re
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.action_dependencies import command_is_proven_observation
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t03_inspect_directory_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t07_begin_file_write.json'

def is_readonly_command(action):
    if command_is_proven_observation(action):return True
    command=str(action.get('command',''))
    match=re.fullmatch(r"Get-Content\s+-Raw\s+-LiteralPath\s+('(?:[^'\r\n]|'')*')",command,re.I)
    if match:
        return command_is_proven_observation({**action,'command':'Get-Content -LiteralPath '+match[1]+' -Raw'})
    # A literal double-quoted path is equivalent only without PowerShell
    # interpolation/escape characters. Keep the oracle read-only and bounded.
    match=re.fullmatch(r'Get-Content\s+-Raw\s+-LiteralPath\s+"([^"\r\n$`]+)"',command,re.I)
    if match:
        literal="'"+match[1].replace("'","''")+"'"
        return command_is_proven_observation({**action,'command':'Get-Content -LiteralPath '+literal+' -Raw'})
    return False

def check_absence_verify(action,result,target,parent,aborted_id):
    assert action['operation']=='VERIFY' and action['verifies_action_id']==aborted_id
    assert Path(action['working_directory']).resolve()==Path(parent).resolve()
    assert is_readonly_command(action),action
    checks=action.get('verify',[])
    assert checks and all(c.get('action',c.get('type'))=='file_exists' and c.get('expect') is False for c in checks),checks
    for c in checks:
        if c.get('command'):assert is_readonly_command({'operation':'VERIFY','command':c['command']}),c
        p=Path(c['path']);p=p if p.is_absolute() else Path(parent)/p
        assert p.resolve()==Path(target).resolve(),c
    assert decode_result(result)['data']['verification_status']=='PASS'


def begin(path,write_id=None):
    return {'tool':'begin_file_write','action_id':'T07-'+uuid.uuid4().hex[:12],
            'write_id':write_id or 'W-T07-'+uuid.uuid4().hex[:12],'path':str(path),'encoding':'utf-8','overwrite':False}


def call(ctx,a):
    valid,detail=validate_tool_envelope(a);assert valid,detail
    raw=ctx.execute(a);return json.loads(raw.split('] ',1)[1]) if raw.startswith('[CHUNKED_WRITE_') else json.loads(raw),decode_result(raw),raw


def check_begin(ctx,a,payload,row):
    assert row['status']=='SUCCEEDED' and row['executed'] and not row['data']['file_committed'] and not row['data']['terminal_eligible'],row
    assert payload['write_id']==a['write_id'] and payload['state']=='OPEN' and payload['next_offset']==0
    assert not Path(a['path']).exists()
    manifest=ctx._chunked_write_manager._load_manifest(a['write_id'])
    assert manifest['state']=='OPEN' and manifest['destination_path']==a['path']
    assert Path(manifest['staging_path']).is_file() and Path(manifest['staging_path']).stat().st_size==0
    return manifest


def abort(ctx,wid):
    return call(ctx,{'tool':'abort_file_write','action_id':'T07-CLEANUP-'+uuid.uuid4().hex[:12],'write_id':wid})


def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t07-') as temp:
        root=Path(temp);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T07','只建立測試交易，不 commit：\n'+str(root),[str(root)])
        a=begin(root/'streaming.txt');payload,row,_=call(ctx,a);m=check_begin(ctx,a,payload,row)
        before=Path(m['staging_path']).stat();again,_,_=call(ctx,a)
        assert again['replay'] is True and Path(m['staging_path']).stat().st_ino==before.st_ino
        assert len(list(ctx._chunked_write_manager.staging_dir.glob('*.part')))==1
        conflicting={**a,'path':str(root/'different.txt')};_,row,_=call(ctx,conflicting)
        assert row['status']=='REJECTED' and not row['executed'] and not Path(conflicting['path']).exists()
        duplicate={**a,'action_id':'T07-NEW-ID'};_,row,_=call(ctx,duplicate)
        assert row['status']=='REJECTED' and not row['executed']
        for a2 in (begin(root/'declared.txt'),):
            a2.update(expected_size=0,expected_sha256=hashlib.sha256(b'').hexdigest(),chunk_count=1)
            payload,row,_=call(ctx,a2);check_begin(ctx,a2,payload,row);abort(ctx,a2['write_id'])
        existing=root/'existing.txt';existing.write_bytes(b'T07_KEEP_EXISTING')
        _,row,_=call(ctx,begin(existing));assert row['status']=='REJECTED' and not row['executed'] and existing.read_bytes()==b'T07_KEEP_EXISTING'
        partial=begin(root/'missing_expectations.txt');partial['expected_size']=5
        _,row,_=call(ctx,partial);assert row['status']=='REJECTED' and not row['executed']
        payload,row,_=abort(ctx,a['write_id'])
        assert payload['state']=='ABORTED' and not Path(m['staging_path']).exists() and not Path(a['path']).exists()
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':['streaming','declared','manifest/staging-real','idempotent-replay','changed-action/duplicate-write-id rejected','existing-no-overwrite','incomplete-expectations','no-output-file']}


def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture']);ctx=WebAgentToolContext(root,interface_name='remote');turns=[]
    a=begin(root/('runtime_'+uuid.uuid4().hex[:8]+'.txt'));cleanup={'tool':'abort_file_write','action_id':'T07-R-ABORT-'+uuid.uuid4().hex[:8],'write_id':a['write_id']}
    verify={'tool':'run_command','action_id':'T07-R-VERIFY-'+uuid.uuid4().hex[:8],'operation':'VERIFY',
            'command':"Test-Path -LiteralPath '"+Path(a['path']).name+"'",'working_directory':str(root),
            'success_criteria':'destination absent after transaction abort','verifies_action_id':cleanup['action_id'],
            'verify':[{'action':'file_exists','path':Path(a['path']).name,'expect':False}]}
    goal='交易建立正確並取消，沒有 commit 或產生目的檔案'
    p={**initial(),'base_evaluation':'T07 begin-only transaction with cleanup','next_action':'begin_file_write',
       'completion_contract':{'success':[goal],'failure':['交易建立或取消失敗'],'in_progress':['待建立並取消'],'interrupted':['環境阻塞']},'matched_condition':'待建立並取消'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        if len(turns)==1:return response(p,a)
        if len(turns)==2:
            raw=loop.action_ledger[a['action_id']]['result'];payload=json.loads(raw.split('] ',1)[1]);check_begin(ctx,a,payload,decode_result(raw))
            return response({**p,'next_action':'abort_file_write'},cleanup)
        if len(turns)==3:return response({**p,'next_action':'run_command'},verify)
        assert verify['action_id'] in loop.action_ledger, prompt
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
           'steps':[{'step':1,'desc':'建立並取消交易','status':'COMPLETED'}],'matched_condition':goal,'evidence_refs':[cleanup['action_id'],verify['action_id']]},
           {'tool':'final_response','action_id':'T07-R-FINAL','content':'T07_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('測試 begin_file_write，建立後用 abort_file_write 取消，再唯讀驗證目的檔不存在。不寫 chunk，不 commit，不產生檔案。\npath：\n'+a['path']+'\n只供唯讀驗證的 parent：\n'+str(root))=='T07_RUNTIME_PASS'
    manifest=ctx._chunked_write_manager._load_manifest(a['write_id'])
    assert manifest['state']=='ABORTED' and not Path(manifest['staging_path']).exists() and not Path(a['path']).exists()
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),
       'begin_result':loop.action_ledger[a['action_id']]['result'],'abort_result':loop.action_ledger[cleanup['action_id']]['result'],
       'verify_result':loop.action_ledger[verify['action_id']]['result']}


def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture']);dest=root/('web_'+uuid.uuid4().hex[:8]+'.txt');wid='W-T07-WEB-'+uuid.uuid4().hex[:12]
    request='只測試 begin_file_write 交易建立，不測寫入內容。對下方 path 用 begin_file_write，write_id 使用下方指定值、encoding=utf-8、overwrite=false；使用 streaming mode，不提供 expected_size/expected_sha256/chunk_count。確認 Runtime 回傳 OPEN/next_offset=0，這不代表目的檔已寫入。然後用 abort_file_write 取消同一 write_id，回報 ABORTED。最後以 run_command operation=VERIFY、verifies_action_id 指向實際 abort action，working_directory 使用下方 parent，唯讀確認目的檔不存在（verify file_exists expect=false）。禁止 write_file_chunk、commit_file_write、上傳或任何修改命令。只執行 begin、abort、唯讀 VERIFY 三項操作；回報交易狀態與是否尚未 commit，不得宣稱產生了目的檔案。\npath：\n'+str(dest)+'\nparent：\n'+str(root)+'\nwrite_id：'+wid
    sink={};code=send_and_wait(request=request,workspace=r'E:\card',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    if len(calls)==3 and {c['action']['tool'] for c in calls}=={'begin_file_write','abort_file_write','run_command'}:
        assert all(c['action']['write_id']==wid for c in calls if c['action']['tool']!='run_command')
        b=next(c for c in calls if c['action']['tool']=='begin_file_write');assert b['action']['path']==str(dest)
        body=json.loads(b['result'].split('] ',1)[1]);assert body['state']=='OPEN' and body['next_offset']==0
        m=json.loads((Path(r'E:\card\.agents\chunked_writes\manifests')/(hashlib.sha256(wid.encode()).hexdigest()+'.json')).read_text(encoding='utf-8'))
        assert m['state']=='ABORTED' and not Path(m['staging_path']).exists() and not dest.exists()
        aborted=next(c for c in calls if c['action']['tool']=='abort_file_write')
        checked=next(c for c in calls if c['action']['tool']=='run_command')
        check_absence_verify(checked['action'],checked['result'],dest,root,aborted['action']['action_id'])
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'path':str(dest),'write_id':wid,'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);a=p.parse_args()
    assert read_json_retry(STATE.with_name('t06_delete_path.json'))['status']=='PASS','T06 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\card\runtime_it\matrix38')/('T07-'+uuid.uuid4().hex[:12]);root.mkdir();s={'tool':'begin_file_write','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':live}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS';write_json_atomic(STATE,s)
    print('T07_PHASE_PASS',a.phase,'overall='+s['status'])


if __name__=='__main__':main()
