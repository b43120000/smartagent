"""P1 native-schema preservation and real local transaction/query tests."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.tools import execute_tool
from agent_core.tool_result_contract import decode_result, attach_runtime_result
from agent_core.tool_result_policy import P1_TOOLS,structured_outcome
from agent_core.security_context import SecurityContext
from agent_core.progress_recovery import read_fact_digest
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.action_loop_state import build_action_loop_state


def run():
    with tempfile.TemporaryDirectory(prefix='p1-contract-') as temp:
        root=Path(temp); source=root/'input.txt'; source.write_text('P1_SOURCE',encoding='utf-8')
        (root/'long.txt').write_text('\n'.join('line' for _ in range(250)),encoding='utf-8')
        agent=WebAgentToolContext(root); agent.current_request_id='REQ-P1';agent.current_run_id='RUN-P1'
        context=SecurityContext.for_workspace(root)
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=context):
            target=root/'chunks.txt'
            rejected=decode_result(agent.execute({'tool':'query_project','action_id':'P1-BAD-QUERY',
                'project_root':str(root),'operation':'read_range','queries':[]}))
            assert rejected['status']=='REJECTED' and not rejected['executed']
            assert rejected['request_id']=='REQ-P1' and rejected['action_id']=='P1-BAD-QUERY'
            sequence=0
            def execute(tool,**fields):
                nonlocal sequence
                sequence+=1
                result=execute_tool({'tool':tool,'action_id':f'{tool}-{sequence}',**fields},agent)
                row=decode_result(result)
                assert row and row['tool']==tool and row['request_id']=='REQ-P1'
                return result,row
            result,row=execute('begin_file_write',write_id='P1-WRITE',path=str(target))
            assert row['status']=='SUCCEEDED' and not row['data']['file_committed'] and not target.exists()
            ev=WebAgentProtocolLoop._classify_action_evidence({'tool':'begin_file_write'},result)
            state=build_action_loop_state(action={'tool':'begin_file_write'},result=result,
                execution_status=ev['execution_status'],verification_status=ev['verification_status'])
            assert state['required_transition']=='CONTINUE_PLAN' and not ev['terminal_eligible']
            result,row=execute('write_file_chunk',write_id='P1-WRITE',content='P1_A')
            assert row['data']['chunk_accepted'] and not row['data']['file_committed'] and not target.exists()
            execute('write_file_chunk',write_id='P1-WRITE',content='P1_B')
            result,row=execute('commit_file_write',write_id='P1-WRITE')
            assert row['data']['file_committed'] and target.read_text()=='P1_AP1_B',result
            # Prefix consumers still see original business status/state.
            business=json.loads(result.split('] ',1)[1])
            assert business['status']=='success' and business['state']=='COMMITTED'
            execute('begin_file_write',write_id='P1-ABORT',path=str(root/'never.txt'))
            result,row=execute('abort_file_write',write_id='P1-ABORT')
            assert row['status']=='SUCCEEDED' and not (root/'never.txt').exists()
            result,row=execute('project_sync',project_root=str(root),strategy='INDEX_ONLY')
            capsule=json.loads(result); snapshot=capsule['snapshot_id']
            assert row['status']=='SUCCEEDED' and capsule['status']=='READY' and snapshot
            result,row=execute('query_project',project_root=str(root),snapshot_id=snapshot,
                queries=[{'operation':'read_range','path':'input.txt','start_line':1,'end_line':120}])
            query=json.loads(result)
            assert row['status']=='SUCCEEDED' and query['results'][0]['content']=='1: P1_SOURCE',result
            assert query['snapshot_id'] and query['_runtime_result']['action_id'].startswith('query_project-')
            assert not query['results'][0]['truncated'] and query['results'][0]['next_cursor'] is None
            result,row=execute('query_project',project_root=str(root),snapshot_id=snapshot,
                queries=[{'operation':'read_range','path':'long.txt','start_line':1,'end_line':500}])
            page=json.loads(result)['results'][0]
            assert page['truncated'] and page['next_cursor']==241
            result,row=execute('query_project',project_root=str(root),snapshot_id=snapshot,
                queries=[{'operation':'read_range','path':'long.txt','start_line':241,'end_line':500}])
            page=json.loads(result)['results'][0]
            assert not page['truncated'] and page['next_cursor'] is None
            result,row=execute('validate_edit_plan',workspace=str(root),plan={})
            assert row['data']['validation_status']=='INVALID_PLAN' and not row['data']['mutation_applied']
            assert json.loads(result)['missing_fields']
            result,row=execute('apply_edit_plan',workspace=str(root),plan={})
            assert row['status']=='REJECTED' and not row['executed']
            execute('inspect_semantic_map',workspace=str(root))
            execute('inspect_project_ledger',workspace=str(root))
            result,row=execute('query_project_history',workspace=str(root))
            assert row['status']=='SUCCEEDED'
        samples={
            'inspect_project_scope':{'schema':'PROJECT_SNAPSHOT_V1','snapshot_id':'S'},
            'inspect_project_working_set':{'status':'READY'},'inspect_semantic_map':{'status':'MISSING'},
            'update_semantic_map':{'status':'UPDATED'},'update_semantic_map_file':{'status':'PARTIAL'},
            'compare_project_snapshot':{'freshness':'CHANGED'},
            'extract_project_dependencies':{'schema':'DEPENDENCY_EVIDENCE_V1'},
            'build_project_bundle':{'schema':'PROJECT_SOURCE_BUNDLE_V2','complete':True},
            'build_project_delta':{'status':'DELTA_NOT_AVAILABLE'},
            'propose_task_plan':{'status':'PLAN_FROZEN','plan_id':'P'},
            'repair_task_plan':{'status':'INVALID_PLAN'},'propose_task_plan_file':{'status':'PLAN_FROZEN','plan_id':'P'},
            'execute_frozen_plan':{'status':'PASS'},'aggregate_verification':{'status':'FAIL'},
            'inspect_directory':{'paths':[{'authorized':True,'exists':True,'errors':[]}]},
        }
        tested={'begin_file_write','write_file_chunk','commit_file_write','abort_file_write','project_sync',
            'query_project','validate_edit_plan','apply_edit_plan','inspect_semantic_map','inspect_project_ledger','query_project_history'}
        for tool,payload in samples.items():
            status,reason,executed,data=structured_outcome(tool,payload)
            assert status!='UNKNOWN',tool
            wrapped=attach_runtime_result(json.dumps(payload),status,reason,tool=tool,executed=executed,data=data)
            assert all(json.loads(wrapped)[key]==value for key,value in payload.items())
            assert decode_result(wrapped)['status']==status
            tested.add(tool)
        assert tested==P1_TOOLS,(P1_TOOLS-tested)
        a=attach_runtime_result(json.dumps(samples['inspect_directory']),'SUCCEEDED','READ',tool='inspect_directory',executed=True,action_id='A')
        b=attach_runtime_result(json.dumps(samples['inspect_directory']),'SUCCEEDED','READ',tool='inspect_directory',executed=True,action_id='B')
        assert read_fact_digest({'tool':'inspect_directory'},a)==read_fact_digest({'tool':'inspect_directory'},b)
    print('P1_RESULT_CONTRACT_PASS tools='+str(len(tested)))


if __name__=='__main__':run()
