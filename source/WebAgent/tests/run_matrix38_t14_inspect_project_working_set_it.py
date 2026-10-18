"""T14 exact working-set inspection with independently read native files."""
import argparse,hashlib,json,os,subprocess,tempfile,uuid
from pathlib import Path
from unittest.mock import patch
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.project_sync import inspect_project_scope,save_project_snapshot
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t14_inspect_project_working_set.json'
SELECTED=['src/a.py','docs/b.md']

def setup(parent):
    root=parent/'project';root.mkdir()
    for name,content in [('src/a.py',b'T14_A0'),('docs/b.md',b'T14_B'),('unrelated.txt',b'T14_UNUSED')]:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
    (parent/'outside.txt').write_bytes(b'OUTSIDE_SCOPE')
    return root

def action(root,paths=SELECTED):
    return {'tool':'inspect_project_working_set','action_id':'T14-'+uuid.uuid4().hex[:12],'workspace':str(root),'paths':list(paths)}

def check(raw,root,paths=SELECTED):
    row=decode_result(raw);p=json.loads(raw)
    assert row['status']=='SUCCEEDED' and row['executed'] is True,(p,row)
    assert p['status']=='READY' and p['scope']=='TASK_WORKING_SET' and p['attachments_uploaded']==0
    assert p['requested_paths']==paths and not p['missing_paths'] and p['file_count']==len(paths)
    assert Path(p['workspace_root']).resolve()==root.resolve()
    assert {r['path'] for r in p['files']}==set(paths)
    for r in p['files']:
        content=(root/r['path']).read_bytes()
        assert r['size_bytes']==len(content) and r['sha256']==hashlib.sha256(content).hexdigest(),r
    return p

def native(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t14-') as tmp:
        parent=Path(tmp);root=setup(parent);ctx=WebAgentToolContext(parent,interface_name='remote')
        ctx.begin_run('REQ-T14','查核精確工作集：\n'+str(root),[str(root)])
        baseline=inspect_project_scope(root);save_project_snapshot(baseline)
        with patch('agent_core.project_sync._records',side_effect=AssertionError('must not inventory entire project')):
            first=check(ctx.execute(action(root)),root)
            (root/'unrelated.txt').write_bytes(b'T14_UNRELATED_CHANGED')
            other=check(ctx.execute(action(root)),root);assert first['snapshot_id']==other['snapshot_id']
            selected=root/'src/a.py';old=selected.stat()
            # Same size + restored timestamp must not hide actual selected bytes.
            selected.write_bytes(b'T14_A1');os.utime(selected,ns=(old.st_atime_ns,old.st_mtime_ns))
            changed=check(ctx.execute(action(root)),root);assert changed['snapshot_id']!=first['snapshot_id']
        raw=ctx.execute(action(root,['src/a.py','missing.py']));p=json.loads(raw)
        assert decode_result(raw)['status']=='PARTIAL' and p['status']=='INCOMPLETE' and p['missing_paths']==['missing.py'] and p['file_count']==1
        for paths in ([],['../outside.txt'],[str(parent/'outside.txt')]):
            row=decode_result(ctx.execute(action(root,paths)));assert row['status']=='REJECTED' and row['executed'] is False,(paths,row)
        # The readable workspace is not permission to expand this request scope.
        row=decode_result(ctx.execute(action(parent,['outside.txt'])))
        assert row['status']=='REJECTED' and row['reason']=='TOOL_SCOPE_REJECTED' and row['executed'] is False,row
        ctx.begin_run('REQ-T14-EXACT','只查核指定兩檔',[str(root/p) for p in SELECTED])
        check(ctx.execute(action(root)),root)
        leading=root/' src/a.py';leading.parent.mkdir();leading.write_bytes(b'T14_LEADING_DIRECTORY')
        ctx.begin_run('REQ-T14-SPACES','只允許指定檔案',[str(leading)])
        with patch('agent_core.project_sync._sha256_file',side_effect=AssertionError('must not read normalized alternate file')):
            row=decode_result(ctx.execute(action(root,[' src/a.py'])))
        assert row['status']=='REJECTED' and row['executed'] is False,row
        # A literal leading-space path component remains usable via absolute
        # spelling; do not silently turn it into the different src directory.
        raw=ctx.execute(action(root,[str(leading)]));literal=json.loads(raw)
        assert decode_result(raw)['status']=='SUCCEEDED' and literal['requested_paths']==[' src/a.py']
        assert literal['files'][0]['sha256']==hashlib.sha256(leading.read_bytes()).hexdigest()
        gp=parent/'git_case';gp.mkdir();gr=setup(gp)
        def git(*args):return subprocess.run(['git','-C',str(gr),*args],check=True,capture_output=True,text=True).stdout.strip()
        git('-c','init.templateDir=','init')
        no_hooks=gr/'.git/it_no_hooks';no_hooks.mkdir()
        for key,value in [('user.name','RuntimeIT'),('user.email','runtime-it@example.invalid'),('commit.gpgsign','false'),('core.hooksPath',str(no_hooks))]:git('config','--local',key,value)
        git('add','src/a.py','docs/b.md','unrelated.txt');git('commit','-m','T14_FIXTURE_ONLY')
        save_project_snapshot(inspect_project_scope(gr));gc=WebAgentToolContext(gr,interface_name='remote')
        gc.begin_run('REQ-T14-GIT','精確 working set：\n'+str(gr),[str(gr)])
        gfirst=check(gc.execute(action(gr)),gr)
        assert gfirst['inventory_mode']=='GIT_FIRST_HYBRID' and gfirst['git_head']==git('rev-parse','HEAD')
        git('update-index','--assume-unchanged','src/a.py');p=gr/'src/a.py';before=p.stat()
        p.write_bytes(b'T14_A1');os.utime(p,ns=(before.st_atime_ns,before.st_mtime_ns))
        gchanged=check(gc.execute(action(gr)),gr)
        assert gchanged['snapshot_id']!=gfirst['snapshot_id'] and gchanged['file_count']==2
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS',
        'cases':['exact-files/no-full-inventory','unrelated-edit-stable','same-size/mtime-selected-edit-detected','missing-partial','empty/traversal/absolute-outside-rejected','exact-request-files-scope','git-first/native-commit/index-assume-unchanged-cannot-hide-bytes']}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['target']);ctx=WebAgentToolContext(Path(s['fixture']),interface_name='remote');a=action(root);turns=[]
    goal='只取得兩個指定檔案的精確工作集與真實 SHA，沒有附件'
    p={**initial(),'next_action':'inspect_project_working_set','base_evaluation':'bounded selected source inspection',
       'completion_contract':{'success':[goal],'failure':['工作集不完整'],'in_progress':['等待實際讀取'],'interrupted':['環境阻塞']},'matched_condition':'等待實際讀取'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments and len(turns)<=2
        if len(turns)==1:return response(p,a)
        check(loop.action_ledger[a['action_id']]['result'],root)
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
                         'steps':[{'step':1,'desc':'精確工作集','status':'COMPLETED'}],'matched_condition':goal,'evidence_refs':[a['action_id']]},
                        {'tool':'final_response','action_id':'T14-FINAL','content':'T14_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(Path(s['fixture']),planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('只查核指定專案的 src/a.py 與 docs/b.md working set，不掃全案、不上傳：\n'+str(root))=='T14_RUNTIME_PASS'
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','actions':loop.action_ledger,'rounds':len(turns)}

def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    prompt=('T14 exact working set 測試。專案根目錄：\n'+s['target']+
            '\n只執行一次 inspect_project_working_set，workspace 明確填此根目錄，paths 精確為 ["src/a.py","docs/b.md"]。'
            '\n回報指定兩檔的 path、size_bytes、SHA256、snapshot_id、missing_paths。不得把 unrelated.txt 納入，不 inspect_project_scope、不 project_sync、不 query、不上傳、不修改或額外執行命令。根據實際 Runtime 結果結案。')
    sink={};code=send_and_wait(request=prompt,workspace=r'E:\card',root=ROOT,timeout_sec=900,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];journal=TaskContinuationStore(ROOT).get(task.task_id) or {}
    actions=list(journal.get('actions',{}).values());final=task.result_ledger.get('final',{})
    record={'status':'UNKNOWN',**sink,'sender_exit_code':code,'actions':actions,'task_state':task.state,
            'outcome':final.get('task_outcome'),'error':task.error,'response':final.get('response','')};s['phases']['IT-W']=record
    assert len(actions)==1 and actions[0]['action']['tool']=='inspect_project_working_set',record
    assert actions[0]['action']['paths']==SELECTED and Path(actions[0]['action']['workspace']).resolve()==Path(s['target']).resolve()
    check(actions[0]['result'],Path(s['target']))
    assert task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments'),record
    record.update(status='PASS',E='PASS',V='PASS',R='PASS')

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);args=p.parse_args()
    assert read_json_retry(STATE.with_name('t13_inspect_project_scope.json'))['status']=='PASS','T13 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        parent=Path(r'E:\card\runtime_it\matrix38')/('T14-'+uuid.uuid4().hex[:12]);parent.mkdir();root=setup(parent)
        s={'tool':'inspect_project_working_set','status':'IN_PROGRESS','fixture':str(parent),'target':str(root),'phases':{},'rca':[]}
    s['rca']=[{'layer':'RUNTIME_CONTENT_EVIDENCE','reason':'unchanged size/mtime caused old SHA reuse for changed selected bytes',
               'fix':'fresh SHA256 for exact working set; Runtime inspect_project_scope also verifies cached content without changing background sync cache policy'},
              {'layer':'RUNTIME_ADMISSION','reason':'invalid/outside working-set inputs were UNKNOWN and exact request scope was not checked; preview/execution whitespace normalization differed',
               'fix':'working-set preflight checks every resolved selected path against project root and explicit request roots; surrounding whitespace rejected consistently; literal internal space paths preserved; known invalid inputs REJECTED/no execution'}]
    try:{'UT':native,'IT-R':runtime,'IT-W':live}[args.phase](s)
    except Exception as exc:
        s['phases'].setdefault(args.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}'[:4000]);write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(k,{}).get('status')=='PASS' for k in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T14_PHASE_PASS',args.phase,'overall='+s['status'])

if __name__=='__main__':main()
