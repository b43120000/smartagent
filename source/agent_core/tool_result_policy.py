"""Producer-specific result policy, not a model/prose success classifier.

Business schemas remain unchanged. This module is pure and does not execute
tools, access a project, infer an action, or grant authorization.
"""
import json

CHUNK_TOOLS = {'begin_file_write','write_file_chunk','commit_file_write','abort_file_write'}
PROJECT_TOOLS = {'inspect_directory','inspect_project_scope','inspect_project_working_set',
    'inspect_semantic_map','update_semantic_map','update_semantic_map_file',
    'compare_project_snapshot','extract_project_dependencies','build_project_bundle',
    'build_project_delta','project_sync','query_project','inspect_project_ledger','query_project_history'}
PLAN_TOOLS = {'validate_edit_plan','apply_edit_plan','aggregate_verification',
    'propose_task_plan','repair_task_plan','propose_task_plan_file','execute_frozen_plan'}
P1_TOOLS = CHUNK_TOOLS | PROJECT_TOOLS | PLAN_TOOLS


def business_payload(result):
    text = str(result)
    for prefix in ('[CHUNKED_WRITE_SUCCESS]', '[CHUNKED_WRITE_FAILED]'):
        if text.startswith(prefix):
            text=text[len(prefix):].strip()
            break
    try: value=json.loads(text)
    except (ValueError,TypeError): return None
    return value if isinstance(value,dict) else None


def structured_outcome(tool, payload):
    if not isinstance(payload,dict):
        return 'UNKNOWN','INVALID_BUSINESS_RESULT',False,{}
    business=str(payload.get('status','')).upper()
    data={'business_status':business,'business_schema':payload.get('schema','')}
    status,reason,executed='UNKNOWN','UNRECOGNIZED_BUSINESS_RESULT',False
    if tool in CHUNK_TOOLS:
        if payload.get('execution_status')=='SUCCEEDED' and business=='SUCCESS':
            status,reason,executed='SUCCEEDED','WRITE_TRANSACTION_UPDATED',True
        elif payload.get('execution_status')=='FAILED':
            status,reason='REJECTED',str(payload.get('error_code','CHUNK_REJECTED'))
        committed = tool=='commit_file_write' and status=='SUCCEEDED'
        data.update(chunk_accepted=tool=='write_file_chunk' and status=='SUCCEEDED',
            file_committed=committed, write_id=payload.get('write_id',''),
            transaction_state=payload.get('state',''), terminal_eligible=committed or tool=='abort_file_write')
    elif tool=='inspect_directory':
        rows=payload.get('paths')
        if isinstance(rows,list) and rows:
            good=sum(bool(row.get('authorized') and row.get('exists')) for row in rows)
            errors=sum(bool(row.get('errors')) for row in rows)
            status='SUCCEEDED' if good==len(rows) and not errors else 'PARTIAL' if good else 'REJECTED' if any(not row.get('authorized') for row in rows) else 'FAILED'
            reason='DIRECTORY_INSPECTED' if status=='SUCCEEDED' else 'DIRECTORY_INSPECTION_INCOMPLETE'
            executed=good>0
    elif tool=='query_project':
        if business=='REJECTED': status,reason='REJECTED',str(payload.get('error','QUERY_REJECTED'))
        elif business=='READY' and isinstance(payload.get('results'),list):
            rows=payload['results']; good=sum(row.get('status') in {'OK','NOT_FOUND'} for row in rows)
            status='SUCCEEDED' if rows and good==len(rows) else 'PARTIAL' if good else 'FAILED'
            reason='PROJECT_QUERIED' if status=='SUCCEEDED' else 'PROJECT_QUERY_INCOMPLETE'; executed=bool(rows)
        data.update(snapshot_id=payload.get('snapshot_id',''),project_handle=payload.get('project_handle',''))
    elif tool=='project_sync':
        if business=='READY': status,reason,executed='SUCCEEDED','PROJECT_INDEX_OR_SYNC_READY',True
        elif business in {'LOCAL_READY','INCOMPLETE'}: status,reason,executed='PARTIAL','REMOTE_SYNC_NOT_COMPLETE',True
        elif business in {'REJECTED','DELTA_NOT_AVAILABLE'}: status,reason='REJECTED',business
        data.update(snapshot_id=payload.get('snapshot_id',''),sync_status=payload.get('sync_status',''),
            strategy=payload.get('strategy','INDEX_ONLY' if payload.get('schema')=='PROJECT_ACCESS_CAPSULE_V1' else ''),
            delivery_status='NOT_ATTEMPTED' if payload.get('attachments_uploaded')==0 else 'CONFIRMED' if payload.get('sync_status')=='PROJECT_SYNC_READY' else 'NOT_CONFIRMED')
    elif tool=='validate_edit_plan':
        if business in {'VALID','INVALID_PLAN','INVALID'}:
            status,reason,executed='SUCCEEDED','EDIT_PLAN_VALIDATION_COMPLETED',True
            data.update(validation_status=business, valid=business=='VALID', mutation_applied=False,terminal_eligible=business=='VALID')
    elif tool=='apply_edit_plan':
        if business=='APPLIED': status,reason,executed='SUCCEEDED','EDIT_PLAN_APPLIED',True
        elif business=='ROLLED_BACK': status,reason,executed='FAILED','EDIT_ROLLED_BACK',True
        elif business: status,reason='REJECTED','EDIT_PLAN_REJECTED'
        data.update(mutation_applied=business=='APPLIED',rolled_back=bool(payload.get('rolled_back')),
            verification_status='UNVERIFIED',terminal_eligible=False)
    elif tool in {'propose_task_plan','repair_task_plan','propose_task_plan_file'}:
        if business=='PLAN_FROZEN': status,reason,executed='SUCCEEDED','PLAN_FROZEN',True
        elif business: status,reason='REJECTED',business
        data.update(plan_id=payload.get('plan_id',''),plan_sha256=payload.get('plan_sha256',''),mutation_applied=False)
    elif tool=='aggregate_verification':
        rows=payload.get('results')
        started=(any(row.get('process_started',row.get('exit_code') is not None) is True for row in rows)
                 if isinstance(rows,list) and rows else False)
        uncertain=bool(isinstance(rows,list) and any('process_started' in row and row['process_started'] is None for row in rows))
        if uncertain:status,reason,executed='UNKNOWN','VERIFICATION_EXECUTION_UNCONFIRMED',started
        elif isinstance(rows,list) and rows and all(row.get('security_rejected') and row.get('process_started') is False for row in rows):
            status,reason,executed='REJECTED','SECURITY_COMMAND_REJECTED',False
        elif business=='PASS':status,reason,executed='SUCCEEDED','VERIFICATION_PASSED',started if isinstance(rows,list) else True
        elif business=='PARTIAL':status,reason,executed=('PARTIAL' if started else 'FAILED'),'VERIFICATION_PARTIALLY_REJECTED',started
        elif business in {'FAIL','FAILED'}:status,reason,executed='FAILED','VERIFICATION_OR_EXECUTION_FAILED',started if isinstance(rows,list) else True
        elif business:status,reason='REJECTED',business
        checks_completed=bool(isinstance(rows,list) and rows and all(
            row.get('process_started',row.get('exit_code') is not None) is True
            and row.get('exit_code') is not None and not row.get('timed_out')
            and not row.get('security_rejected') and not row.get('error') for row in rows))
        data.update(verification_status='PASS' if business=='PASS' else 'FAIL' if executed else 'UNKNOWN',
                    checks_completed=checks_completed,
                    execution_status='UNKNOWN' if uncertain else 'SUCCEEDED' if checks_completed else 'PARTIAL' if status=='PARTIAL' else 'FAILED',
                    possible_partial_execution=uncertain)
        if uncertain:data['verification_status']='UNKNOWN'
    elif tool=='execute_frozen_plan':
        if business=='PASS': status,reason,executed='SUCCEEDED','VERIFICATION_PASSED',True
        elif business in {'FAIL','FAILED','REPAIR_REQUIRED'}: status,reason,executed='FAILED','VERIFICATION_OR_EXECUTION_FAILED',True
        elif business: status,reason='REJECTED',business
        data.update(verification_status='PASS' if business=='PASS' else 'FAIL' if executed else 'UNKNOWN',
            rolled_back=bool(payload.get('rolled_back')))
    elif tool in {'update_semantic_map','update_semantic_map_file','build_project_delta','inspect_project_working_set'}:
        if business in {'UPDATED','READY'}: status,reason,executed='SUCCEEDED','PROJECT_STATE_UPDATED_OR_READ',True
        elif business in {'PARTIAL','INCOMPLETE'}: status,reason,executed='PARTIAL','PROJECT_STATE_INCOMPLETE',True
        elif business: status,reason='REJECTED',business
    elif tool=='inspect_project_scope' and payload.get('schema')=='PROJECT_SNAPSHOT_V1' and payload.get('snapshot_id'):
        status,reason,executed='SUCCEEDED','PROJECT_SNAPSHOT_READ',True
    elif tool=='inspect_semantic_map' and business in {'FRESH','STALE','MISSING'}:
        status,reason,executed='SUCCEEDED','SEMANTIC_STATE_READ',True
    elif tool=='compare_project_snapshot' and payload.get('freshness') in {'FRESH','CHANGED','UNKNOWN'}:
        status,reason,executed='SUCCEEDED','SNAPSHOT_COMPARISON_COMPLETED',True
    elif tool=='extract_project_dependencies' and payload.get('schema')=='DEPENDENCY_EVIDENCE_V1':
        status,reason,executed='SUCCEEDED','DEPENDENCY_EVIDENCE_READ',True
    elif tool=='build_project_bundle' and payload.get('schema')=='PROJECT_SOURCE_BUNDLE_V2':
        status,reason,executed=('SUCCEEDED' if payload.get('complete') else 'PARTIAL'),'LOCAL_BUNDLE_BUILT',True
        data['delivery_status']='NOT_ATTEMPTED'
    elif tool in {'inspect_project_ledger','query_project_history'} and business in {'READY','EMPTY'}:
        status,reason,executed='SUCCEEDED','PROJECT_LEDGER_READ',True
    elif tool=='query_project_history' and payload.get('schema')=='PROJECT_LEDGER_QUERY_RESULT_V1':
        status,reason,executed='SUCCEEDED','PROJECT_HISTORY_READ',True
    return status,reason,executed,data
