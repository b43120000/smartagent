"""Typed path recovery and explicit request input reuse, with real local tools."""
import json
import tempfile
from pathlib import Path

from agent_core.typed_field_values import parse_stable_field, stable_value_valid
from agent_core.request_context import RequestContextStore, RequestContextError
from agent_core.narrative_bridge import NarrativeDecisionBridge, NarrativeBridgeError
from agent_core.protocol_v9 import parse_v9_tool_transport
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def rejects(fn,reason):
    try: fn()
    except RequestContextError as exc: assert reason in str(exc),exc
    else: raise AssertionError('expected rejection '+reason)


def run():
    from agent_core.smartagent_protocol import validate_tool_envelope
    from agent_core.tools import execute_tool
    from agent_core.tool_result_contract import decode_result
    from unittest.mock import patch
    # Bundle children use this generic validator, not request-bound admission.
    # Never silently ignore an optional output destination and overwrite input.
    unresolved={'tool':'web_edit_file','action_id':'UNRESOLVED', 'path':'source.txt',
                'instruction':'edit', 'field_refs':{'output_path':{'ref_id':'CTX-X','version':1}}}
    valid,detail=validate_tool_envelope(unresolved)
    assert not valid and 'UNEXPANDED_REQUEST_CONTEXT_REFERENCE' in str(detail)
    with patch('agent_core.tools._execute_tool_impl',side_effect=AssertionError('must not execute')):
        result=decode_result(execute_tool(unresolved))
        assert result['status']=='REJECTED' and result['executed'] is False
        assert result['reason']=='UNEXPANDED_REQUEST_CONTEXT_REFERENCE'
    path=r'C:\SmartAgent\HDRViewer\sources\Androidviewer\third_party'
    malformed='["'+path+'"]'
    value,error=parse_stable_field(malformed,'paths')
    assert value is None and error['reason']=='invalid_json_value' and 'escape' in error['detail']
    assert error['line']==1 and error['column']>0 and 'V9_PATHS' in error['hint']
    assert parse_stable_field('[V9_PATHS]\n'+path+'\n[/V9_PATHS]','paths')==([path],None)
    assert parse_stable_field(json.dumps([path]),'paths')==([path],None)
    spaced=path+' '
    assert parse_stable_field('[V9_PATHS]\n'+spaced+'\n[/V9_PATHS]','paths')[0]==[spaced]
    assert parse_stable_field('[V9_PATH]\n'+path+'\n[/V9_PATH]','path')==(path,None)
    assert parse_stable_field('[V9_PATHS]\n'+path+':secret\n[/V9_PATHS]','paths')[0]==[path+':secret']
    assert parse_stable_field('[V9_PATHS]\n'+path+'\n\n[/V9_PATHS]','paths')[1]
    for control in ('\x00','\t','\x1f'):
        assert not stable_value_valid('path',path+control)
    assert parse_stable_field('[V9_PATHS]\n'+path+'\n[/V9_PATHS]','command')[1]
    with tempfile.TemporaryDirectory(prefix='typed-context-') as temp:
        root=Path(temp); store_root=root/'request_contexts'; draft_root=root/'narrative_drafts'
        store=RequestContextStore(store_root,'REQ','EPOCH','private goal must not be persisted')
        ref=store.remember('paths',[str(root)],'USER_REQUEST')
        single=store.remember('path',str(root),'USER_REQUEST')
        assert store.resolve('paths',ref)==[str(root)]
        assert store.remember('path',str(root),'EXECUTED_ACTION')==single
        assert 'private goal' not in store.path.read_text(encoding='utf-8')
        rejects(lambda:store.resolve('path',ref),'type_mismatch')
        rejects(lambda:store.resolve('paths',{**ref,'version':True}),'version_invalid')
        rejects(lambda:store.resolve('paths',{**ref,'version':99}),'missing_or_stale')
        rejects(lambda:store.expand({'tool':'list_directory','path':str(root),'field_refs':{'path':single}}),'literal_reference_conflict')
        rejects(lambda:store.expand({'tool':'run_command','field_refs':{'command':single}}),'reference_invalid')
        rejects(lambda:store.remember('outcome','SUCCESS','USER_REQUEST'),'field_invalid')
        rejects(lambda:store.remember('workspace',str(root),'USER_REQUEST'),'field_invalid')
        rejects(lambda:store.remember('path',str(root),'arbitrary instructions'),'origin_invalid')
        rejects(lambda:RequestContextStore(store_root,'OTHER','EPOCH').resolve('path',single),'missing_or_stale')
        rejects(lambda:RequestContextStore(store_root,'REQ','OTHER').resolve('path',single),'identity_mismatch')
        store.invalidate(single['ref_id'])
        rejects(lambda:store.resolve('path',single),'missing_or_stale')
        newer=store.remember('path',str(root),'USER_REQUEST')
        assert newer['version']==3 and store.resolve('path',newer)==str(root)
        payload=json.loads(store.path.read_text(encoding='utf-8'))
        assert len(payload['objects'][single['ref_id']])==3
        expected={'run_id':'REQ','turn_id':1,'narrative_recovery_context':{
            'goal':'inspect directory','progress':initial(),'authorized_paths':[str(root)],'request_context_epoch':'EPOCH'}}
        bridge=NarrativeDecisionBridge.create(expected=expected,source_text='Inspect the directory',root=draft_root)
        bridge.accept_reply('ACTION'); bridge.accept_reply('inspect_directory')
        assert 'V9_PATHS' in bridge.next_prompt()
        bridge.accept_reply(malformed)
        assert bridge.draft.pending_slot=='field:paths' and bridge.draft.validation_errors[0]['reason']=='invalid_json_value'
        assert 'column' in bridge.next_prompt() and 'escaped backslashes' in bridge.next_prompt()
        bridge.accept_reply('[V9_PATHS]\n'+str(root)+'\n[/V9_PATHS]')
        calls,errors=parse_v9_tool_transport(bridge.canonical_response())
        assert not errors and calls[1]['paths']==[str(root)]
        # Explicit bridge ref is preserved in canonical action, then expanded by
        # the same normal admission path as ordinary planner field_refs.
        expected['turn_id']=2
        reused=NarrativeDecisionBridge.create(expected=expected,source_text='inspect same confirmed directory',root=draft_root)
        reused.accept_reply('ACTION'); reused.accept_reply('inspect_directory')
        reused.accept_reply('[V9_CONTEXT_REF]\n'+json.dumps(ref)+'\n[/V9_CONTEXT_REF]')
        calls,errors=parse_v9_tool_transport(reused.canonical_response())
        assert not errors and 'paths' not in calls[1] and calls[1]['field_refs']=={'paths':ref}
        assert store.expand(calls[1])['paths']==[str(root)]
        try:
            NarrativeDecisionBridge.create(expected={**expected,'narrative_recovery_context':{
                **expected['narrative_recovery_context'],'request_context_epoch':'NEW'}},
                source_text='inspect same confirmed directory',root=draft_root)
        except NarrativeBridgeError as exc: assert 'identity mismatch' in str(exc)
        else: raise AssertionError('resumed wrong-epoch draft')
        snapshot=store.path.read_text(encoding='utf-8')
        corrupt=json.loads(snapshot); corrupt['objects'][ref['ref_id']][0]['value']=['tampered']
        store.path.write_text(json.dumps(corrupt),encoding='utf-8')
        rejects(lambda:store.catalog(),'corrupt')
        store.path.write_text(snapshot,encoding='utf-8')

        prompts=[]; invoked=[]; chosen={}
        def planner(prompt,expected,attachments):
            prompts.append(prompt)
            catalog=loop.request_context.catalog()
            assert '[REQUEST_CONTEXT_OBJECTS]' in prompt
            if len(prompts)==1:
                entry=next(item for item in catalog['objects'] if item['field']=='path' and item['value']==str(root))
                chosen.update(ref_id=entry['ref_id'],version=entry['version'])
            if len(prompts)<=2:
                return response(initial(),{'tool':'list_directory','action_id':'READ-ONCE','field_refs':{'path':dict(chosen)}})
            final={**initial(),'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
                   'steps':[{'step':1,'desc':'read','status':'COMPLETED'}],
                   'matched_condition':'目錄資料已讀取','evidence_refs':['READ-ONCE']}
            return response(final,{'tool':'final_response','action_id':'F','content':'REF_REUSE_OK'})
        loop=WebAgentProtocolLoop(root,planner,progress_root=root)
        native=loop.tools.execute
        def execute(action): invoked.append(action['action_id']); return native(action)
        loop.tools.execute=execute
        assert loop.run('read directory\n'+str(root),trusted_authorized_paths=[str(root)])=='REF_REUSE_OK'
        assert len(prompts)==3 and invoked==['READ-ONCE']
        # Context literals cannot widen scope. Even a deliberately inserted
        # out-of-scope ref remains rejected by the existing target policy.
        outsider=loop.request_context.remember('path',str(root.parent),'USER_REQUEST')
        expanded=loop.request_context.expand({'tool':'list_directory','action_id':'OUTSIDE','field_refs':{'path':outsider}})
        assert not loop.tools.preview_tool_scope(expanded)['allowed']
        assert not loop._execute_action(expanded).startswith('[DIR]')
        assert not loop.request_context.expand({'tool':'read_file','action_id':'MISSING'}).get('path')
    print('TYPED_PATHS_REQUEST_CONTEXT_PASS')


if __name__=='__main__':run()
