"""P0 artifact scheduling is not delivery; no network and isolated files only."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from agent_core.tools import execute_tool
from agent_core.security_context import SecurityContext
from agent_core.tool_result_contract import decode_result, encode_result
from agent_core.artifact_bundle_receiver import ArtifactBundleReceiver
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop


def run():
    with tempfile.TemporaryDirectory(prefix='p0-artifacts-') as temp:
        root = Path(temp); target = root/'test.txt'; target.write_text('TEST', encoding='utf-8')
        agent = WebAgentToolContext(root, interface_name='remote')
        agent.configure_web_file_capabilities(lambda:{'browser_upload':True,'browser_download':True})
        agent.current_request_id='REQ-P0'; agent.current_task_id='TASK-P0'
        context=SecurityContext.for_workspace(root)
        with patch('agent_core.tools.SecurityContext.from_agent', return_value=context):
            for tool in ('upload_file','return_artifact'):
                result = execute_tool({'tool':tool,'action_id':'QUEUE-'+tool,'path':str(target)}, agent)
                row = decode_result(result)
                assert row['status']=='QUEUED' and row['executed']
                assert row['request_id']=='REQ-P0' and row['action_id']=='QUEUE-'+tool
                assert row['data']['delivery_status'] in {'NOT_DELIVERED','NOT_CONFIRMED'}
                assert result.count('[RUNTIME_TOOL_RESULT]')==1
                assert not WebAgentProtocolLoop._classify_action_evidence({'tool':tool},result)['terminal_eligible']
            with patch('agent_core.tools.build_evidence_to_action_route', return_value={'active':False}):
                row=decode_result(execute_tool({'tool':'read_file','path':str(target)},agent,agent._models_registry))
                assert row['status']=='SUCCEEDED' and row['data']['content_read'] is True
                assert row['data']['text']=='TEST'
            result=execute_tool({'tool':'web_edit_file','path':str(target)},agent)
            assert decode_result(result)['status']=='REJECTED'
            missing=execute_tool({'tool':'return_artifact','path':str(root/'missing')},agent)
            assert decode_result(missing)['status']=='REJECTED'
            second=decode_result(execute_tool({'tool':'return_artifact','path':str(target)},agent))
            assert second['status']=='QUEUED' and second['data']['deduplicated']
            assert len(agent.take_outbound_artifacts())==1
            third=decode_result(execute_tool({'tool':'return_artifact','path':str(target)},agent))
            assert third['status']=='QUEUED' and len(agent._outbound_artifacts)==1
            agent.take_pending_attachments()
            requeued=decode_result(execute_tool({'tool':'upload_file','path':str(target)},agent))
            assert requeued['status']=='QUEUED' and requeued['data']['queued_count']==1
            agent.web_edit_file=lambda *a:'[WEB_DIRECT_EDIT_SUCCESS] unchanged'
            unchanged=decode_result(execute_tool({'tool':'web_edit_file','path':str(target)},agent))
            assert unchanged['status']=='FAILED' and not unchanged['data']['changed']
            def partial_edit(*a):
                target.write_text('PARTIAL_CHANGE',encoding='utf-8')
                raise RuntimeError('provider failed after write')
            agent.web_edit_file=partial_edit
            partial=decode_result(execute_tool({'tool':'web_edit_file','path':str(target)},agent))
            assert partial['status']=='PARTIAL' and partial['data']['changed']
            manager=SimpleNamespace(download_latest_artifact=lambda *a,**k:{'status':'ARTIFACT_DOWNLOAD_SUCCESS','path':str(target),'artifact_id':'IMG-P0','sha256':agent._file_sha256(target)})
            with patch('agent_core.web_runtime.get_manager', return_value=manager):
                result=execute_tool({'tool':'download_artifact','output_path':str(target),'action_id':'DOWNLOAD'},agent)
            row=decode_result(result)
            assert row['status']=='SUCCEEDED' and row['data']['file_saved']
            assert row['data']['artifact_id']=='IMG-P0' and row['request_id']=='REQ-P0'
            manager.download_latest_artifact=lambda *a,**k:{'status':'ARTIFACT_DOWNLOAD_SUCCESS','path':str(root/'never_saved')}
            with patch('agent_core.web_runtime.get_manager', return_value=manager):
                row=decode_result(execute_tool({'tool':'download_artifact','output_path':str(target)},agent))
            assert row['status']=='FAILED' and row['data']['file_saved'] is False
            other=root/'other.txt'; other.write_text('OTHER',encoding='utf-8')
            manager.download_latest_artifact=lambda *a,**k:{'status':'ARTIFACT_DOWNLOAD_SUCCESS','path':str(other)}
            agent.artifact_delivery_mode='telegram'
            before_count=len(agent._outbound_artifacts)
            with patch('agent_core.web_runtime.get_manager', return_value=manager):
                row=decode_result(execute_tool({'tool':'download_artifact','output_path':str(target)},agent))
            assert row['status']=='FAILED' and row['data']['artifact']['error']=='ARTIFACT_OUTPUT_PATH_MISMATCH'
            assert len(agent._outbound_artifacts)==before_count
        # Bundle must stop at an authoritative child rejection, not commit it.
        action={'tool':'read_file','action_id':'CHILD','path':str(root/'missing')}
        evidence={'bundle_id':'bundle-p0','zip_sha256':'hash','actions_sha256':'actions','action_count':1}
        manifest={'bundle_id':'bundle-p0','protocol_name':'web_agent_direct','protocol_version':9}
        receiver=ArtifactBundleReceiver(root/'bundle-state')
        with patch('agent_core.artifact_bundle_receiver.load_bundle', return_value=(manifest,[action],evidence)):
            result=receiver.execute('fixture.zip',validate_action=lambda a:(True, ''),
                execute_action=lambda a:encode_result('REJECTED','ACCESS_DENIED',tool='read_file'))
        payload=json.loads(result)
        assert payload['status']=='ARTIFACT_BUNDLE_FAILED' and not payload['failed_action_executed']
        assert receiver._load_state('bundle-p0')['committed']=={}
        with patch('agent_core.artifact_bundle_receiver.load_bundle', return_value=(manifest,[action],evidence)):
            payload=json.loads(receiver.execute('fixture.zip',validate_action=lambda a:(True,''),execute_action=lambda a:'ambiguous result'))
        assert payload['reason']=='CHILD_RESULT_UNKNOWN'
    print('P0_ARTIFACT_CONTRACT_PASS')


if __name__=='__main__': run()
