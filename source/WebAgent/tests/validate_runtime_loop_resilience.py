"""Eight anti-loop contracts; no browser, network or real user file changes."""
import tempfile
from pathlib import Path
from agent_core.smartagent_protocol import normalize_tool_numeric_fields, validate_tool_envelope
from agent_core.action_loop_state import build_action_loop_state, action_lifecycle, classify_evidence_state
from agent_core.tool_result_contract import attach_runtime_result
from agent_core.path_security import canonicalize_path, PathSecurityError
from WebAgent.protocol_loop import WebAgentProtocolLoop, extract_authorized_paths


def run():
    numeric={'tool':'inspect_directory','action_id':'A','paths':['x'],'sample_limit':'3','recursive':'false'}
    fixed=normalize_tool_numeric_fields(numeric)
    assert fixed['sample_limit']==3 and fixed['recursive']=='false' and numeric['sample_limit']=='3'
    assert not validate_tool_envelope(fixed)[0] # bools never guessed
    valid,issues=validate_tool_envelope({'tool':'run_command','timeout':'wrong'})
    assert not valid and {item['field'] for item in issues['field_issues']}=={'command','operation','timeout'}
    assert normalize_tool_numeric_fields({'tool':'write_file','path':'3','content':'3'})['content']=='3'
    assert normalize_tool_numeric_fields({'tool':'run_command','timeout':'3 ms'})['timeout']=='3 ms'
    assert normalize_tool_numeric_fields({'tool':'inspect_directory','sample_limit':True})['sample_limit'] is True
    query=normalize_tool_numeric_fields({'tool':'query_project','queries':[{'operation':'read_range','start_line':'3','end_line':'20'}]})
    assert query['queries'][0]['start_line']==3 and query['queries'][0]['end_line']==20
    assert extract_authorized_paths(r'C:\workspace\project，snapshot_id=S，queries=[{operation:read_range}]')==[r'C:\workspace\project']
    assert extract_authorized_paths(r'"C:\workspace\file，snapshot_id=literal.txt"')==[r'C:\workspace\file，snapshot_id=literal.txt']
    for text in (r'C:\workspace\file:secret，snapshot_id=S',r'"C:\workspace\file":secret，queries=[]',r'C:\workspace\file，suffix:secret'):
        try:
            canonicalize_path(extract_authorized_paths(text)[0],require_absolute=True)
        except PathSecurityError:
            pass
        else:
            raise AssertionError('ADS must not disappear')
    for status in ('QUEUED','RUNNING','PARTIAL','REJECTED','UNKNOWN'):
        action={'tool':'return_artifact','action_id':'PENDING'}
        assert action_lifecycle(action,status,'PASS','AVAILABLE') not in {'VERIFIED_PASS','EXECUTED'}
        state=build_action_loop_state(action=action,execution_status=status,verification_status='UNKNOWN')
        assert state['blockers'] and not state['blockers'][0]['satisfied']
        if status in {'QUEUED','RUNNING','PARTIAL'}:
            assert state['evidence_state']=='PENDING'
            assert state['required_transition']=='WAIT_RUNTIME'
            assert state['blocked_action_signatures']
    failed=build_action_loop_state(action={'tool':'read_file','action_id':'DENIED'},execution_status='REJECTED',
        available_tools=['list_directory','read_file','run_command'])
    assert set(failed['allowed_next_actions']) <= {'list_directory','read_file','run_command'}
    assert failed['blockers'][0]['blocking_action_id']=='DENIED'
    passed=build_action_loop_state(action={'tool':'run_command','operation':'VERIFY'},execution_status='SUCCEEDED',verification_status='PASS')
    assert not passed['blockers'] and passed['required_transition']=='CONTINUE_PLAN'
    with tempfile.TemporaryDirectory(prefix='runtime-resilience-') as temp:
        root=Path(temp); loop=WebAgentProtocolLoop(root,lambda *_:'',progress_root=root)
        loop.run_id='REQ';loop.task_id='TASK';loop.task_epoch='EPOCH';loop.turn_id=1
        first={'tool':'list_directory','action_id':'A','path':str(root)}
        second={'tool':'find_file','action_id':'B','pattern':'*.txt'}
        loop.pending_progress_repair_actions=[first,second]
        partial=[{'tool':'report_progress'},first,{'tool':'turn_commit','action_count':2}]
        restored=loop._restore_progress_repair_actions(partial)
        assert restored[1:3]==[first,second] and restored[-1]['action_count']==3
        assert not loop.action_ledger # restoration does not execute
        changed=[{'tool':'report_progress','decision':'CONTINUE'},dict(first,path=str(root/'other')),{'tool':'turn_commit','action_count':2}]
        restored=loop._restore_progress_repair_actions(changed)
        _,diagnostics=loop._accept_v8_ack(restored,{})
        assert diagnostics[0]['reason']=='pending_actions_not_preserved'
        assert loop.pending_progress_repair_actions==[first,second]
        loop.action_loop_state=failed
        assert loop._normalize_action_loop_decision({'decision':'INTERRUPT'},[{'tool':'final_response'}]) is None
        assert loop._normalize_action_loop_decision({'decision':'COMPLETE'},[{'tool':'final_response'}]) is None
        # These proposals still require the later objective terminal/interrupt gate.
        loop.pending_progress_repair_actions=[]
        waiting=build_action_loop_state(action={'tool':'return_artifact','action_id':'Q','path':str(root/'queued.txt')},execution_status='QUEUED')
        loop.action_loop_state=waiting
        assert loop._normalize_action_loop_decision({'decision':'CONTINUE'},[{'tool':'write_file','action_id':'NEW','path':str(root/'other'),'content':'x'}])['reason']=='required_transition_action_missing'
        assert loop._normalize_action_loop_decision({'decision':'CONTINUE'},[{'tool':'run_command','operation':'MUTATE','action_id':'NEW'}])['reason']=='pending_action_requires_observation'
        read={'tool':'query_project','action_id':'READ','project_root':str(root),'queries':[{'operation':'read_range','path':'one.txt'}]}
        loop.action_ledger['READ']={'action':read,'result':''}
        loop._record_action_result_evidence(read,{'execution_status':'SUCCEEDED','verification_status':'UNKNOWN','evidence_state':'AVAILABLE','terminal_eligible':True})
        other={'tool':'read_file','action_id':'OTHER','path':str(root/'other.txt')}
        loop.action_ledger['OTHER']={'action':other,'result':''}
        loop._record_action_result_evidence(other,{'execution_status':'SUCCEEDED','verification_status':'PASS','terminal_eligible':True})
        mutate={'tool':'write_file','action_id':'CHANGE','path':str(root/'one.txt'),'content':'NEW'}
        loop.action_ledger['CHANGE']={'action':mutate,'result':''}
        loop._record_action_result_evidence(mutate,{'execution_status':'SUCCEEDED','verification_status':'UNVERIFIED','terminal_eligible':False})
        assert loop.action_result_ledger['READ']['evidence_status']=='STALE'
        assert loop.action_result_ledger['OTHER']['evidence_status']=='ACTIVE'
        assert loop._terminal_evidence_verdict(['READ'])=='UNVERIFIED'
        remedy=loop._terminal_recovery_requirement({'evidence_refs':['READ']},'UNVERIFIED')
        assert remedy['reason_code']=='STALE_EVIDENCE' and remedy['required_action']=='query_project'
        loop.action_result_ledger['OTHER']['request_id']='FOREIGN'
        assert not loop._resolve_effective_evidence_entries(['OTHER'])
        begin={'tool':'begin_file_write','action_id':'BEGIN','write_id':'W','path':str(root/'written.txt')}
        loop.action_ledger['BEGIN']={'action':begin,'result':attach_runtime_result('OPEN','SUCCEEDED','STAGED',tool='begin_file_write',executed=True,data={'transaction_state':'OPEN'})}
        loop._record_action_result_evidence(begin,{'execution_status':'SUCCEEDED','verification_status':'UNKNOWN','terminal_eligible':False})
        remedy=loop._terminal_recovery_requirement({'evidence_refs':['BEGIN']},'UNVERIFIED')
        assert remedy['required_action']=='commit_file_write' and remedy['write_id']=='W'
        # A later PASS read cannot hide the ACTIVE OPEN transaction.
        records=[{'action':begin,'result':loop.action_ledger['BEGIN']['result'],
                  'execution_status':'SUCCEEDED','verification_status':'UNKNOWN','evidence_status':'ACTIVE'},
                 {'action':other,'result':'read','execution_status':'SUCCEEDED','verification_status':'PASS','evidence_status':'ACTIVE'}]
        state=build_action_loop_state(action=other,result='read',execution_status='SUCCEEDED',verification_status='PASS',action_records=records)
        assert any(item['reason']=='OPEN_WRITE_TRANSACTION' for item in state['blockers'])
        loop.action_loop_state=state
        loop.action_result_ledger['OTHER']['request_id']='REQ'
        from WebAgent.tests.validate_action_loop_runtime_state import canonical_progress
        terminal=canonical_progress(decision='COMPLETE',outcome='SUCCESS',current_step=2,
            steps=[{'step':1,'desc':'read','status':'COMPLETED'},{'step':2,'desc':'finish','status':'COMPLETED'}],
            matched_condition='verified',evidence_refs=['OTHER'])
        _,diag=loop._accept_v8_ack([terminal,{'tool':'final_response','action_id':'FINAL','content':'done'},
                                  {'tool':'turn_commit','action_count':2}],{})
        assert diag[0]['reason']=='active_action_not_complete',diag
        commit={'tool':'commit_file_write','action_id':'COMMIT','write_id':'W'}
        loop.action_ledger['COMMIT']={'action':commit,'result':''}
        loop.pending_verification_requirement=remedy
        loop._record_action_result_evidence(commit,{'execution_status':'SUCCEEDED','verification_status':'UNKNOWN','terminal_eligible':True})
        assert loop.action_result_ledger['BEGIN']['evidence_status']=='SUPERSEDED'
        assert loop._resolve_effective_evidence_entries(['BEGIN'])[0][0]=='COMMIT'
        assert not loop.pending_verification_requirement
        begin2=dict(begin,action_id='BEGIN2',write_id='W2')
        loop.action_ledger['BEGIN2']={'action':begin2,'result':loop.action_ledger['BEGIN']['result']}
        loop._record_action_result_evidence(begin2,{'execution_status':'SUCCEEDED','verification_status':'UNKNOWN','terminal_eligible':False})
        abort={'tool':'abort_file_write','action_id':'ABORT','write_id':'W2'}
        loop.action_ledger['ABORT']={'action':abort,'result':''}
        loop._record_action_result_evidence(abort,{'execution_status':'SUCCEEDED','verification_status':'UNKNOWN','terminal_eligible':True})
        assert loop._resolve_effective_evidence_entries(['BEGIN2'])[0][0]=='ABORT'
        unknown={'tool':'run_command','action_id':'U','operation':'MUTATE','command':'test'}
        loop.action_ledger['U']={'action':unknown,'result':''}
        loop._record_action_result_evidence(unknown,{'execution_status':'UNKNOWN','verification_status':'UNKNOWN','terminal_eligible':False})
        verifier={'tool':'run_command','action_id':'V','operation':'VERIFY','verifies_action_id':'U'}
        loop.action_ledger['V']={'action':verifier,'result':''}
        loop._record_action_result_evidence(verifier,{'execution_status':'SUCCEEDED','verification_status':'PASS','terminal_eligible':True})
        assert loop.action_result_ledger['U']['execution_status']=='UNKNOWN'
        assert loop.action_result_ledger['U']['pending_resolution']=='SCOPED_VERIFICATION_OBTAINED'
        assert loop._terminal_evidence_verdict(['U'])=='PASS'
        assert loop._terminal_evidence_verdict(['U','V'])=='PASS'
        produced=loop.action_result_ledger['U']['produced_round']
        loop.turn_id+=1
        reused=loop._record_action_result_evidence(unknown,{'execution_status':'UNKNOWN','verification_status':'UNKNOWN'})
        assert reused['produced_round']==produced and reused['effective_verification_status']=='PASS'
    print('RUNTIME_LOOP_RESILIENCE_PASS categories=8')


if __name__=='__main__':run()
