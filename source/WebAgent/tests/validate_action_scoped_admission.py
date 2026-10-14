"""Fixed planner/runtime integration; no browser or Telegram mocking claim."""
import json
import tempfile
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from agent_core.action_dependencies import dependency_diagnostic, unsatisfied_dependencies, is_observation
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.protocol_v9 import parse_v9_tool_transport


def finished(refs, outcome='SUCCESS'):
    return {**initial(),'decision':'COMPLETE','outcome':outcome,'current_step':1,
        'steps':[{'step':1,'desc':'讀取目錄','status':'COMPLETED'}],
        'matched_condition':'目錄資料已讀取','evidence_refs':refs}


def run():
    with tempfile.TemporaryDirectory(prefix='action-scoped-') as temp:
        root=Path(temp); (root/'proof.txt').write_text('PROOF')
        events=[]; turns=[]; executed=[]
        def planner(prompt, expected, attachments):
            turns.append(prompt)
            if len(turns)==1:
                return response(initial(),
                    {'tool':'read_file','action_id':'BAD','path':17},
                    {'tool':'list_directory','action_id':'GOOD','path':str(root)})
            if len(turns)==2:
                assert 'REPAIR_REQUIRED' in prompt and 'GOOD' in prompt and 'proof.txt' in prompt
                assert 'BAD' in loop.deferred_action_repairs and 'BAD' not in loop.action_result_ledger
                state=loop._refresh_action_loop_state()
                assert any(x.get('admission_status')=='REPAIR_REQUIRED' and x['action_id']=='BAD' for x in state['action_states'])
                # Fix the never-executed action only. The successful sibling is
                # absent and must not be restored by legacy batch repair.
                return response({**initial(),'next_action':'query_project','evidence_refs':['GOOD']},
                    {'tool':'query_project','action_id':'BAD','project_root':str(root),
                     'queries':[{'operation':'read_range','path':'proof.txt','start_line':1,'end_line':10}]})
            assert len(turns)==3 and not loop.deferred_action_repairs, prompt
            return response(finished(['GOOD','BAD']),{'tool':'final_response','action_id':'FINAL','content':'PARTITION_OK'})
        loop=WebAgentProtocolLoop(root,planner,progress_root=root)
        original=loop._execute_action
        def execute(action):
            executed.append(action['action_id']); return original(action)
        loop._execute_action=execute
        assert loop.run('只讀取測試目錄\n'+str(root),trusted_authorized_paths=[str(root)])=='PARTITION_OK'
        assert executed==['GOOD','BAD'] and len(turns)==3

        # Expected missing file does not cancel an independent directory read;
        # an explicit dependent is held, not executed or recorded as evidence.
        turns=[]; executed=[]
        def dependent_planner(prompt, expected, attachments):
            turns.append(prompt)
            if len(turns)==1:
                return response(initial(),
                    {'tool':'read_file','action_id':'MISSING','path':str(root/'missing.txt')},
                    {'tool':'list_directory','action_id':'DEPENDENT','path':str(root),'depends_on':['MISSING']},
                    {'tool':'list_directory','action_id':'INDEPENDENT','path':str(root)})
            if len(turns)==2:
                assert 'BLOCKED_DEPENDENCY' in prompt
                assert 'DEPENDENT' not in dependent.action_result_ledger
                # Executed failure is immutable. New prerequisite ID, same
                # unexecuted dependent with corrected declaration.
                return response({**initial(),'evidence_refs':['MISSING','INDEPENDENT']},
                    {'tool':'query_project','action_id':'FOUND','project_root':str(root),
                     'queries':[{'operation':'read_range','path':'proof.txt','start_line':1,'end_line':10}]},
                    {'tool':'list_directory','action_id':'DEPENDENT','path':str(root),'depends_on':['FOUND']})
            assert len(turns)==3 and not dependent.deferred_action_repairs
            return response(finished(['MISSING','INDEPENDENT','DEPENDENT'],'PARTIAL'),
                {'tool':'final_response','action_id':'FINAL-D','content':'DEPENDENCY_OK'})
        dependent=WebAgentProtocolLoop(root,dependent_planner,progress_root=root)
        original_d=dependent._execute_action
        def execute_d(action):
            executed.append(action['action_id']); return original_d(action)
        dependent._execute_action=execute_d
        assert dependent.run('只讀測試\n'+str(root),trusted_authorized_paths=[str(root)])=='DEPENDENCY_OK'
        assert executed==['MISSING','INDEPENDENT','FOUND','DEPENDENT']
        assert dependent.action_result_ledger['MISSING']['execution_status']=='FAILED'

        # Unknown side-effect command cannot be followed by another overlapping
        # mutation even when depends_on is omitted; independent reads continue.
        observed=[]; unknown_turns=[]
        def unknown_planner(prompt, expected, attachments):
            unknown_turns.append(prompt)
            if len(unknown_turns)==1:
                return response(initial(),
                    {'tool':'run_command','action_id':'UNKNOWN-CMD','operation':'GENERAL','command':'echo probe',
                     'success_criteria':'probe observed','verify':[{'action':'file_exists','path':str(root),'expect':True}]},
                    {'tool':'write_file','action_id':'LATE-WRITE','path':str(root/'never.txt'),'content':'X'},
                    {'tool':'list_directory','action_id':'AFTER-UNKNOWN','path':str(root)})
            assert len(unknown_turns)==2 and 'BLOCKED_DEPENDENCY' in prompt
            assert not (root/'never.txt').exists()
            assert unknown.action_result_ledger['UNKNOWN-CMD']['execution_status']=='UNKNOWN'
            assert unknown.action_result_ledger['AFTER-UNKNOWN']['execution_status']=='SUCCEEDED'
            assert 'LATE-WRITE' not in unknown.action_result_ledger
            raise RuntimeError('UNKNOWN_ISOLATION_OBSERVED')
        unknown=WebAgentProtocolLoop(root,unknown_planner,progress_root=root)
        native=unknown.tools.execute
        def unknown_execute(action):
            observed.append(action['action_id'])
            if action['action_id']=='UNKNOWN-CMD': raise OSError('simulated uncertain command')
            return native(action)
        unknown.tools.execute=unknown_execute
        try:
            unknown.run('只用測試資料\n'+str(root),trusted_authorized_paths=[str(root)])
        except RuntimeError as exc:
            assert str(exc)=='UNKNOWN_ISOLATION_OBSERVED',exc
        else: raise AssertionError('missing uncertain side effect observation')
        assert observed==['UNKNOWN-CMD','AFTER-UNKNOWN']

        control=WebAgentProtocolLoop(root,lambda *args:'',progress_root=root)
        control.run_id=control.task_id=control.task_epoch='CONTROL'; control.intent_digest='a'*64
        control.authorized_paths=[str(root)]; control.tools._authorized_local_paths=[str(root)]
        valid={'tool':'list_directory','action_id':'A','path':str(root)}
        invalid={'tool':'read_file','action_id':'B','path':12}
        for commit in ({'tool':'turn_commit','action_count':2},
                       {'tool':'turn_commit','action_count':3,'action_id':'FORBIDDEN'}):
            admitted, errors=control._accept_ack([initial(),valid,invalid,commit],{})
            assert not admitted and errors and not control.action_result_ledger
        admitted,errors=control._accept_ack([initial(),valid,{**valid,'path':str(root/'other')},
                                           {'tool':'turn_commit','action_count':3}],{})
        assert not admitted and errors
        # Operation errors belong to their action, not transport authority.
        bad_command={'tool':'run_command','action_id':'CMD-BAD','operation':'NOT_AN_OPERATION','command':'echo x'}
        calls,errors=parse_v9_tool_transport(response(initial(),bad_command,valid))
        assert calls and not errors
        admitted,errors=control._accept_ack(calls,{})
        assert not errors and [a['action_id'] for a in admitted]==['P-INITIAL','A']
        assert 'CMD-BAD' in control.deferred_action_repairs
        control.deferred_action_repairs.clear()
        with patch('agent_core.write_budget.WRITE_BUDGET_LIMITS_ENABLED',True):
            batch=[initial(),valid,
                {'tool':'write_file','action_id':'W1','path':str(root/'one'),'content':'A'*1100},
                {'tool':'write_file','action_id':'W2','path':str(root/'two'),'content':'B'*1100},
                {'tool':'turn_commit','action_count':4}]
            admitted,errors=control._accept_ack(batch,{})
            assert not admitted and errors[0]['reason']=='round_write_budget_exceeded'
        # Forbidden target is rejected by the native security dispatcher while
        # an unrelated legitimate read remains executable.
        control.tools._authorized_local_paths=[str(root)]
        foreign={'tool':'list_directory','action_id':'OUTSIDE','path':str(root.parent)}
        assert not control.tools.preview_tool_scope(foreign)['allowed']
        for deps in (['A','A'],[False],[''], 'A'):
            assert not validate_tool_envelope({**valid,'depends_on':deps})[0]
        assert dependency_diagnostic({**valid,'depends_on':['A']},set(),{})
        assert dependency_diagnostic({**valid,'depends_on':['LATER']},set(),{})
        assert not dependency_diagnostic({**valid,'depends_on':['EARLIER']},{'EARLIER'}, {})
        good={'request_id':'R','task_epoch':'T','evidence_status':'ACTIVE',
              'action_id':'P','tool':'read_file',
              'execution_status':'SUCCEEDED','executed':True,'result_status':'SUCCEEDED','verification_status':'UNKNOWN'}
        check={**valid,'depends_on':['P']}
        assert not unsatisfied_dependencies(check,{'P':good},'R','T')
        for delta in ({'request_id':'FOREIGN'},{'task_epoch':'OLD'},{'evidence_status':'STALE'}, {'action_id':'OTHER'},
                      {'execution_status':'FAILED'},{'executed':False},{'result_status':'REJECTED'},
                      {'result_status':'QUEUED'},{'result_status':'UNKNOWN'}, {'verification_status':'FAIL'},
                      {'verification_status':'UNVERIFIED'}):
            assert unsatisfied_dependencies(check,{'P':{**good,**delta}},'R','T')==['P']
        control.deferred_action_repairs={'B':{'action_id':'B','status':'REPAIR_REQUIRED'}}
        admitted,errors=control._accept_ack([finished(['RUNTIME_STATUS']),
            {'tool':'final_response','action_id':'F','content':'done'}, {'tool':'turn_commit','action_count':2}],{})
        assert not admitted and errors[0]['reason']=='unresolved_action_repairs'
        control.action_result_ledger['EXISTING-FAIL']={'request_id':'CONTROL','task_epoch':'CONTROL',
            'execution_status':'FAILED','verification_status':'UNKNOWN','evidence_status':'ACTIVE'}
        for outcome in ('FAILED','PARTIAL'):
            proposed={**finished(['EXISTING-FAIL'],outcome),'decision':'CONTINUE',
                      'matched_condition':'目錄讀取失敗'}
            admitted,errors=control._accept_ack([proposed,{'tool':'final_response','action_id':'F-NORM','content':'done'},
                {'tool':'turn_commit','action_count':2}],{})
            assert not admitted and any(e['reason']=='unresolved_action_repairs' for e in errors)
        for command in ("Set-Content -LiteralPath 'probe' -Value 'X'", "Start-Process calc.exe", "Test-Path -LiteralPath 'probe'; Set-Content probe X"):
            assert not is_observation({'tool':'run_command','operation':'INSPECT','command':command})
        assert is_observation({'tool':'run_command','operation':'VERIFY','command':"Test-Path -LiteralPath 'probe'"})
        control.action_loop_state={'required_transition':'WAIT_RUNTIME','allowed_next_actions':['run_command','list_directory']}
        for command in ("Set-Content -LiteralPath 'probe' -Value 'X'", "Start-Process calc.exe"):
            diag=control._normalize_action_loop_decision(initial(),[{'tool':'run_command','operation':'INSPECT','command':command}])
            assert diag and diag['reason']=='pending_action_requires_observation'
        # Exceptions are unknown, not successful or proof of no side effects.
        control.tools.execute=lambda action: (_ for _ in ()).throw(OSError('test exception'))
        outcome=control._execute_action(valid)
        assert decode_result(outcome)['status']=='UNKNOWN'
        assert control._execute_action(valid)==outcome
    print('ACTION_SCOPED_ADMISSION_PASS scenarios=partition,dependencies,transport,identity,terminal,unknown')


if __name__=='__main__':
    run()
