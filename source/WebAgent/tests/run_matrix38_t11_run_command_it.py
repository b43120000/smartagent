"""T11 native command matrix; no mocks for command execution or file effects."""
import argparse,hashlib,json,subprocess,tempfile,threading,uuid,time
from pathlib import Path
from unittest.mock import patch
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t11_run_command.json'

def setup(root):
    source=root/'cmake_source';source.mkdir();(source/'build_input.txt').write_bytes(b'T11_BUILD_OK')
    (source/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.15)\nproject(T11Fixture NONE)\nadd_custom_target(probe ALL COMMAND "${CMAKE_COMMAND}" -E copy "${CMAKE_CURRENT_SOURCE_DIR}/build_input.txt" "${CMAKE_BINARY_DIR}/T11_BUILD_OK.txt")\n',encoding='utf-8')
    subprocess.run(['cmake','-S',str(source),'-B',str(root/'build'),'-G','Ninja'],check=True,capture_output=True)
    repo=root/'repo';repo.mkdir();subprocess.run(['git','-C',str(repo),'init'],check=True,capture_output=True)
    (repo/'no_hooks').mkdir()
    for key,value in [('user.name','RuntimeIT'),('user.email','runtime-it@example.invalid'),('commit.gpgsign','false'),('core.hooksPath',str(repo/'no_hooks'))]:
        subprocess.run(['git','-C',str(repo),'config','--local',key,value],check=True,capture_output=True)
    (repo/'probe.txt').write_bytes(b'T11_GIT_FIXTURE_ONLY');subprocess.run(['git','-C',str(repo),'add','probe.txt'],check=True,capture_output=True)

def command(root,operation,text,**fields):
    return {'tool':'run_command','action_id':'T11-'+uuid.uuid4().hex[:12],'operation':operation,'command':text,
            'working_directory':str(root),'success_criteria':'this isolated command result matches its declared test assertion',**fields}

def predicates(path,marker):
    return [{'action':'file_exists','path':path,'expect':True},{'action':'file_contains','path':path,'text':marker,'expect':True}]

def cases(root):
    mutate=command(root,'MUTATE',"Set-Content -LiteralPath 'probe.txt' -Value 'T11_ACTUAL' -NoNewline",verify=predicates('probe.txt','T11_ACTUAL'))
    return [
        ('INSPECT',command(root,'INSPECT','Get-Location'), 'SUCCEEDED','UNVERIFIED'),
        ('MUTATE',mutate,'SUCCEEDED','PASS'),
        ('VERIFY_NEGATIVE',command(root,'VERIFY',"Get-Content -LiteralPath 'probe.txt' -Raw",verifies_action_id=mutate['action_id'],verify=predicates('probe.txt','WRONG')),'SUCCEEDED','FAIL'),
        ('VERIFY_REPLACEMENT',command(root,'VERIFY',"Get-Content -LiteralPath 'probe.txt' -Raw",verifies_action_id=mutate['action_id'],verify=predicates('probe.txt','T11_ACTUAL')),'SUCCEEDED','PASS'),
        ('BUILD',command(root,'BUILD','cmake --build build',verify=predicates('build/T11_BUILD_OK.txt','T11_BUILD_OK')),'SUCCEEDED','PASS'),
        ('BUILD_NEGATIVE',command(root,'BUILD','cmake --build missing_build',expected_failure={'exit_codes':[1],'output_contains':'not a directory'}),'FAILED','FAIL'),
        ('TEST',command(root,'TEST',"Test-Path -LiteralPath 'probe.txt'",verify=[{'action':'run_command','command':"Test-Path -LiteralPath 'probe.txt'",'expect_exit_code':0,'expect_regex':r'^True\s*$'}]),'SUCCEEDED','PASS'),
        ('TEST_NEGATIVE',command(root,'TEST','exit 7',expected_failure={'exit_codes':[7]}),'FAILED','FAIL'),
        ('GIT_INSPECT',command(root,'GIT_INSPECT',"git -C 'repo' status --short"),'SUCCEEDED','UNVERIFIED'),
        ('GIT_MUTATE',command(root,'GIT_MUTATE',"git -C 'repo' commit -m 'T11_FIXTURE_CHECKPOINT'",verify=[{'action':'run_command','command':"git -C 'repo' rev-parse HEAD",'expect_exit_code':0,'expect_regex':r'^[0-9a-f]{40}\s*$'}]),'SUCCEEDED','PASS'),
        ('PROCESS',command(root,'PROCESS','Start-Sleep -Seconds 3',verify=[{'action':'run_command','command':"Write-Output 'T11_PROCESS_DONE'",'expect_exit_code':0,'expect_contains':'T11_PROCESS_DONE'}]),'SUCCEEDED','PASS'),
        ('TRANSFER',command(root,'TRANSFER',"Copy-Item -LiteralPath 'probe.txt' -Destination 'transferred.txt'",verify=predicates('transferred.txt','T11_ACTUAL')),'SUCCEEDED','PASS'),
        ('GENERAL',command(root,'GENERAL',"Write-Output 'T11_GENERAL_OK'"),'SUCCEEDED','UNVERIFIED'),
    ]

def check(root,name,a,row,raw):
    d=row['data'];assert row['executed'] and d['operation']==a['operation'] and d['process_state']=='EXITED' and Path(d['working_directory']).resolve()==root.resolve(),row
    if name=='INSPECT':assert str(root) in raw and d['terminal_eligible']
    if name.startswith('VERIFY') or name=='MUTATE':assert (root/'probe.txt').read_bytes()==b'T11_ACTUAL'
    if name=='BUILD':assert (root/'build/T11_BUILD_OK.txt').read_bytes()==b'T11_BUILD_OK'
    if name=='TRANSFER':assert (root/'transferred.txt').read_bytes()==(root/'probe.txt').read_bytes()
    if name.endswith('NEGATIVE') and a.get('expected_failure'):
        assert d['expectation_status']=='PASS' and d['terminal_eligible'] and d['exit_code'] in a['expected_failure']['exit_codes']
    if name=='GIT_MUTATE':
        head=subprocess.check_output(['git','-C',str(root/'repo'),'rev-parse','HEAD'],text=True).strip()
        assert len(head)==40 and subprocess.check_output(['git','-C',str(root/'repo'),'log','-1','--format=%s'],text=True).strip()=='T11_FIXTURE_CHECKPOINT'
    if name=='GENERAL':assert not d['terminal_eligible'] and 'T11_GENERAL_OK' in raw

def native(s):
    with tempfile.TemporaryDirectory(prefix='matrix38-t11-') as temp:
        root=Path(temp);setup(root);ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.begin_run('REQ-T11-NATIVE','隔離原生命令測試\n'+str(root),[str(root)]);results=[]
        for name,a,status,verification in cases(root):
            ok,detail=validate_tool_envelope(a);assert ok,detail
            if name=='PROCESS':
                real=subprocess.Popen;started=threading.Event();owners=[];box={}
                def spy(*args,**kw):
                    p=real(*args,**kw)
                    if not owners:owners.append(p);started.set()
                    return p
                def worker():
                    try:box['raw']=ctx.execute(a)
                    except BaseException as exc:box['error']=exc
                with patch('agent_core.bounded_process.subprocess.Popen',side_effect=spy):
                    thread=threading.Thread(target=worker);thread.start();assert started.wait(5)
                    assert owners[0].poll() is None and 'raw' not in box  # real process running; no final success yet
                    running_pid=owners[0].pid;thread.join(timeout=15);assert not thread.is_alive() and 'error' not in box,box
                raw=box['raw'];assert owners[0].poll()==0
            else:raw=ctx.execute(a)
            row=decode_result(raw);assert row and row['status']==status and row['data']['verification_status']==verification,(name,row)
            check(root,name,a,row,raw);results.append({'case':name,'action':a,'result':raw,'running_pid':running_pid if name=='PROCESS' else None})
        # Wrong operation is rejected before a command can alter the fixture.
        wrong=command(root,'INSPECT',"Set-Content -LiteralPath 'must_not_exist.txt' -Value 'WRONG'")
        assert not validate_tool_envelope(wrong)[0] and not (root/'must_not_exist.txt').exists()
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':results,'notes':'All process/file effects real; Popen spy delegates to actual constructor. Native PROCESS pending state observed before final result.'}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture'])/('runtime_'+uuid.uuid4().hex[:8]);root.mkdir();setup(root)
    ctx=WebAgentToolContext(root,interface_name='remote');turns=[];allcases=cases(root);normal=allcases[:-1]
    goal='隔離原生命令與預期負向結果皆已真實驗證'
    negative_goals={'BUILD_NEGATIVE':'不存在的 missing_build 返回 exit 1 且含 not a directory，符合預先宣告負向測試',
                    'TEST_NEGATIVE':'TEST exit 7 符合預先宣告負向測試'}
    for name,a,_,_ in normal:
        if name in negative_goals:a['success_criteria']=negative_goals[name]
    p={**initial(),'base_evaluation':'native command schema/effect/verification distinctions','next_action':'run_command',
       'completion_contract':{'success':[goal,*negative_goals.values()],'failure':['非預期命令或驗證失敗'],'in_progress':['待完成矩陣'],'interrupted':['環境阻塞']},'matched_condition':'待完成矩陣'}
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments;n=len(turns)
        if n>1:
            name,previous,status,v=normal[n-2]
            assert previous['action_id'] in loop.action_ledger,prompt
            raw=loop.action_ledger[previous['action_id']]['result'];row=decode_result(raw)
            assert row['status']==status and row['data']['verification_status']==v;check(root,name,previous,row,raw)
        if n<=len(normal):return response(p,normal[n-1][1])
        refs=[a['action_id'] for _,a,_,_ in normal]
        return response({**p,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,'steps':[{'step':1,'desc':'原生命令矩陣','status':'COMPLETED'}],
                         'matched_condition':goal,'evidence_refs':refs},{'tool':'final_response','action_id':'T11-R-FINAL','content':'T11_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=ctx,progress_root=ROOT)
    assert loop.run('測試隔離命令，TEST exit7 與不存在的 build 預期失敗，FAILED/FAIL不得改寫；其他操作驗收通過。只使用此 root：\n'+str(root))=='T11_RUNTIME_PASS'
    general_goal='GENERAL 只讀命令及明確原檔內容條件已驗證；不能僅以 exit 0 自動成功'
    g=command(root,'GENERAL',"Get-Content -LiteralPath 'probe.txt' -Raw",verify=predicates('probe.txt','T11_ACTUAL'))
    assert 'missing=verify' in WebAgentProtocolLoop._run_command_verification_detail({k:v for k,v in g.items() if k!='verify'})
    gp={**p,'completion_contract':{'success':[general_goal],'failure':['明確條件驗證失敗'],'in_progress':['待取得證據'],'interrupted':['GENERAL 無法取得正式終態']},'matched_condition':'待取得證據'}
    gv=command(root,'VERIFY',"Get-Content -LiteralPath 'probe.txt' -Raw",verifies_action_id=g['action_id'],verify=predicates('probe.txt','T11_ACTUAL'))
    gt=[]
    def general_planner(prompt,expected,attachments):
        gt.append(prompt);assert not attachments
        if len(gt)==1:return response(gp,g)
        if len(gt)==3:
            assert 'terminal_success_verification_missing' in prompt and gv['action_id'] not in gl.action_ledger,prompt
            remedy=gl.pending_verification_requirement['next_actions'][0]
            assert remedy['verifies_action_id']==g['action_id'] and remedy['condition_id'] in prompt
            gv['condition_id']=remedy['condition_id']
            return response({**gp,'next_action':'run_command'},gv)
        raw=gl.action_ledger[g['action_id']]['result'];row=decode_result(raw)
        assert row['status']=='SUCCEEDED' and row['data']['verification_status']=='PASS' and not row['data']['terminal_eligible']
        return response({**gp,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,'steps':[{'step':1,'desc':'GENERAL 明確驗證','status':'COMPLETED'}],
                         'matched_condition':general_goal,'evidence_refs':[gv['action_id']] if len(gt)>3 else [g['action_id']]},{'tool':'final_response','action_id':'T11-G-FINAL','content':'T11_GENERAL_EXPLICIT_CHECK'})
    gl=WebAgentProtocolLoop(root,general_planner,tool_context=WebAgentToolContext(root,interface_name='remote'),progress_root=ROOT)
    try:
        outcome=gl.run('GENERAL 診斷需明確原檔條件，不得只有 exit 0 成功。\n'+str(root));assert outcome=='T11_GENERAL_EXPLICIT_CHECK'
        assert len(gl.action_ledger)==2 and decode_result(gl.action_ledger[gv['action_id']]['result'])['data']['verification_status']=='PASS'
        general_outcome='GENERAL automatic success refused, then genuine linked VERIFY accepted; original terminal_eligible=false preserved'
    except RuntimeError as exc:
        raise AssertionError('GENERAL explicit verification recovery did not converge: '+str(exc)) from exc
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),'actions':loop.action_ledger,
                       'general_actions':gl.action_ledger,'general_rounds':len(gt),'general_outcome':general_outcome,'notes':'No-verify GENERAL preflight rejects; verified GENERAL remains not auto terminal eligible.'}

def live(s,group):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture'])/('web_'+group+'_'+uuid.uuid4().hex[:8]);root.mkdir();setup(root)
    if group!='A':(root/'probe.txt').write_bytes(b'T11_ACTUAL')
    instructions={
      'A':"只使用 run_command 依序四項：1 INSPECT/Get-Location，不提供 verify；2 MUTATE/Set-Content -LiteralPath 'probe.txt' -Value 'T11_ACTUAL' -NoNewline，verify file_exists及file_contains text=T11_ACTUAL；3 新 VERIFY 讀 probe.txt，verifies_action_id 關聯實際第2項 MUTATE，verify file_contains text=WRONG，預期 execution SUCCEEDED/verification FAIL，這是刻意負向；4 新 VERIFY 關聯同一 MUTATE，verify file_contains text=T11_ACTUAL，預期 PASS。兩筆 VERIFY 的 command 都為 Get-Content -LiteralPath 'probe.txt' -Raw。不得重寫、不重做 MUTATE、不把負向 FAIL 改成 PASS；最後回報兩筆歷史及原 MUTATE 最新有效 PASS。",
      'B':"只使用 run_command 依序四項：1 BUILD/cmake --build build，verify build/T11_BUILD_OK.txt 存在且 text=T11_BUILD_OK；2 BUILD/cmake --build missing_build，expected_failure={\"exit_codes\":[1],\"output_contains\":\"not a directory\"}，不需要額外 verify；3 TEST/Test-Path -LiteralPath 'probe.txt'，verify run_command 重查同一路徑且 expect_regex=^True\\s*$；4 TEST/exit 7，expected_failure={\"exit_codes\":[7]}，不需額外 verify。第一份 completion_contract.success 必須預先含兩個精確文字：『不存在的 missing_build 返回 exit 1 且含 not a directory，符合預先宣告負向測試』、『TEST exit 7 符合預先宣告負向測試』；第2、4项 success_criteria 各使用對應相同文字或 Runtime condition_id。負向保留 FAILED/FAIL，expectation PASS 才表示本測試成功。不可修改原 source、不可補造 missing_build。",
      'C':"只使用 run_command 兩項：1 GIT_INSPECT/git -C 'repo' status --short，不提供 verify；2 GIT_MUTATE/git -C 'repo' commit -m 'T11_FIXTURE_CHECKPOINT'，verify run_command git -C 'repo' rev-parse HEAD，expect_exit_code=0、expect_regex=^[0-9a-f]{40}\\s*$。repo 是測試器剛建立並已 stage 一個檔案的合成 repo，只有這個 repo 可 commit，禁止操作任何其他 Git repo、不 push。回報真 HEAD及commit message。",
      'D':"只使用 run_command 依序：1 PROCESS/Start-Sleep -Seconds 3，timeout=15，verify run_command Write-Output 'T11_PROCESS_DONE'、expect_contains=T11_PROCESS_DONE；2 TRANSFER/Copy-Item -LiteralPath 'probe.txt' -Destination 'transferred.txt'，verify transferred.txt 存在且 text=T11_ACTUAL；3 GENERAL/Get-Content -LiteralPath 'probe.txt' -Raw，verify file_contains text=T11_ACTUAL。GENERAL 的 terminal_eligible=false，不得僅以 exit 0 宣稱成功。然後4 新 VERIFY、verifies_action_id 指向實際 GENERAL，command 讀 probe.txt，verify file_contains text=T11_ACTUAL；若 Runtime 提供 condition_id，必須按 next_actions 同時帶上，以客觀條件結案，不重做 GENERAL。回報真 execution/verification/process_state 與 eligibility。"
    }
    prompt='T11 原生命令小組 '+group+'。只在此合成 fixture root 操作，所有 working_directory 固定為此目錄：\n'+str(root)+'\n\n'+instructions[group]+'\n所有需要 verify 的动作都提供明确 success_criteria。不得上傳、下載、調用其他工具或操作使用者專案；不增加多餘 source/plan 操作。由 Runtime 實際結果決定狀態，無法執行如實回報。'
    phase='IT-W-'+group
    if phase in s['phases']:s.setdefault('attempt_history',[]).append(dict(s['phases'][phase]))
    sink={};code=send_and_wait(request=prompt,workspace=r'E:\card',root=ROOT,timeout_sec=900,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']];journal=TaskContinuationStore(ROOT).get(task.task_id) or {};actions=list(journal.get('actions',{}).values());final=task.result_ledger.get('final',{})
    s['phases'][phase]={'status':'UNKNOWN',**sink,'sender_exit_code':code,'fixture':str(root),'actions':actions,'task_state':task.state,'outcome':final.get('task_outcome'),'error':task.error,'response':final.get('response','')}
    validate_live(s,group,journal)

def validate_live(s,group,journal):
    phase='IT-W-'+group
    record=s['phases'][phase];root=Path(record['fixture']);actions=record['actions']
    expected={'A':['INSPECT','MUTATE','VERIFY','VERIFY'],'B':['BUILD','BUILD','TEST','TEST'],'C':['GIT_INSPECT','GIT_MUTATE'],'D':['PROCESS','TRANSFER','GENERAL','VERIFY']}[group]
    # D may legitimately require a fresh condition-bound verifier after the
    # terminal guard supplies a task condition. Never repeat GENERAL or mutation.
    if group=='D' and len(actions)==5:expected.append('VERIFY')
    assert len(actions)==len(expected) and [v['action'].get('operation') for v in actions]==expected,s['phases'][phase]
    for v in actions:
        assert v['action']['tool']=='run_command' and Path(v['action']['working_directory']).resolve()==root.resolve()
        row=decode_result(v['result']);assert row and row['executed'] and row['data']['operation']==v['action']['operation'] and row['data']['process_state']=='EXITED'
    if group=='A':
        assert (root/'probe.txt').read_bytes()==b'T11_ACTUAL'
        original=actions[1]['action']['action_id']
        assert all(v['action']['verifies_action_id']==original for v in actions[2:])
        assert [decode_result(v['result'])['data']['verification_status'] for v in actions[2:]]==['FAIL','PASS']
        entry=journal['results'][original];assert entry.get('effective_verification_status')=='PASS',entry
        history=entry['verification_history']
        assert [h['observed_verification_status'] for h in history[-2:]]==['FAIL','PASS'],history
        assert [h['action_id'] for h in history[-2:]]==[v['action']['action_id'] for v in actions[2:]],history
        record['verified_history']=history
    elif group=='B':
        assert (root/'build/T11_BUILD_OK.txt').read_bytes()==b'T11_BUILD_OK' and not (root/'missing_build').exists()
        for i,exit_code in ((1,1),(3,7)):
            row=decode_result(actions[i]['result']);assert row['status']=='FAILED' and row['data']['verification_status']=='FAIL' and row['data']['exit_code']==exit_code and row['data']['expectation_status']=='PASS'
        assert all(decode_result(actions[i]['result'])['data']['verification_status']=='PASS' for i in (0,2))
    elif group=='C':
        actual=subprocess.check_output(['git','-C',str(root/'repo'),'rev-parse','HEAD'],text=True).strip()
        assert len(actual)==40 and subprocess.check_output(['git','-C',str(root/'repo'),'log','-1','--format=%s'],text=True).strip()=='T11_FIXTURE_CHECKPOINT'
        assert decode_result(actions[1]['result'])['data']['verification_status']=='PASS'
    else:
        assert (root/'transferred.txt').read_bytes()==b'T11_ACTUAL' and decode_result(actions[0]['result'])['data']['verification_status']=='PASS'
        assert decode_result(actions[2]['result'])['data']['terminal_eligible'] is False
        for item in actions[3:]:
            a=item['action'];d=decode_result(item['result'])['data']
            assert a['verifies_action_id']==actions[2]['action']['action_id'] and d['verification_status']=='PASS'
            assert a['command']=="Get-Content -LiteralPath 'probe.txt' -Raw"
            assert any(p.get('action')=='file_contains' and p.get('text')=='T11_ACTUAL' and p.get('found') is True for p in d['postcondition_evidence'])
        if len(actions)==5:
            last=actions[-1]['action'];evidence=journal['results'][last['action_id']]
            assert last.get('condition_id') and evidence['condition_id']==last['condition_id'] and evidence['effective_verification_status']=='PASS'
            assert last['action_id'] in journal['progress']['evidence_refs']
            record['additional_condition_verification']=last['action_id']
    assert record['task_state']=='COMPLETED' and record.get('outcome')=='SUCCESS',record
    s['phases'][phase].update(status='PASS',E='PASS',V='PASS',R='PASS')

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W-A','IT-W-B','IT-W-C','IT-W-D'],required=True);p.add_argument('--recheck',action='store_true');a=p.parse_args()
    assert read_json_retry(STATE.with_name('t10_abort_file_write.json'))['status']=='PASS','T10 gate'
    if STATE.exists():s=read_json_retry(STATE)
    else:
        root=Path(r'E:\card\runtime_it\matrix38')/('T11-'+uuid.uuid4().hex[:12]);root.mkdir();s={'tool':'run_command','status':'IN_PROGRESS','fixture':str(root),'phases':{},'rca':[]}
    try:
        if a.phase.startswith('IT-W-'):
            group=a.phase[-1]
            if group!='A':assert s['phases'].get('IT-W-'+chr(ord(group)-1),{}).get('status')=='PASS','prior live group gate'
            if a.recheck:
                from agent_core.task_continuation import TaskContinuationStore
                journal=TaskContinuationStore(ROOT).get(s['phases'][a.phase]['task_id']) or {}
                s['phases'][a.phase]['actions']=list(journal.get('actions',{}).values())
                validate_live(s,group,journal)
            else:live(s,group)
        else:{'UT':native,'IT-R':runtime}[a.phase](s)
    except Exception as exc:
        s['phases'].setdefault(a.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}'[:3000]);write_json_atomic(STATE,s);raise
    required=['UT','IT-R','IT-W-A','IT-W-B','IT-W-C','IT-W-D']
    s['status']='PASS' if all(s['phases'].get(p,{}).get('status')=='PASS' for p in required) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T11_PHASE_PASS',a.phase,'overall='+s['status'])

if __name__=='__main__':main()
