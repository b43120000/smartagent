"""P0 operation contracts, shared cwd, request-scoped field repair."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.security_context import SecurityContext
from agent_core.path_security import PathSecurityError
from agent_core.tools import tool_run_command, resolve_command_working_directory, execute_tool
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_action_scoped_admission import finished
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def run():
    inspect={'tool':'run_command','operation':'INSPECT','command':"Write-Output 'READ'"}
    assert not WebAgentProtocolLoop._run_command_verification_detail(inspect)
    assert 'missing=verify' in WebAgentProtocolLoop._run_command_verification_detail({**inspect,'operation':'BUILD'})
    assert 'missing=verify' in WebAgentProtocolLoop._run_command_verification_detail({**inspect,'operation':'GENERAL'})
    negative={**inspect,'operation':'TEST','command':'exit 7','success_criteria':'expected exit 7','expected_failure':{'exit_codes':[7]}}
    assert not WebAgentProtocolLoop._run_command_verification_detail(negative)
    for invalid in ({**negative,'operation':'MUTATE'},{**negative,'expected_failure':{'exit_codes':[0]}},
                    {**negative,'expected_failure':{'exit_codes':[]}}, {**negative,'expected_failure':{'unknown':True}}):
        assert 'missing=verify' in WebAgentProtocolLoop._run_command_verification_detail(invalid)
    assert 'invalid=verify' in WebAgentProtocolLoop._run_command_verification_detail({**negative,'verify':'skip'})
    with tempfile.TemporaryDirectory(prefix='p0-cwd-') as temp:
        root=Path(temp); project=root/'project'; project.mkdir()
        (project/'proof.txt').write_text('PROOF')
        (project/'gradlew.bat').write_text('@exit /b 0')
        context=SecurityContext.for_workspace(root)
        calls=[]
        def capture(command,timeout,capture_root=None,**kwargs):
            calls.append((command,kwargs.get('cwd')))
            return {'exit_code':0,'stdout':'OBSERVED','stderr':'','timed_out':False}
        with patch('agent_core.tools._run_powershell_capture',side_effect=capture):
            result=tool_run_command('Get-Location','INSPECT',working_directory=str(project),security_context=context,
                verify=[{'action':'run_command','command':'Get-Location','expect_contains':'OBSERVED'},
                    {'action':'file_exists','path':'proof.txt'}])
            assert decode_result(result)['data']['verification_status']=='PASS'
            assert all(cwd==project for _command,cwd in calls) and len(calls)==2
            assert 'working_directory: '+str(project) in result
            calls.clear()
            result=tool_run_command('Get-Location','INSPECT',security_context=context)
            assert calls[0][1]==root and 'working_directory_source: workspace_default' in result
            assert decode_result(result)['data']['verification_status']=='UNVERIFIED'
            general=tool_run_command('Get-Location','GENERAL',security_context=context)
            assert not decode_result(general)['data']['terminal_eligible']
        for value in (str(root.parent),'relative',str(project/'proof.txt'),str(project)+':stream'):
            try: resolve_command_working_directory(value,context)
            except PathSecurityError: pass
            else: raise AssertionError('unsafe cwd accepted '+value)
        scoped=SimpleNamespace(_authorized_local_paths=[str(project)],current_request_text='')
        try: resolve_command_working_directory(str(root),context,scoped)
        except PathSecurityError: pass
        else: raise AssertionError('request scope widened')
        try: resolve_command_working_directory(None,context,scoped)
        except PathSecurityError: pass
        else: raise AssertionError('omitted cwd widened request scope')
        file_scope=SimpleNamespace(_authorized_local_paths=[str(project/'proof.txt')])
        assert resolve_command_working_directory(str(project),context,file_scope)==project
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=context),patch('agent_core.tools._run_powershell_capture',side_effect=AssertionError('must not start')):
            rejected=decode_result(execute_tool({**inspect,'working_directory':str(root.parent)}))
            assert rejected['status']=='REJECTED' and not rejected['executed']

        prompts=[]; executed=[]
        command={'tool':'run_command','action_id':'BUILD-ONCE','operation':'BUILD',
            'command':'.\\gradlew.bat assembleDebug','working_directory':str(project),
            'success_criteria':'proof exists'}
        def planner(prompt,expected,attachments):
            prompts.append(prompt)
            if len(prompts)==1:
                return response({**initial(),'next_action':'run_command'},command)
            if len(prompts)==2:
                assert 'COMMAND_FIELD_REPAIR' in prompt and not executed
                return response({**initial(),'next_action':'run_command'},
                    {'tool':'run_command','action_id':'BUILD-ONCE',
                     'verify':[{'action':'file_exists','path':'proof.txt'}]})
            assert not loop.pending_command_field_repairs
            return response(finished(['BUILD-ONCE']),{'tool':'final_response','action_id':'FINAL','content':'FIELD_REPAIR_OK'})
        loop=WebAgentProtocolLoop(root,planner,progress_root=root)
        native=loop.tools.execute
        def execute(action): executed.append(action); return native(action)
        loop.tools.execute=execute
        with patch('agent_core.tools._run_powershell_capture',side_effect=capture):
            assert loop.run('build\n'+str(project),trusted_authorized_paths=[str(project)])=='FIELD_REPAIR_OK'
        assert len(executed)==1 and executed[0]['command']==command['command'] and executed[0]['working_directory']==str(project)
        assert len(prompts)==3
        native_result=tool_run_command('Get-Location','INSPECT',working_directory=str(project),security_context=context,
            verify=[{'action':'run_command','command':"Test-Path -LiteralPath '.\\proof.txt'",'expect_contains':'True'},
                    {'action':'file_exists','path':'proof.txt'}])
        native_data=decode_result(native_result)
        assert native_data['status']=='SUCCEEDED' and native_data['data']['verification_status']=='PASS',native_result
        assert str(project) in native_result
        # Explicit identity/request guards before normal admission.
        saved={'request_id':loop.run_id,'task_epoch':loop.task_epoch,'action':command,'required_fields':['verify']}
        loop.pending_command_field_repairs={'B':{**saved,'action':{**command,'action_id':'B'}}}
        _,issues=loop._accept_ack([{'tool':'run_command','action_id':'B','command':'different'}],{})
        assert issues[0]['reason']=='command_repair_identity_changed'
        loop.pending_command_field_repairs['B']['request_id']='OTHER'
        _,issues=loop._accept_ack([{'tool':'run_command','action_id':'B','verify':[]}],{})
        assert issues[0]['reason']=='command_repair_target_not_pending'
        loop.pending_command_field_repairs={'BUILD-ONCE':saved}
        _,issues=loop._accept_ack([{'tool':'run_command','action_id':'BUILD-ONCE','verify':[]}],{})
        assert issues[0]['reason']=='command_repair_target_not_pending'
    print('P0_COMMAND_FIELD_REPAIR_PASS')

if __name__=='__main__':run()
