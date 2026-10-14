"""Integration: legal reads survive display repairs without accepting bad state."""
import tempfile
from pathlib import Path
from copy import deepcopy

from agent_core.task_progress import initialize_progress, record_model_progress, read_progress, validate_model_progress
from agent_core.progress_reply_contract import materialize_progress
from agent_core.progress_recovery import repair_diagnostic, repair_signature, read_fact_digest
from agent_core.tool_result_contract import decode_result, adapt_legacy_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def initial():
    return {'tool':'report_progress','action_id':'P-INITIAL', 'decision':'CONTINUE','outcome':'PENDING',
            'base_evaluation':'只需讀取指定目錄', 'total_steps':1,'current_step':0.5,
            'steps':[{'step':1,'desc':'讀取目錄','status':'IN_PROGRESS'}],
            'current_focus':'讀取目錄', 'next_action':'list_directory',
            'completion_contract':{'success':['目錄資料已讀取'], 'failure':['目錄讀取失敗'],
                                   'in_progress':['仍需讀取目錄'], 'interrupted':['需要使用者補充']},
            'matched_condition':'仍需讀取目錄', 'evidence_refs':['RUNTIME_STATUS'], 'decision_reason':'需要實際資料'}


def run():
    with tempfile.TemporaryDirectory(prefix='flexible-progress-') as temp:
        root=Path(temp); sub=root/'sub'; sub.mkdir(); (sub/'proof.txt').write_text('OK')
        executed=[]; prompts=[]
        def planner(prompt, expected, attachments):
            prompts.append(prompt)
            if len(prompts)==1:
                return response(initial(), {'tool':'list_directory','action_id':'A-ROOT','path':str(root)})
            if len(prompts)==2:
                # Bad display update, but explicit CONTINUE with current evidence.
                p={'tool':'report_progress','action_id':'P-BAD','decision':'CONTINUE',
                   'current_focus':'', 'next_action':'list_directory','decision_reason':'讀取已確認的子目錄',
                   'evidence_refs':['A-ROOT']}
                return response(p, {'tool':'list_directory','action_id':'A-SUB','path':str(sub)})
            assert '[PROGRESS_FIELD_REPAIR]' in prompt and 'proof.txt' in prompt
            stored=read_progress(loop.task_id, root)
            assert stored.current_focus=='讀取目錄' and stored.total_steps==1
            assert stored.repair_state['status']=='REPAIR_REQUIRED'
            assert any('A-SUB' in row['executed_action_ids'] for row in stored.repair_state['history'])
            p={'tool':'report_progress','action_id':'P-FINAL','decision':'COMPLETE','outcome':'SUCCESS',
               'current_step':'1','steps':[{'step':'1','desc':'讀取目錄','status':'COMPLETE'}],
               'current_focus':'已讀取目錄','decision_reason':'已取得資料', 'evidence_refs':['A-SUB'],
               'matched_condition':'目錄資料已讀取'}
            return response(p, {'tool':'final_response','action_id':'F','content':'READ_OK'})
        loop=WebAgentProtocolLoop(root, planner, progress_root=root)
        original=loop._execute_action
        def execute(action):
            executed.append(action['action_id']); return original(action)
        loop._execute_action=execute
        assert loop.run('只讀取目錄資料\n'+str(root), trusted_authorized_paths=[str(root)])=='READ_OK'
        assert executed==['A-ROOT','A-SUB'] and len(prompts)==3
        ledger=read_progress(loop.task_id, root)
        assert ledger.repair_state['status']=='RESOLVED'
        # No PATCH marker is needed; strings normalize without inheriting evidence.
        p={'tool':'report_progress','action_id':'P','decision':'CONTINUE','current_focus':'下一步',
           'next_action':'list_directory','decision_reason':'新資料','evidence_refs':['RUNTIME_STATUS']}
        saved=initialize_progress('NEG',request_id='NEG',goal='readonly',root=root)
        saved=record_model_progress('NEG',initial(),request_id='NEG',goal='readonly',round_id=1,root=root)
        assert materialize_progress(p,saved)['total_steps']==1
        assert materialize_progress({**p,'progress_update':'FULL'},saved)['total_steps']==1
        numeric={**p,'total_steps':'1','current_step':'0.5'}
        assert validate_model_progress(numeric,saved)['current_step']==0.5
        control=WebAgentProtocolLoop(root,lambda *a:'',progress_root=root)
        control.run_id=control.task_id=control.task_epoch='NEG'; control.intent_digest='a'*64
        control.progress_ledger=saved; control.authorized_paths=[str(root)]
        control.tools._authorized_local_paths=[str(root)]
        bad={**p,'current_focus':''}
        for action in ({'tool':'write_file','action_id':'M','path':str(root/'no-write'),'content':'X'},
                       {'tool':'final_response','action_id':'F','content':'done'}):
            accepted, errors=control._accept_ack([bad,action,{'tool':'turn_commit','action_count':2}],{'ack_web_ack_id':''})
            assert not accepted and errors
        assert not (root/'no-write').exists()
        accepted, errors=control._accept_ack([bad,
            {'tool':'list_directory','action_id':'R-MIX','path':str(root)},
            {'tool':'write_file','action_id':'M-MIX','path':str(root/'no-write'),'content':'X'},
            {'tool':'turn_commit','action_count':3}],{'ack_web_ack_id':''})
        assert not accepted and errors and not (root/'no-write').exists()
        forbidden={**bad,'decision':'INTERRUPT','outcome':'UNKNOWN'}
        accepted,errors=control._accept_ack([forbidden,{'tool':'list_directory','action_id':'R','path':str(root)},
                                           {'tool':'turn_commit','action_count':2}],{'ack_web_ack_id':''})
        assert not accepted and errors
        bad_refs={**bad,'evidence_refs':['FORGED']}
        accepted,errors=control._accept_ack([bad_refs,{'tool':'list_directory','action_id':'R','path':str(root)},
                                           {'tool':'turn_commit','action_count':2}],{'ack_web_ack_id':''})
        assert not accepted and errors
        multi=repair_diagnostic({'decision':'CONTINUE','current_focus':'','decision_reason':'','evidence_refs':[]},saved,ValueError('missing'))
        assert {'current_focus','decision_reason','evidence_refs','next_action'} <= {r['field'] for r in multi['field_issues']}
        reduced=deepcopy(multi); reduced['field_issues']=reduced['field_issues'][1:]
        assert repair_signature(multi,1)!=repair_signature(reduced,1)
        assert repair_signature(multi,1)!=repair_signature(multi,2)
        # Reproduce F768: outcome repaired first, evidence repaired next. The
        # held read must survive both replies and execute exactly once.
        repair_turns=[]; repair_actions=[]
        bad_initial={**initial(), 'outcome':'in_progress', 'evidence_refs':[]}
        first_diag=repair_diagnostic(bad_initial,None,ValueError('invalid progress outcome: IN_PROGRESS'),
                                     {'REQUEST_ACCEPTED','RUNTIME_STATUS'})
        second_diag=repair_diagnostic({**bad_initial,'outcome':'PENDING'},None,
                                      ValueError('evidence_refs must contain 1..32 references'),
                                      {'REQUEST_ACCEPTED','RUNTIME_STATUS'})
        assert {'outcome','evidence_refs'} <= {i['field'] for i in first_diag['field_issues']}
        assert 'outcome' not in {i['field'] for i in second_diag['field_issues']}
        assert repair_signature(first_diag,0)!=repair_signature(second_diag,0)
        assert repair_signature(second_diag,0)==repair_signature({**second_diag,'suggestion':'changed prose'},0)
        assert first_diag['available_evidence_refs']==['REQUEST_ACCEPTED','RUNTIME_STATUS']
        assert repair_signature(first_diag,0)==repair_signature({**first_diag,
            'detail':'invalid progress outcome: ANOTHER_INVALID_VALUE'},0)
        oversized=repair_diagnostic({**initial(),'evidence_refs':['RUNTIME_STATUS']*33},None,ValueError('too many'))
        assert any(i['field']=='evidence_refs' for i in oversized['field_issues'])
        def repair_planner(prompt, expected, attachments):
            repair_turns.append(prompt)
            if len(repair_turns)==1:
                return response(bad_initial,{'tool':'list_directory','action_id':'HELD-READ','path':str(sub)})
            if len(repair_turns)==2:
                assert 'available_evidence_refs' in prompt and 'REQUEST_ACCEPTED' in prompt
                return response({**bad_initial,'outcome':'PENDING'})
            if len(repair_turns)==3:
                assert 'evidence_refs' in prompt and 'RUNTIME_STATUS' in prompt
                return response({**initial(),'evidence_refs':['REQUEST_ACCEPTED']})
            assert len(repair_turns)==4 and 'proof.txt' in prompt
            return response({**initial(),'decision':'COMPLETE','outcome':'SUCCESS',
                'current_step':1,'steps':[{'step':1,'desc':'讀取目錄','status':'COMPLETED'}],
                'matched_condition':'目錄資料已讀取','evidence_refs':['HELD-READ']},
                {'tool':'final_response','action_id':'REPAIRED-FINAL','content':'REPAIR_SEQUENCE_OK'})
        repaired=WebAgentProtocolLoop(root,repair_planner,progress_root=root)
        repair_original=repaired._execute_action
        def repair_execute(action):
            repair_actions.append(action['action_id']); return repair_original(action)
        repaired._execute_action=repair_execute
        assert repaired.run('只讀取目錄\n'+str(sub),trusted_authorized_paths=[str(sub)])=='REPAIR_SEQUENCE_OK'
        assert repair_actions==['HELD-READ'] and len(repair_turns)==4
        # Diagnostic candidates never advertise stale/foreign action evidence.
        repaired.action_result_ledger.update({
            'STALE':{'evidence_status':'STALE'},
            'FOREIGN':{'request_id':'OTHER'},
            'OLD-EPOCH':{'task_epoch':'OTHER'},
        })
        offered=repaired._progress_repair_diagnostic(bad_initial,ValueError('invalid outcome'))['available_evidence_refs']
        assert 'HELD-READ' in offered and not {'STALE','FOREIGN','OLD-EPOCH'} & set(offered)
        stalled_repairs=[]; stalled_actions=[]
        def repeat_invalid(prompt, expected, attachments):
            stalled_repairs.append(prompt)
            progress={**initial(),'evidence_refs':[],'decision_reason':'different prose '+str(len(stalled_repairs))}
            if len(stalled_repairs)==1:
                return response(progress,{'tool':'list_directory','action_id':'NEVER-EXECUTE','path':str(sub)})
            return response(progress)
        repeat_loop=WebAgentProtocolLoop(root,repeat_invalid,progress_root=root)
        repeat_loop._execute_action=lambda action: stalled_actions.append(action)
        try:
            repeat_loop.run('只讀目錄\n'+str(sub),trusted_authorized_paths=[str(sub)])
        except RuntimeError as exc:
            assert 'evidence_refs must contain' in str(exc), exc
        else:
            raise AssertionError('unchanged missing evidence bypassed stagnation guard')
        assert len(stalled_repairs)==2 and not stalled_actions
        import json
        query={'tool':'query_project'}
        q={'status':'READY','snapshot_id':'S','results':[{'content':'same'}],'query_id':'A'}
        assert read_fact_digest(query,json.dumps(q))==read_fact_digest(query,json.dumps({**q,'query_id':'B'}))
        assert read_fact_digest(query,json.dumps(q))!=read_fact_digest(query,json.dumps({**q,'results':[{'content':'new'}]}))
        inspect={'tool':'inspect_directory'}
        sample={'elapsed_ms':1,'paths':[{'elapsed_ms':2,'file_count':3}]}
        assert read_fact_digest(inspect,json.dumps(sample))==read_fact_digest(inspect,json.dumps({'elapsed_ms':99,'paths':[{'elapsed_ms':88,'file_count':3}]}))
        assert read_fact_digest(inspect,json.dumps(sample))!=read_fact_digest(inspect,json.dumps({'elapsed_ms':99,'paths':[{'elapsed_ms':88,'file_count':4}]}))
        assert decode_result(adapt_legacy_result('list_directory','[錯誤] 目錄不存在'))['status']=='FAILED'
        assert decode_result(adapt_legacy_result('write_file','something happened'))['status']=='UNKNOWN'
        from agent_core.stage_result import classify_tool_result
        assert classify_tool_result('list_directory', adapt_legacy_result('list_directory','[錯誤] 目錄不存在'))['status']=='FAILED'
        assert classify_tool_result('write_file', adapt_legacy_result('write_file','something happened'))['status']=='FAILED'
        from agent_core.stage_result import safe_stage_summary
        assert safe_stage_summary('list_directory',adapt_legacy_result('list_directory','[錯誤] 目錄不存在'),'FAILED')['reason_code']=='STATUS_FAILED'
        # Reusing a loop cannot borrow PASS or repair state from another request.
        control.tools.last_verification_status='PASS'; control.deferred_progress_repair=multi
        def stop(*args):
            assert control.tools.last_verification_status is None and not control.deferred_progress_repair
            raise RuntimeError('RESET_OK')
        control.planner=stop
        try: control.run('新的唯讀任務')
        except RuntimeError as exc: assert str(exc)=='RESET_OK'
        else: raise AssertionError('missing reset check')
        turns=[]
        def stalled_planner(prompt, expected, attachments):
            turns.append(prompt)
            if len(turns)==1:
                return response(initial(),{'tool':'list_directory','action_id':'START','path':str(sub)})
            return response({**p,'action_id':'P-STALLED-'+str(len(turns)),'current_focus':''},
                {'tool':'list_directory','action_id':'READ-STALLED-'+str(len(turns)),'path':str(sub)})
        stalled=WebAgentProtocolLoop(root,stalled_planner,progress_root=root)
        try: stalled.run('只讀取既有目錄',trusted_authorized_paths=[str(root)])
        except RuntimeError as exc: assert '重複讀取未取得新資料' in str(exc), exc
        else: raise AssertionError('repeated deferred reads bypassed stagnation guard')
        assert len(turns)==3 and read_progress(stalled.task_id,root).runtime_state=='PAUSED'
    print('FLEXIBLE_PROGRESS_ADMISSION_OK')


if __name__=='__main__':run()
