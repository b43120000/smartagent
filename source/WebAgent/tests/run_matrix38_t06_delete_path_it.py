"""T06 safe fixture deletion; live approval is never synthesized."""
import argparse,json,tempfile,uuid
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.security_approval import SecurityApprovalLedger
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t03_inspect_directory_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t06_delete_path.json'


def action(target,recursive=False):
    return {'tool':'delete_path','action_id':'T06-'+uuid.uuid4().hex[:8],'path':str(target),'recursive':recursive,'reason':'delete only the isolated matrix38 synthetic fixture'}


def fixture_approval(ctx,root,targets,change=None):
    ctx.security_approval_ledger_root=root
    def approve(record,manifest):
        target=Path(manifest['target']).resolve()
        assert target in targets and root in target.parents and target.exists()
        if change:change(target)
        SecurityApprovalLedger(root).decide(record['approval_id'],approve=True,chat_id='',actor='matrix38-isolated-test-fixture')
    ctx.configure_security_approval(approve)


def unit(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t06-') as temp:
        root=Path(temp);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T06','刪除隔離測試目標：\n'+str(root),[str(root)]);ctx.current_task_id='TASK-T06-UNIT'
        pending=root/'pending.txt';pending.write_bytes(b'KEEP_PENDING')
        row=decode_result(ctx.execute(action(pending)))
        assert row['status']=='REJECTED' and row['reason']=='USER_APPROVAL_REQUIRED' and not row['executed'] and pending.exists(),row
        file=root/'delete_file.txt';file.write_bytes(b'DISPOSABLE');empty=root/'delete_empty';empty.mkdir()
        tree=root/'delete_tree';tree.mkdir();(tree/'nested.txt').write_bytes(b'DISPOSABLE')
        fixture_approval(ctx,root,{file,empty,tree})
        for p,recursive in ((file,False),(empty,False),(tree,True)):
            row=decode_result(ctx.execute(action(p,recursive)))
            assert row['status']=='SUCCEEDED' and row['executed'] and row['data']['status']=='DELETED' and not p.exists(),row
        for p in (root,root/'missing','relative_probe','../parent_probe',str(root/'*.txt')):
            row=decode_result(ctx.execute(action(p)))
            assert row['status']=='REJECTED' and not row['executed'],row
        changed=root/'changed.txt';changed.write_bytes(b'ORIGINAL')
        fixture_approval(ctx,root,{changed},change=lambda p:p.write_bytes(b'CHANGED_BEFORE_DELETE'))
        row=decode_result(ctx.execute(action(changed)))
        assert row['status']=='REJECTED' and row['reason']=='TARGET_CHANGED' and not row['executed'] and changed.read_bytes()==b'CHANGED_BEFORE_DELETE',row
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','approval_scope':'only isolated test fixtures; no production grants',
        'cases':['no-approval/no-effect','file/empty-directory/recursive-tree','root/missing/relative/traversal/glob rejected','manifest-changed/no-delete']}


def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture']);target=root/'runtime_delete';target.mkdir(exist_ok=True);assert not list(target.iterdir())
    ctx=WebAgentToolContext(root,interface_name='remote');fixture_approval(ctx,root,{target})
    deletion=action(target);deletion['action_id']='T06-R-DELETE'
    verify={'tool':'run_command','action_id':'T06-R-VERIFY','operation':'VERIFY','command':"Test-Path -LiteralPath 'runtime_delete'",'working_directory':str(root),
            'success_criteria':'isolated directory absent after actual delete','verifies_action_id':deletion['action_id'],
            'verify':[{'action':'file_exists','path':'runtime_delete','expect':False}]}
    turns=[];goal='實際刪除指定測試目錄且關聯唯讀驗證 PASS'
    p={**initial(),'base_evaluation':'T06 isolated deletion and linked verification','next_action':'delete_path',
       'completion_contract':{'success':[goal],'failure':['刪除或驗證失敗'],'in_progress':['待刪除與驗證'],'interrupted':['需要人工批准']},'matched_condition':'待刪除與驗證'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        if len(turns)==1:return response(p,deletion)
        if len(turns)==2:return response({**p,'next_action':'run_command'},verify)
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,'steps':[{'step':1,'desc':'刪除並驗證','status':'COMPLETED'}],
            'matched_condition':goal,'evidence_refs':[deletion['action_id'],verify['action_id']]},
            {'tool':'final_response','action_id':'T06-R-FINAL','content':'T06_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('只刪除新建空測試目錄並唯讀驗證：\n'+str(target)+'\n唯讀 working_directory：\n'+str(root))=='T06_RUNTIME_PASS'
    assert not target.exists() and loop.action_result_ledger[deletion['action_id']]['effective_verification_status']=='PASS'
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),
        'approval_scope':'test fixture callback only; production approval NOT_TESTED here',
        'delete_result':loop.action_ledger[deletion['action_id']]['result'],'verify_result':loop.action_ledger[verify['action_id']]['result']}


def live(s, reuse_target=False):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture'])
    if reuse_target:
        target=Path(s['live_target'])
        assert target.parent==root and target.name.startswith('web_disposable_')
        assert target.resolve()==target and target.is_dir() and not target.is_junction() and not target.is_symlink()
    else:
        target=root/('web_disposable_'+uuid.uuid4().hex[:8]);target.mkdir()
    assert not list(target.iterdir())
    s['live_target']=str(target);s['phases']['IT-W']={'status':'RUNNING'};write_json_atomic(STATE,s)
    request='只測試 delete_path。下方 target 是測試器建立且尚未刪除的空目錄，只能刪除這一個；使用 delete_path recursive=false，不可刪除 parent 或其他路徑。保留 Runtime 所有安全檢查與人工批准，不得猜測或繞過批准。刪除成功後，以 run_command operation=VERIFY、verifies_action_id 指向實際 delete action，working_directory 使用下方 parent，唯讀確認 target 不存在（verify file_exists expect=false）。若需要批准請等待批准；不寫入、不上傳、不重做已成功刪除。\ntarget：\n'+str(target)+'\n只供唯讀驗證的 parent：\n'+str(root)
    sink={};code=send_and_wait(request=request,workspace=r'E:\workspace',root=ROOT,timeout_sec=600,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];j=TaskContinuationStore(ROOT).get(sink['task_id']) or {};calls=list(j.get('actions',{}).values());final=task.result_ledger.get('final',{});passed=False
    deletes=[c for c in calls if c['action']['tool']=='delete_path'];verifies=[c for c in calls if c['action']['tool']=='run_command' and c['action'].get('operation')=='VERIFY']
    if len(deletes)==1 and verifies and not target.exists():
        d=deletes[0];assert d['action']['path']==str(target) and decode_result(d['result'])['status']=='SUCCEEDED'
        assert any(v['action'].get('verifies_action_id')==d['action']['action_id'] and decode_result(v['result'])['data']['verification_status']=='PASS' for v in verifies)
        passed=task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments')
    s['phases']['IT-W']={'status':'PASS' if passed else 'FAIL','E':'PASS' if passed else 'UNKNOWN','V':'PASS' if passed else 'UNKNOWN','R':'PASS' if passed else 'UNKNOWN',**sink,'sender_exit_code':code,'actions':calls,'target':str(target),'response':final.get('response',''),'error':task.error}
    assert passed,s['phases']['IT-W']


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True)
    p.add_argument('--reuse-live-target',action='store_true');a=p.parse_args()
    assert read_json_retry(STATE.with_name('t05_write_file.json'))['status']=='PASS','T05 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\workspace\runtime_it\matrix38')/('T06-'+uuid.uuid4().hex[:12]);root.mkdir()
        s={'tool':'delete_path','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:{'UT':unit,'IT-R':runtime,'IT-W':lambda v:live(v,a.reuse_live_target)}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}');write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(x,{}).get('status')=='PASS' for x in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T06_PHASE_PASS',a.phase,'overall='+s['status'])


if __name__=='__main__':main()
