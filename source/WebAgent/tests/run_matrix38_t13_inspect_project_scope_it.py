"""T13 scope/inventory tests with real fixtures and native Windows denial."""
import argparse,hashlib,json,os,subprocess,tempfile,uuid
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t13_inspect_project_scope.json'
FILES={'src/main.py':b'print("T13")\n','docs/readme.md':b'T13_SCOPE\n',
       'CMakeLists.txt':b'cmake_minimum_required(VERSION 3.15)\n','asset.bin':b'\0\1\2\3'}

def setup(parent):
    root=parent/'project';root.mkdir()
    for name,raw in FILES.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    for name in ('build/generated.txt','.venv/private.txt','localdata/logs/test.txt','source/WebAgent/state/test.txt'):
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'EXCLUDED')
    (root/'empty').mkdir();(parent/'outside.txt').write_bytes(b'OUTSIDE_SCOPE')
    return root

def action(root,**fields):
    return {'tool':'inspect_project_scope','action_id':'T13-'+uuid.uuid4().hex[:12],'workspace':str(root),**fields}

def check(raw,root):
    row=decode_result(raw)
    assert row['status']=='SUCCEEDED' and row['executed'] is True,(row,raw)
    payload=json.loads(raw)
    assert payload['schema']=='PROJECT_SNAPSHOT_V1' and Path(payload['workspace_root']).resolve()==root.resolve()
    records={r['path']:r for r in payload['files']}
    assert set(records)==set(FILES) and payload['file_count']==len(FILES),payload
    assert payload['total_bytes']==sum(map(len,FILES.values()))
    assert payload['build_files']==['CMakeLists.txt'] and len(payload['snapshot_id'])==64
    for name,content in FILES.items():
        assert records[name]['size_bytes']==len(content) and records[name]['sha256']==hashlib.sha256(content).hexdigest()
        assert (root/name).read_bytes()==content
    return payload

@contextmanager
def locked_directory(path):
    from ctypes import WinDLL,c_void_p,get_last_error,WinError,wintypes
    k=WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype=wintypes.HANDLE;k.CloseHandle.argtypes=[wintypes.HANDLE]
    h=k.CreateFileW(str(path),1,0,None,3,0x02000000,None)
    if h==c_void_p(-1).value:raise WinError(get_last_error())
    try:yield
    finally:k.CloseHandle(h)

def native(s):
    cases=[]
    with tempfile.TemporaryDirectory(prefix='matrix38-t13-') as tmp:
        parent=Path(tmp);root=setup(parent);ctx=WebAgentToolContext(parent,interface_name='remote')
        ctx.begin_run('REQ-T13','建立專案快照，僅此專案：\n'+str(root),[str(root)])
        a=action(root);payload=check(ctx.execute(a),root);cases.append('inventory/hash/bytes/exclusions/build-files')
        from agent_core.tools import _directory_listing_request
        from types import SimpleNamespace
        for text,want in [('不是列出目錄，只建立完整專案快照',False),('不要列出檔案，只建立快照',False),
                          ('Do not list files; inspect project snapshot',False),('請列出目錄內的檔案',True),
                          ('不要列出父目錄，但請列出指定目錄檔案',True)]:
            assert _directory_listing_request(SimpleNamespace(current_request_text=text)) is want,text
        ctx.begin_run('REQ-T13-NEGATION','只建立完整專案快照，不是列出目錄：\n'+str(root),[str(root)])
        check(ctx.execute(action(root)),root)
        again=check(ctx.execute(action(root)),root);assert payload['snapshot_id']==again['snapshot_id']
        # Neither hashing nor inspection publishes a project-sync snapshot.
        assert not (root/'.agents/project_context/current.json').exists() and not ctx.take_pending_attachments()
        from agent_core.project_sync import inspect_project_scope,save_project_snapshot
        save_project_snapshot(inspect_project_scope(root))
        selected=root/'src/main.py';before=selected.stat();changed=FILES['src/main.py'].replace(b'T13',b'T14')
        selected.write_bytes(changed);os.utime(selected,ns=(before.st_atime_ns,before.st_mtime_ns))
        refreshed=json.loads(ctx.execute(action(root)));record=next(r for r in refreshed['files'] if r['path']=='src/main.py')
        assert record['sha256']==hashlib.sha256(changed).hexdigest() and refreshed['snapshot_id']!=payload['snapshot_id']
        selected.write_bytes(FILES['src/main.py']);os.utime(selected,ns=(before.st_atime_ns,before.st_mtime_ns))
        cases.append('cached-same-size/mtime-byte-change-detected')
        # Denials must happen before any filesystem traversal.
        for fields in ({'workspace':str(parent)},{'workspace':''},{'path':str(root)}, {'depth':1}, {'include_files':True}):
            a=action(root);a.update(fields)
            with patch('agent_core.project_sync.os.walk',side_effect=AssertionError('forbidden scan')):
                row=decode_result(ctx.execute(a))
            assert row['status']=='REJECTED' and row['executed'] is False,(a,row)
        cases.append('no-scope-expansion/no-default-fallback/unsupported-fields')
        # Actual Windows sharing denial, not a mocked scan and no ACL change.
        with locked_directory(root/'src'):
            raw=ctx.execute(action(root));row=decode_result(raw)
            assert row['status']=='FAILED' and row['executed'] is True,(row,raw)
            assert row['reason']=='PROJECT_SCAN_INCOMPLETE' and not row['data'].get('snapshot_id')
        cases.append('native-unreadable-subdirectory/no-false-empty-snapshot')
        with locked_directory(root/'src/main.py'):
            row=decode_result(ctx.execute(action(root)))
            assert row['status']=='FAILED' and row['data']['error_reason']=='file_content_unreadable' and not row['data']['snapshot_id']
        cases.append('native-unreadable-file/no-false-snapshot')
        from agent_core.project_sync import inspect_project_scope,ProjectInventoryError
        real_stat=Path.stat
        def deny_metadata(p,*args,**kwargs):
            if p==root/'asset.bin':raise PermissionError('T13 injected stat denial')
            return real_stat(p,*args,**kwargs)
        with patch.object(Path,'stat',deny_metadata):
            try:inspect_project_scope(root)
            except ProjectInventoryError as exc:assert exc.reason=='file_metadata_unreadable'
            else:raise AssertionError('metadata denial published snapshot')
        cases.append('metadata-denial-fault-injection/no-false-snapshot')
        check(ctx.execute(action(root)),root)
        empty=parent/'empty_project';empty.mkdir();ctx.begin_run('REQ-T13-EMPTY','專案快照：\n'+str(empty),[str(empty)])
        raw=ctx.execute(action(empty));p=json.loads(raw)
        assert decode_result(raw)['status']=='SUCCEEDED' and p['file_count']==0 and p['total_bytes']==0
        for bad in (empty/'missing',parent/'outside.txt'):
            row=decode_result(ctx.execute(action(bad)));assert row['status']=='REJECTED' and row['executed'] is False
        cases.append('empty/missing/non-directory')
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':cases}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['target']);ctx=WebAgentToolContext(Path(s['fixture']),interface_name='remote');turns=[];a=action(root)
    goal='取得指定小專案完整且精確的快照 inventory，未上傳附件'
    p={**initial(),'next_action':'inspect_project_scope','base_evaluation':'bounded synthetic project inventory',
       'completion_contract':{'success':[goal],'failure':['快照不完整'],'in_progress':['待查核快照'],'interrupted':['客觀環境阻塞']},'matched_condition':'待查核快照'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments and len(turns)<=2
        if len(turns)==1:return response(p,a)
        check(loop.action_ledger[a['action_id']]['result'],root)
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
                         'steps':[{'step':1,'desc':'專案快照','status':'COMPLETED'}],'matched_condition':goal,'evidence_refs':[a['action_id']]},
                        {'tool':'final_response','action_id':'T13-FINAL','content':'T13_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(Path(s['fixture']),planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('建立指定專案快照，不是列目錄，不擴大範圍、不上傳：\n'+str(root))=='T13_RUNTIME_PASS'
    with locked_directory(root/'src'):
        row=decode_result(ctx.execute(action(root)));assert row['status']=='FAILED' and row['reason']=='PROJECT_SCAN_INCOMPLETE'
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','actions':loop.action_ledger,'rounds':len(turns),'denial':'native exclusive directory handle'}

def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    if s['phases'].get('IT-W'):s.setdefault('attempt_history',[]).append(dict(s['phases']['IT-W']))
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    prompt=('T13 inspect_project_scope 測試。只對以下小專案取得完整 snapshot inventory，不是列出目錄：\n'+s['target']+
            '\n只執行一次 inspect_project_scope，workspace 明確填此路徑；不要 path/depth/include_files。回報實際 file_count、total_bytes、build_files、snapshot_id 及每檔 SHA256。'
            '\n禁止掃父目錄、不 project_sync、不 query、不上傳、不修改任何檔案、不補跑命令。依真實 Runtime 結果判斷，完整快照成功才 SUCCESS。')
    sink={};code=send_and_wait(request=prompt,workspace=r'E:\card',root=ROOT,timeout_sec=900,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];journal=TaskContinuationStore(ROOT).get(task.task_id) or {}
    actions=list(journal.get('actions',{}).values());final=task.result_ledger.get('final',{})
    record={'status':'UNKNOWN',**sink,'sender_exit_code':code,'actions':actions,'task_state':task.state,
            'outcome':final.get('task_outcome'),'error':task.error,'response':final.get('response','')};s['phases']['IT-W']=record
    assert len(actions)==1 and actions[0]['action']['tool']=='inspect_project_scope',record
    assert Path(actions[0]['action']['workspace']).resolve()==Path(s['target']).resolve()
    check(actions[0]['result'],Path(s['target']))
    assert task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments'),record
    record.update(status='PASS',E='PASS',V='PASS',R='PASS')

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);args=p.parse_args()
    assert read_json_retry(STATE.with_name('t12_aggregate_verification.json'))['status']=='PASS','T12 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        parent=Path(r'E:\card\runtime_it\matrix38')/('T13-'+uuid.uuid4().hex[:12]);parent.mkdir();root=setup(parent)
        s={'tool':'inspect_project_scope','status':'IN_PROGRESS','fixture':str(parent),'target':str(root),'phases':{},'rca':[]}
    s['rca']=[{'layer':'RUNTIME_INVENTORY','reason':'os.walk silently ignored unreadable subdirectory and stat failures; incomplete inventory published as SUCCEEDED snapshot',
               'fix':'onerror and metadata/content failures raise ProjectInventoryError; inspect tool returns FAILED/PROJECT_SCAN_INCOMPLETE with no snapshot_id; other snapshot consumers fail closed'},
              {'layer':'RUNTIME_INTENT_GUARD','reason':'negated listing mention incorrectly rejected affirmative inspect_project_scope; rejection reason falsely labeled DELETE_PREFLIGHT_REJECTED',
               'fix':'remove explicit negated listing mentions while preserving separate positive directives; all scoped inspections receive TOOL_SCOPE_REJECTED reason'}]
    try:{'UT':native,'IT-R':runtime,'IT-W':live}[args.phase](s)
    except Exception as exc:
        s['phases'].setdefault(args.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}'[:4000]);write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(k,{}).get('status')=='PASS' for k in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T13_PHASE_PASS',args.phase,'overall='+s['status'])

if __name__=='__main__':main()
