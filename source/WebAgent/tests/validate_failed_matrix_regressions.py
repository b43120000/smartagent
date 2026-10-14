"""Fixed-action Runtime regressions for the failed 1/3/5 matrix cases."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_core.tools import tool_run_command
from agent_core.security_context import SecurityContext
from agent_core.tool_result_contract import decode_result
from agent_core.action_loop_state import classify_evidence_state
from agent_core.plan_ledger import is_planning_request
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.verification_expectation import validate_expectation, evaluate_expectation
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
from WebAgent.protocol_loop import WebAgentProtocolLoop, extract_authorized_paths
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_action_scoped_admission import finished
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def run():
    assert extract_authorized_paths('只在 E:\\workspace\\runtime_it 建立 disposable_delete_probe 測試資料夾。') == [r'E:\workspace\runtime_it']
    assert not is_planning_request('請測試 Runtime 的只讀命令。不要修改檔案。')
    assert is_planning_request('先規劃不改，分析怎麼修改。')
    with tempfile.TemporaryDirectory(prefix='matrix-fixed-') as temp:
        root=Path(temp); calls=[]; prompts=[]
        def capture(command, timeout, capture_root=None, **kwargs):
            calls.append(command)
            return {'exit_code':0,'stdout':str(root),'stderr':'','timed_out':False}
        action={'tool':'run_command','action_id':'INSPECT-ONLY','operation':'INSPECT',
                'command':'Get-Location','working_directory':str(root)}
        def planner(prompt, expected, attachments):
            prompts.append(prompt)
            return (response({**initial(),'next_action':'run_command'},action) if len(prompts)==1 else
                response(finished(['INSPECT-ONLY']),{'tool':'final_response','action_id':'FINAL','content':'OBSERVATION_ONLY'}))
        loop=WebAgentProtocolLoop(root,planner,progress_root=root)
        with patch('agent_core.tools._run_powershell_capture',side_effect=capture):
            assert loop.run('只讀取\n'+str(root))=='OBSERVATION_ONLY'
        assert calls==['Get-Location'] and len(prompts)==2
        assert loop.action_result_ledger['INSPECT-ONLY']['verification_status']=='UNVERIFIED'
        assert classify_evidence_state({'tool':'run_command','operation':'GENERAL'},'output','SUCCEEDED','UNVERIFIED')[0]=='UNVERIFIED'
        for command in ("Set-Content -LiteralPath 'probe.txt' -Value WRONG", "git add .", "powershell -NoProfile -Command \"Remove-Item 'x'\""):
            valid,_=validate_tool_envelope({**action,'command':command})
            assert not valid,command
        (root/'negative_probe.txt').write_text('ACTUAL_VALUE')
        condition='主命令成功且故意的 WRONG_VALUE 後置檢查 FAIL，檔案仍為 ACTUAL_VALUE'
        negative={'tool':'run_command','action_id':'NEGATIVE','operation':'MUTATE',
            'command':"Write-Output 'ACTUAL_VALUE'",'working_directory':str(root),
            'success_criteria':condition,'expected_verification':{'status':'FAIL','failed_checks':[1]},
            'verify':[{'action':'file_contains','path':'negative_probe.txt','text':'WRONG_VALUE'}]}
        valid, issue=validate_tool_envelope(negative)
        assert valid,issue
        progress={**initial(),'next_action':'run_command','completion_contract':{
            **initial()['completion_contract'],'success':[condition]}}
        steps=[]; calls.clear()
        def negative_planner(prompt, expected, attachments):
            steps.append(prompt)
            if len(steps)==1:return response(progress,negative)
            complete={**finished(['NEGATIVE']),'completion_contract':progress['completion_contract'],
                      'matched_condition':condition}
            return response(complete,{'tool':'final_response','action_id':'NEG-FINAL','content':'EXPECTED_NEGATIVE_PASS'})
        loop=WebAgentProtocolLoop(root,negative_planner,progress_root=root)
        with patch('agent_core.tools._run_powershell_capture',side_effect=capture):
            assert loop.run('負向測試\n'+str(root))=='EXPECTED_NEGATIVE_PASS'
        result=loop.action_result_ledger['NEGATIVE']
        assert result['execution_status']=='SUCCEEDED' and result['verification_status']=='FAIL'
        assert result['expectation_status']=='PASS' and loop.terminal_outcome=='SUCCESS'
        assert (root/'negative_probe.txt').read_text()=='ACTUAL_VALUE' and len(calls)==1
        assert evaluate_expectation({'status':'FAIL','failed_checks':[1]},'PASS',[{'action':'file_contains','found':True,'passed':True}])[0]=='FAIL'
        assert evaluate_expectation({'status':'FAIL','failed_checks':[1]},'SPEC_INVALID',[{'passed':False,'spec_valid':False}])[0]=='SPEC_INVALID'
        for error in ({'action':'file_contains','passed':False,'error':'PermissionError'},
            {'action':'file_exists','passed':False,'error':'SECURITY_PATH_REJECTED'},
            {'action':'run_command','passed':False,'result':{'exit_code':None,'security_rejected':True}},
            {'action':'run_command','passed':False,'result':{'exit_code':1,'timed_out':True}},
            {'action':'run_command','passed':False,'result':{'exit_code':1,'stderr':'command not found','timed_out':False}}):
            assert evaluate_expectation({'status':'FAIL','failed_checks':[1]},'FAIL',[error])[0]=='SPEC_INVALID'
        for bad in ({'status':'PASS','failed_checks':[1]},{'status':'FAIL','failed_checks':[True]},
                    {'status':'FAIL','failed_checks':[2]},{'status':'FAIL','failed_checks':[]}):
            valid,_=validate_tool_envelope({**negative,'expected_verification':bad})
            assert not valid
            with patch('agent_core.tools._run_powershell_capture',side_effect=AssertionError('must not execute')):
                rejected=decode_result(tool_run_command(negative['command'],'MUTATE',
                    verify=negative['verify'],expected_verification=bad,security_context=SecurityContext.for_workspace(root)))
                assert rejected['status']=='REJECTED' and rejected['executed'] is False
        # Main command failure cannot be turned into expected verification PASS.
        with patch('agent_core.tools._run_powershell_capture',return_value={'exit_code':1,'stdout':'','stderr':'actual failure','timed_out':False}):
            failed=decode_result(tool_run_command(negative['command'],'MUTATE',verify=negative['verify'],
                expected_verification=negative['expected_verification'],security_context=SecurityContext.for_workspace(root)))
            assert failed['status']=='FAILED' and failed['data'].get('expectation_status')!='PASS'
        # Even valid matching failure cannot be introduced without a success
        # assertion declared in the same pre-execution progress contract.
        unbound=WebAgentProtocolLoop(root,lambda *a:'',progress_root=root)
        unbound.run_id='UNBOUND';unbound.task_id='UNBOUND';unbound.task_epoch='UNBOUND';unbound.task_request='negative test'
        unbound.intent_digest='digest';unbound.authorized_paths=[str(root)]
        unbound.pending_command_field_repairs={}
        from agent_core.task_progress import initialize_progress
        unbound.progress_ledger=initialize_progress('UNBOUND',request_id='UNBOUND',goal='negative test',root=root)
        _,diagnostics=unbound._accept_ack([{'tool':'report_progress',**initial(),'next_action':'run_command'},negative,
                {'tool':'turn_commit','action_count':2}],unbound._new_commit())
        assert any(d['reason']=='expected_failure_not_bound_to_success_contract' for d in diagnostics),diagnostics
        for outcome,heading in [('FAILED','驗收失敗'),('PARTIAL','部分完成'),('SUCCESS','已完成')]:
            display=TelegramDeliveryAdapter.render({'event_type':'TASK_COMPLETED','payload':{'task_outcome':outcome,'summary':'done'}})
            assert heading in display
    print('FAILED_MATRIX_FIXED_ACTION_PASS')


if __name__=='__main__':
    run()
