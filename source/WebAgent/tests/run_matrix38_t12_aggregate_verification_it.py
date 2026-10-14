"""T12 real process/checker matrix; no mocked command execution."""
import argparse,json,tempfile,uuid
from pathlib import Path
from agent_core.json_state_io import read_json_retry,write_json_atomic
from agent_core.tool_result_contract import decode_result
from agent_core.aggregated_verification import run_aggregated_verification
from agent_core.command_security import inspect_command
from agent_core.bounded_process import run_bounded_process
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.tests.run_matrix38_t07_begin_file_write_it import ROOT
STATE=ROOT/'localdata/runtime_it/matrix38/t12_aggregate_verification.json'
CWD='powershell.exe -NoProfile -NonInteractive -Command "(Get-Location).Path"'
GOOD='findstr /x /c:T12_ACTUAL probe.txt'
BAD='findstr /x /c:WRONG probe.txt'
SLOW='powershell.exe -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 3"'
REJECT="Remove-Item -LiteralPath 'must_not_touch.txt' -Recurse -Force"

def action(root,commands,**extra):
    return {'tool':'aggregate_verification','action_id':'T12-'+uuid.uuid4().hex[:12],
            'workspace':str(root),'commands':commands,'timeout':10,**extra}

def setup(root):
    (root/'probe.txt').write_bytes(b'T12_ACTUAL\r\n')
    (root/'must_not_touch.txt').write_bytes(b'T12_PRESERVE')

def native(s):
    records=[]
    with tempfile.TemporaryDirectory(prefix='matrix38-t12-') as tmp:
        root=Path(tmp);setup(root);ctx=WebAgentToolContext(root,interface_name='remote')
        for name,commands,timeout,status,executed in [
            ('positive',[CWD,GOOD],10,'SUCCEEDED',True),
            ('mixed_fail',[CWD,BAD,GOOD],10,'FAILED',True),
            ('invalid_command',['definitely_missing_t12_executable_620419'],10,'FAILED',True),
            ('timeout',[SLOW],1,'FAILED',True),
            ('security_rejected',[REJECT],10,'REJECTED',False),
            ('mixed_rejected',[GOOD,REJECT],10,'PARTIAL',True),
        ]:
            a=action(root,commands,timeout=timeout);raw=ctx.execute(a);business=json.loads(raw);row=decode_result(raw)
            records.append({'case':name,'action':a,'business':business,'runtime':row})
            assert row['status']==status and row['executed'] is executed,(name,row,business)
            assert business['command_count']==len(commands) and len(business['results'])==len(commands)
            assert [v['command'] for v in business['results']]==commands
            if name=='positive':
                assert business['status']=='PASS' and str(root) in business['results'][0]['stdout']
                assert business['results'][1]['stdout']=='T12_ACTUAL'
            elif name=='mixed_fail':
                assert business['status']=='FAIL' and [v['exit_code'] for v in business['results']]==[0,1,0]
                evidence=WebAgentProtocolLoop._classify_action_evidence(a,raw)
                assert evidence['execution_status']=='SUCCEEDED' and evidence['verification_status']=='FAIL' and not evidence['terminal_eligible']
            elif name=='timeout':assert business['results'][0]['timed_out'] is True
            elif name=='security_rejected':
                assert not inspect_command(REJECT).allowed and business['results'][0]['security_rejected'] is True
            elif name=='mixed_rejected':
                assert business['status']=='PARTIAL' and business['results'][0]['exit_code']==0 and business['results'][1]['security_rejected'] is True
            else:assert business['results'][0]['exit_code']!=0
            assert (root/'probe.txt').read_bytes()==b'T12_ACTUAL\r\n' and (root/'must_not_touch.txt').read_bytes()==b'T12_PRESERVE'
        invalid=run_aggregated_verification(root,[])
        assert invalid['status']=='SPEC_INVALID' and not invalid['results']
        # A native Popen startup failure is not the same as a launched shell
        # failing to find an executable. No fake subprocess exception injected.
        startup=run_bounded_process(['definitely_missing_t12_native_exe_620419'],cwd=root,shell=False,capture_root=root/'captures')
        assert startup['process_started'] is False and startup['exit_code'] is None
        from agent_core.tool_result_policy import structured_outcome
        payload={'schema':'AGGREGATED_VERIFICATION_V1','status':'FAIL','results':[startup]}
        status,reason,executed,data=structured_outcome('aggregate_verification',payload)
        assert status=='FAILED' and executed is False and data['verification_status']=='UNKNOWN'
        records.append({'case':'native_spawn_failed','business':payload,'status':status,'executed':executed})
        # Pure result-policy fault injection, not claimed as a live restricted
        # executor test: dispatched work can be unknown, never a proven refusal.
        uncertain={'status':'UNKNOWN','results':[{'security_rejected':True,'process_started':None,'exit_code':None}]}
        status,reason,executed,data=structured_outcome('aggregate_verification',uncertain)
        assert status=='UNKNOWN' and data['possible_partial_execution'] and data['verification_status']=='UNKNOWN'
        assert not ctx.take_pending_attachments()
    s['phases']['UT']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','cases':records}

def runtime(s):
    assert s['phases'].get('UT',{}).get('status')=='PASS'
    root=Path(s['fixture'])/('runtime_'+uuid.uuid4().hex[:8]);root.mkdir();setup(root)
    mutate={'tool':'run_command','action_id':'T12-MUTATE','operation':'MUTATE','working_directory':str(root),
            'command':"Set-Content -LiteralPath 'probe.txt' -Value 'T12_ACTUAL'",
            'success_criteria':'probe.txt contains T12_ACTUAL','verify':[{'action':'file_contains','path':'probe.txt','text':'T12_ACTUAL'}]}
    bad=action(root,[CWD,BAD,GOOD],verifies_action_id=mutate['action_id'])
    good=action(root,[CWD,GOOD],verifies_action_id=mutate['action_id'])
    goal='同一原 MUTATE 的批次負向檢查保留 FAIL，新的真實批次檢查 PASS，檔案保持 T12_ACTUAL'
    progress={**initial(),'base_evaluation':'native aggregate checks and lifecycle replacement',
              'completion_contract':{'success':[goal],'failure':['最新有效驗證失敗'],'in_progress':['待完成真實檢查'],'interrupted':['環境阻塞']},
              'matched_condition':'待完成真實檢查'}
    turns=[]
    def planner(prompt,expected,attachments):
        turns.append(prompt);assert not attachments
        n=len(turns);assert n<=4,'unexpected recovery: '+prompt[-4000:]
        if n<=3:
            a=[mutate,bad,good][n-1]
            return response({**progress,'next_action':a['tool']},a)
        assert bad['action_id'] in loop.action_ledger and good['action_id'] in loop.action_ledger,'missing execution: '+prompt[:5000]
        for a,status in ((bad,'FAIL'),(good,'PASS')):
            raw=loop.action_ledger[a['action_id']]['result'];business=json.loads(raw)
            assert business['status']==status and str(root) in business['results'][0]['stdout'],business
        assert loop.action_result_ledger[mutate['action_id']]['effective_verification_status']=='PASS'
        return response({**progress,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
                         'steps':[{'step':1,'desc':'真實批次驗證','status':'COMPLETED'}],
                         'matched_condition':goal,'evidence_refs':[mutate['action_id'],good['action_id']]},
                        {'tool':'final_response','action_id':'T12-FINAL','content':'T12_RUNTIME_PASS'})
    loop=WebAgentProtocolLoop(root,planner,tool_context=WebAgentToolContext(root,interface_name='remote'),progress_root=ROOT)
    assert loop.run('只在此 root 測試 aggregate 驗證負向 FAIL 後，以新驗證取代，不重寫原 MUTATE。\n'+str(root))=='T12_RUNTIME_PASS'
    assert (root/'probe.txt').read_bytes()==b'T12_ACTUAL\r\n'
    history=loop.action_result_ledger[mutate['action_id']]['verification_history']
    assert [h['observed_verification_status'] for h in history[-2:]]==['FAIL','PASS']
    s['phases']['IT-R']={'status':'PASS','E':'PASS','V':'PASS','R':'PASS','rounds':len(turns),
                       'actions':loop.action_ledger,'history':history,'fixture':str(root)}

def live(s):
    assert s['phases'].get('IT-R',{}).get('status')=='PASS'
    from RemoteAgent.local_telegram_sender import send_and_wait
    from agent_core.paths import remote_tasks_path
    from agent_core.task_state import TaskStateStore
    from agent_core.task_continuation import TaskContinuationStore
    root=Path(s['fixture'])/('web_'+uuid.uuid4().hex[:8]);root.mkdir();setup(root)
    prompt=('T12 aggregate_verification 真實批次驗證測試，只操作以下全新 fixture root：\n'+str(root)+
      '\n第一步僅一次 run_command operation=MUTATE，working_directory 固定此 root，command 精確為 '
      "Set-Content -LiteralPath 'probe.txt' -Value 'T12_ACTUAL'（不要 -NoNewline），verify file_contains path=probe.txt text=T12_ACTUAL。"
      '\n第二步 aggregate_verification，workspace 固定此 root，verifies_action_id 指向第一步真實 MUTATE，commands 精確依序為：\n'+json.dumps([CWD,BAD,GOOD])+
      '\n預期各 exit 0/1/0、批次結果 FAIL；這是刻意負向，不得重寫檔案，也不可把 FAIL 改成 PASS。'
      '\n第三步新 aggregate_verification，workspace 同一 root、verifies_action_id 同一原 MUTATE，commands 精確依序為：\n'+json.dumps([CWD,GOOD])+
      '\n預期兩項真實檢查 exit 0、批次 PASS，檔案仍為 T12_ACTUAL 加 Windows 換行；保留先前 FAIL 歷史並回報原 MUTATE 最新有效 PASS。'
      '\n第一份 Progress completion_contract 要涵蓋這個負向→正向測試；每輪 next_action 與真實工具一致。最後依 Runtime 證據結案。'
      '\n只允許上述三個 action，不操作其他路徑、不上傳、不 project_sync、不新增 plan、不重做 MUTATE。檢查用明確 powershell.exe，不能將裸 Get-Location 當 cmd 命令。')
    sink={};code=send_and_wait(request=prompt,workspace=r'E:\workspace',root=ROOT,timeout_sec=900,result_sink=sink)
    task=TaskStateStore(remote_tasks_path(ROOT)).load()[sink['task_id']]
    journal=TaskContinuationStore(ROOT).get(task.task_id) or {};actions=list(journal.get('actions',{}).values());final=task.result_ledger.get('final',{})
    record={'status':'UNKNOWN',**sink,'sender_exit_code':code,'fixture':str(root),'actions':actions,
            'task_state':task.state,'outcome':final.get('task_outcome'),'error':task.error,'response':final.get('response','')}
    s['phases']['IT-W']=record
    assert len(actions)==3 and [v['action']['tool'] for v in actions]==['run_command','aggregate_verification','aggregate_verification'],record
    original=actions[0]['action']['action_id']
    assert actions[0]['action']['operation']=='MUTATE' and Path(actions[0]['action']['working_directory']).resolve()==root.resolve()
    for item,commands,status,exits in ((actions[1],[CWD,BAD,GOOD],'FAIL',[0,1,0]),(actions[2],[CWD,GOOD],'PASS',[0,0])):
        a=item['action'];business=json.loads(item['result']);row=decode_result(item['result'])
        assert a['commands']==commands and a['verifies_action_id']==original and Path(a['workspace']).resolve()==root.resolve()
        assert business['status']==status and [v['exit_code'] for v in business['results']]==exits
        assert str(root) in business['results'][0]['stdout'] and all(v['process_started'] is True for v in business['results'])
        assert row['executed'] is True and row['data']['checks_completed'] is True
        assert row['data']['verification_status']==status and item['execution_status']=='SUCCEEDED'
    evidence=journal['results'][original];history=evidence['verification_history']
    assert [h['observed_verification_status'] for h in history[-2:]]==['FAIL','PASS'] and evidence['effective_verification_status']=='PASS'
    assert (root/'probe.txt').read_bytes()==b'T12_ACTUAL\r\n' and (root/'must_not_touch.txt').read_bytes()==b'T12_PRESERVE'
    assert task.state=='COMPLETED' and final.get('task_outcome')=='SUCCESS' and not final.get('uploaded_attachments'),record
    record.update(status='PASS',E='PASS',V='PASS',R='PASS',verified_history=history)

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['UT','IT-R','IT-W'],required=True);args=p.parse_args()
    assert read_json_retry(STATE.with_name('t11_run_command.json'))['status']=='PASS','T11 gate'
    s=read_json_retry(STATE) if STATE.exists() else {'tool':'aggregate_verification','status':'IN_PROGRESS','phases':{},'rca':[]}
    if not s.get('fixture'):
        root=Path(r'E:\workspace\runtime_it\matrix38')/('T12-'+uuid.uuid4().hex[:12]);root.mkdir();s['fixture']=str(root)
    s['rca']=[
      {'layer':'RUNTIME_RESULT','reason':'all security-rejected commands reported executed=true','fix':'per-row rejection and real process_started authority; aggregate all refused REJECTED/false; mixed PARTIAL'},
      {'layer':'PROCESS_TIMEOUT','reason':'killing Windows shell alone left child holding capture files','fix':'terminate only owned live Popen PID tree on timeout before parent fallback'},
      {'layer':'RUNTIME_STATE','reason':'completed scoped negative checker forced REPLAN and rejected new aggregate verifier','fix':'checks_completed distinguishes execution from verification; scoped SUCCEEDED/FAIL allows fresh VERIFY while original FAIL stays'},
      {'layer':'TEST_INPUT','reason':'inherited next_action=list_directory and findstr on unterminated line','fix':'match declared tool and explicit Windows newline fixture; no Runtime bypass'},
    ]
    try:{'UT':native,'IT-R':runtime,'IT-W':live}[args.phase](s)
    except Exception as exc:
        s['phases'].setdefault(args.phase,{}).update(status='FAIL',error=f'{type(exc).__name__}: {exc}'[:5000])
        write_json_atomic(STATE,s);raise
    s['status']='PASS' if all(s['phases'].get(p,{}).get('status')=='PASS' for p in ('UT','IT-R','IT-W')) else 'IN_PROGRESS'
    write_json_atomic(STATE,s);print('T12_PHASE_PASS',args.phase,'overall='+s['status'])

if __name__=='__main__':main()
