"""P0 deterministic producer tests; no live web/Telegram and no app data writes."""
import tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.security_context import SecurityContext
from agent_core.tools import execute_tool, tool_run_command
from agent_core.tool_result_contract import decode_result, attach_runtime_result, outcome_committable
from WebAgent.protocol_loop import WebAgentProtocolLoop


def run():
    with tempfile.TemporaryDirectory(prefix='p0-result-') as temp:
        root = Path(temp)
        context = SecurityContext.for_workspace(root)
        with patch('agent_core.tools.SecurityContext.from_agent', return_value=context):
            target = root/'read.txt'; target.write_text('CONTENT', encoding='utf-8')
            result = execute_tool({'tool':'read_file','path':str(target)})
            row = decode_result(result)
            assert row['status']=='SUCCEEDED' and row['data']['text']=='CONTENT'
            assert row['data']['content_read'] and row['executed']
            result = execute_tool({'tool':'read_file','path':str(root/'missing')})
            row = decode_result(result)
            assert row['status']=='FAILED' and row['reason']=='FILE_NOT_FOUND' and not row['executed']
            with patch('agent_core.tools._read_bounded_bytes', side_effect=PermissionError('denied')):
                row = decode_result(execute_tool({'tool':'read_file','path':str(target)}))
                assert row['status']=='REJECTED' and row['reason']=='ACCESS_DENIED'
            with patch.object(SecurityContext, 'require_read', return_value=target), patch.object(Path, 'stat', side_effect=PermissionError('preflight denied')):
                row = decode_result(execute_tool({'tool':'read_file','path':str(target)}))
                assert row['status']=='REJECTED' and row['reason']=='ACCESS_DENIED' and not row['executed']
        base = {'exit_code':0, 'stdout':'OK', 'stderr':'', 'timed_out':False}
        assert row['executed'] is False
        for exit_code, verify, execution, verification in (
            (0, [], 'SUCCEEDED','UNVERIFIED'),
            (1, [], 'FAILED','FAIL'),
            (0, [{'action':'file_exists','path':str(target),'expect':True}], 'SUCCEEDED','PASS'),
        ):
            with patch('agent_core.tools._run_powershell_capture', return_value={**base,'exit_code':exit_code}):
                result = tool_run_command('echo OK','INSPECT',verify=verify)
            row = decode_result(result)
            assert row['status']==execution and row['data']['verification_status']==verification
            assert result.count('[RUNTIME_TOOL_RESULT]')==1
            evidence = WebAgentProtocolLoop._classify_action_evidence({'tool':'run_command','operation':'INSPECT'},result)
            assert evidence['execution_status']==execution and evidence['verification_status']==verification
        with patch('agent_core.tools._run_powershell_capture', return_value={**base,'exit_code':1}):
            result = tool_run_command('exit 1','BUILD',expected_failure={'exit_codes':[1]})
        evidence = WebAgentProtocolLoop._classify_action_evidence({'tool':'run_command','operation':'BUILD'},result)
        assert evidence['execution_status']=='FAILED' and evidence['verification_status']=='FAIL'
        assert evidence['expectation_status']=='PASS' and evidence['terminal_eligible']
        for invalid in ('REJECTED','QUEUED','RUNNING','PARTIAL','UNKNOWN'):
            row={'status':invalid,'executed':invalid!='REJECTED','data':{'process_state':'EXITED','exit_code':1,'verification_status':'FAIL','expectation_status':'PASS'}}
            assert not outcome_committable('run_command',row)
        assert not outcome_committable('read_file',{'status':'FAILED','executed':True,'data':{'expectation_status':'PASS'}})
        assert not outcome_committable('read_file',{'tool':'write_file','status':'SUCCEEDED','executed':True,'data':{}})
        assert not outcome_committable('run_command',{'tool':'run_command','action_id':'OLD','status':'FAILED','executed':True,
            'data':{'process_state':'EXITED','exit_code':1,'verification_status':'FAIL','expectation_status':'PASS'}}, 'NEW')
        for main, expected_status in (
            ({**base,'security_rejected':True,'exit_code':None}, 'REJECTED'),
            ({**base,'exit_code':None,'error':'spawn failed'}, 'FAILED'),
            ({**base,'exit_code':None,'process_state':'RUNNING','pid':123}, 'RUNNING'),
        ):
            with patch('agent_core.tools._run_powershell_capture', return_value=main):
                row = decode_result(tool_run_command('echo OK','PROCESS'))
            assert row['status']==expected_status
            assert row['executed'] == (expected_status=='RUNNING')
        for pending in ('QUEUED','RUNNING','PARTIAL'):
            result = attach_runtime_result('pending', pending,'WAITING', tool='read_file',executed=True)
            evidence = WebAgentProtocolLoop._classify_action_evidence({'tool':'read_file'},result)
            assert not evidence['terminal_eligible'] and evidence['result_status']==pending
    print('P0_READ_COMMAND_CONTRACT_PASS')


if __name__=='__main__':
    run()
