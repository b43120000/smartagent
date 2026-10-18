"""Real completion/admission paths: fresh image beats display-only Progress."""
import json,tempfile,threading
from pathlib import Path
from unittest.mock import patch
from agent_core.web_runtime import WebLLMScraper
from agent_core.protocol_v9 import parse_v9_tool_transport
from agent_core.task_progress import validate_model_progress, record_model_progress
from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.security_context import SecurityContext
from agent_core.tools import execute_tool
from agent_core.tool_result_contract import decode_result

def wire(*calls):
    return '```smartagent_tool\n'+'\n'.join(json.dumps(c) for c in [*calls,{'tool':'turn_commit','action_count':len(calls)}])+'\n```'

def wait_fixture(text,expected,*,cancel=False,service='chatgpt',priority=True):
    s=WebLLMScraper.__new__(WebLLMScraper);s.service=service;s._cancel_requested=threading.Event();polls=[];logs=[]
    s._wait_for_new_assistant_turn=lambda *a,**k:object()
    s._fresh_ready_page_image_state=lambda *a,**k:{'ready':True}
    s._register_fresh_page_image_immediately=lambda *a:{'artifact_id':'FRESH'}
    for name in ('_dismiss_known_blocking_dialogs','_run_control_hook','_check_provider_transport_error'):
        setattr(s,name,lambda:None)
    def check(stage):
        if cancel and stage=='image_delivery_priority':
            s._cancel_requested.set();raise RuntimeError('TEST_CANCEL')
    s._check_cancel_requested=check
    s._maybe_recover_disconnected_generation=lambda:False
    s._turn_elements=lambda role:[];s._disconnect_signature_visible=lambda:False
    s._is_generation_active=lambda:False;s._current_new_response_text=lambda *a:text
    s._ui_idle_grace_sec=0.5;s._protocol_commit_stable_sec=0.5
    s._active_generation_emergency_sec=1000;s._active_generation_warn_sec=500
    s._log_stage=lambda stage,detail='':logs.append(stage);s._emit_status=lambda *a,**k:None
    def activity(*a):
        polls.append(len(polls));assert len(polls)<30,'wait did not converge'
        return dict(progress_signature='ready',generation_active=len(polls)<=2,media_pending=False,
            image_ready=1,image_count=1,busy_count=0,text_len=len(text),mutation_count=0)
    s._assistant_activity_state=activity
    now=[1.0]
    def clock():now[0]+=0.3;return now[0]
    with patch('agent_core.web_runtime.time.time',side_effect=clock),patch('agent_core.web_runtime.time.sleep'):
        try:result=s._wait_for_response_complete({},protocol_expected=expected)
        except RuntimeError as exc:
            assert cancel and str(exc)=='TEST_CANCEL' and s._cancel_requested.is_set();return
    assert not cancel
    assert 'generating' in logs and ('runtime_image_delivery_priority' in logs)==priority,logs
    return result

def run():
    with tempfile.TemporaryDirectory(prefix='image-priority-') as temp:
        root=Path(temp);loop=WebAgentProtocolLoop(root,lambda *a:'',progress_root=root)
        loop.run_id='RR-PRIORITY';loop.task_id='TASK-PRIORITY';loop.task_epoch='E';loop.intent_digest='I'
        loop.image_delivery_plan={'delivery':'local','output_path':str(root/'fresh.png'),'expected_filename':'fresh.png'}
        expected=loop._new_commit()
        # Missing display fields must not create another Web repair turn.
        progress={'tool':'report_progress','action_id':'MODEL-P','decision':'CONTINUE'}
        text=wire(progress)
        preferred=WebLLMScraper._runtime_progress_image_delivery_response(text,expected,fresh_artifact_seen=True)
        calls,errors=parse_v9_tool_transport(preferred);assert not errors and len(calls)==3
        validate_model_progress(calls[0]);assert calls[1]['tool']=='download_artifact'
        accepted,diagnostics=loop._accept_v8_ack(calls,expected)
        assert not diagnostics and [c['tool'] for c in accepted]==['report_progress','download_artifact'],diagnostics
        result=wait_fixture(text,expected);assert result==preferred
        assert wait_fixture(text,expected,service='gemini',priority=False)==text
        wait_fixture(text,expected,cancel=True)
        for changed in ({'artifact_save_expected':False},{'artifact_auto_delivery_allowed':False},{'artifact_output_path':''}):
            assert not WebLLMScraper._runtime_progress_image_delivery_response(text,{**expected,**changed},fresh_artifact_seen=True)
        assert not WebLLMScraper._runtime_progress_image_delivery_response(text,expected,fresh_artifact_seen=False)
        for decision in ('COMPLETE','INTERRUPT'):
            assert not WebLLMScraper._runtime_progress_image_delivery_response(wire({**progress,'decision':decision}),expected,fresh_artifact_seen=True)
        for extra in ({'tool':'list_directory','action_id':'READ','path':str(root)},
                      {'tool':'final_response','action_id':'FINAL','content':'stop'},
                      {'tool':'download_artifact','action_id':'MODEL-D','output_path':str(root/'fresh.png')}):
            mixed=wire(progress,extra)
            assert not WebLLMScraper._runtime_progress_image_delivery_response(mixed,expected,fresh_artifact_seen=True)
            assert wait_fixture(mixed,expected,priority=False)==mixed
        outside=root.parent/(root.name+'-outside.png')
        forbidden={**calls[1],'output_path':str(outside)}
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)), \
             patch('agent_core.web_runtime.get_manager') as manager:
            refused=decode_result(execute_tool(forbidden,loop.tools))
        assert refused['status']=='REJECTED' and refused['executed'] is False,refused
        assert not outside.exists() and not manager.called
        later={**expected,'turn_id':expected['turn_id']+1}
        same,_=parse_v9_tool_transport(WebLLMScraper._runtime_fresh_image_delivery_response(later,fresh_artifact_seen=True))
        assert calls[1]==same[1]  # stable effect identity across repair rounds
        # A later round must emit fresh decision/evidence fields instead of
        # relying on PATCH inheritance, and must retain the accepted contract.
        loop.progress_ledger=record_model_progress(loop.task_id,calls[0],request_id=loop.run_id,
            goal='Save authorized image',round_id=1,root=root)
        previous_contract=loop.progress_ledger.completion_contract.copy()
        later_expected=loop._new_commit()
        later_text=WebLLMScraper._runtime_progress_image_delivery_response(text,later_expected,fresh_artifact_seen=True)
        later_calls,errors=parse_v9_tool_transport(later_text);assert not errors
        accepted,diagnostics=loop._accept_v8_ack(later_calls,later_expected)
        assert not diagnostics,diagnostics
        assert later_calls[0]['decision']=='CONTINUE' and later_calls[0]['outcome']=='PENDING'
        assert later_calls[0]['evidence_refs']==['RUNTIME_STATUS']
        assert later_calls[0]['completion_contract']==previous_contract
        loop.action_ledger['FAILED-DOWNLOAD']={'action':calls[1],'result':'failed'}
        blocked=loop._new_commit();assert blocked['artifact_auto_delivery_allowed'] is False
        assert not WebLLMScraper._runtime_fresh_image_delivery_response(blocked,fresh_artifact_seen=True)
    print('IMAGE_DELIVERY_PRIORITY_PASS')
if __name__=='__main__':run()
