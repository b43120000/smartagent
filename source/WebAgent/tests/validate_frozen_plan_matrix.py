"""T29 execute / no replay / cross-round ownership / rollback probes."""
import json,tempfile,hashlib
from pathlib import Path
from unittest.mock import patch
from agent_core import task_plan as module
from agent_core.tool_result_policy import structured_outcome
from WebAgent.tests.validate_task_plan_matrix import task_plan

def scope(seq=1):
    return dict(request_id='T29-R',task_id='T29-T',task_epoch='T29-E',intent_digest='T29-D',
        request_phase='PLANNER_ROUND_PENDING',continuation_seq=seq)

def fixture(parent,name):
    root=parent/name;root.mkdir();(root/'probe.txt').write_bytes(b'BEFORE\n')
    plan=task_plan(root)
    plan['verification_commands']=["powershell.exe -NoProfile -NonInteractive -Command \"exit ([int]((Get-Content -LiteralPath 'probe.txt' -Raw) -ne ('AFTER'+[char]10)))\""]
    frozen=module.freeze_task_plan(root,plan,request_scope=scope())
    assert frozen['status']=='PLAN_FROZEN',frozen
    return root,frozen

def run():
    issues=[]
    with tempfile.TemporaryDirectory(prefix='t29-frozen-') as value:
        parent=Path(value)
        def probe(name,fn):
            try:fn()
            except Exception as exc:issues.append((name,type(exc).__name__,str(exc)[:900]))
        def success():
            root,f=fixture(parent,'success')
            r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['status']=='PASS',r
            assert (root/'probe.txt').read_bytes()==b'AFTER\n'
            assert r['verification']['status']=='PASS'
            assert json.loads(Path(f['path']).read_text(encoding='utf-8'))['state']=='COMPLETED'
            again=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(3))
            assert again['status']=='PLAN_NOT_EXECUTABLE',again
        probe('cross_round_execute_and_no_replay',success)
        def foreign():
            root,f=fixture(parent,'foreign')
            for fields in ({'request_id':'foreign'},{'task_epoch':'foreign'},{'intent_digest':'foreign'},{'task_id':'foreign'}):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope={**scope(),**fields})
                assert r['status']=='PLAN_NOT_EXECUTABLE',r
                assert (root/'probe.txt').read_bytes()==b'BEFORE\n'
        probe('foreign_identity_rejected',foreign)
        def verification_failure():
            root,f=fixture(parent,'verifyfail')
            with patch.object(module,'run_aggregated_verification',return_value={'status':'FAIL','results':[]}):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope())
            assert r['status']=='REPAIR_REQUIRED' and r['rolled_back'] is True,r
            assert (root/'probe.txt').read_bytes()==b'BEFORE\n'
        probe('verification_fail_rolls_back',verification_failure)
        def verification_exception():
            root,f=fixture(parent,'verifyraise')
            with patch.object(module,'run_aggregated_verification',side_effect=RuntimeError('INJECTED_VERIFIER_EXCEPTION')):
                try:r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope())
                finally:assert (root/'probe.txt').read_bytes()==b'BEFORE\n','verifier exception left applied data'
            assert r['status']=='REPAIR_REQUIRED' and r['rolled_back'] is True,r
        probe('verification_exception_rolls_back',verification_exception)
        def canonical():
            root=parent/'canonical';root.mkdir();(root/'probe.txt').write_bytes(b'BEFORE\n');p=task_plan(root)
            p['edit_plan']['files_to_modify'][0]['path']=' probe.txt'
            f=module.freeze_task_plan(root,p,request_scope=scope());assert f['status']=='PLAN_FROZEN',f
            with patch.object(module,'run_aggregated_verification',return_value={'status':'FAIL','results':[]}):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope())
            assert r['rolled_back'] is True,r
            assert (root/'probe.txt').read_bytes()==b'BEFORE\n','rollback used noncanonical target, original left AFTER'
            assert not (root/' probe.txt').exists(),'rollback created wrong target'
        probe('canonical_path_used_for_rollback',canonical)
        def same_round_success():
            root,f=fixture(parent,'same_round')
            r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope())
            assert r['status']=='PASS',r
            assert (root/'probe.txt').read_bytes()==b'AFTER\n'
        probe('same_scope_execution_and_verification',same_round_success)
        def backward_round():
            root=parent/'backward';root.mkdir();(root/'probe.txt').write_bytes(b'BEFORE\n')
            f=module.freeze_task_plan(root,task_plan(root),request_scope=scope(3))
            r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['status']=='PLAN_NOT_EXECUTABLE' and (root/'probe.txt').read_bytes()==b'BEFORE\n',r
        probe('backward_round_rejected',backward_round)
        def denied():
            root,f=fixture(parent,'denied')
            def authorize(target):
                if target==root/'probe.txt':raise PermissionError('INJECTED_READ_ONLY')
            r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2),authorize_target=authorize)
            assert r['status']=='PLAN_NOT_EXECUTABLE' and r['executed'] is False,r
            assert (root/'probe.txt').read_bytes()==b'BEFORE\n'
        probe('denied_target_preflight_no_execution',denied)
        def restore_denied():
            root,f=fixture(parent,'restoredenied')
            with patch.object(module,'run_aggregated_verification',return_value={'status':'FAIL'}), \
                    patch.object(module,'_write_atomic',side_effect=PermissionError('INJECTED_RESTORE_DENIED')):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['status']=='ROLLBACK_INCOMPLETE' and r['rolled_back'] is False,r
            status,reason,executed,data=structured_outcome('execute_frozen_plan',r)
            assert (status,reason,executed)==('UNKNOWN','ROLLBACK_INCOMPLETE',True)
            assert data['verification_status']=='UNKNOWN' and data['rollback_errors']
            assert (root/'probe.txt').read_bytes()==b'AFTER\n'
        probe('rollback_failure_truthful_unknown',restore_denied)
        def changed_by_other():
            root,f=fixture(parent,'conflict')
            def verify(*args,**kwargs):
                (root/'probe.txt').write_bytes(b'KEEP_OTHER_CHANGE');return {'status':'FAIL'}
            with patch.object(module,'run_aggregated_verification',side_effect=verify):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['status']=='ROLLBACK_INCOMPLETE' and r['rolled_back'] is False,r
            assert (root/'probe.txt').read_bytes()==b'KEEP_OTHER_CHANGE'
        probe('rollback_does_not_overwrite_unknown_change',changed_by_other)
        def create_cleanup():
            root=parent/'created';root.mkdir();(root/'probe.txt').write_bytes(b'BEFORE\n');p=task_plan(root)
            p['edit_plan']['files_to_modify'].append(dict(path='new/sub/new.txt',create=True,mode='whole_file',content='NEW'))
            f=module.freeze_task_plan(root,p,request_scope=scope());assert f['status']=='PLAN_FROZEN',f
            with patch.object(module,'run_aggregated_verification',side_effect=RuntimeError('INJECTED_EXCEPTION')):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['rolled_back'] is True,r
            assert (root/'probe.txt').read_bytes()==b'BEFORE\n' and not (root/'new').exists()
        probe('created_file_and_empty_directories_rollback',create_cleanup)
        def receipt_failure():
            root,f=fixture(parent,'receiptfail');original_write=module._write_atomic
            def write(root_,target,data,**kwargs):
                if target==Path(f['path']):raise PermissionError('INJECTED_RECEIPT_DENIAL')
                return original_write(root_,target,data,**kwargs)
            with patch.object(module,'_write_atomic',side_effect=write):
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['rolled_back'] is True and r['status']=='REPAIR_REQUIRED',r
            assert (root/'probe.txt').read_bytes()==b'BEFORE\n'
            assert json.loads(Path(f['path']).read_text(encoding='utf-8'))['state']=='FROZEN'
        probe('completion_receipt_failure_rolls_back',receipt_failure)
        def semantic_case(name):
            root=parent/name;root.mkdir();(root/'probe.txt').write_bytes(b'BEFORE\n')
            source=root/'a.py';source.write_bytes(b"VALUE='BEFORE'\n")
            snapshot=module.inspect_project_scope(root)
            row=dict(path='a.py',source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                responsibility='Stores a test fixture value.',public_symbols=['VALUE'],dependencies=[],flows=[],invariants=[],tests=[])
            updated=module.update_semantic_map(root,dict(base_snapshot_id=snapshot['snapshot_id'],required_paths=['a.py'],
                project_summary='Isolated T29 fixture.',flows=[],files=[row]))
            assert updated['status']=='UPDATED',updated
            p=task_plan(root);p['semantic_map_revision']=updated['semantic_map_revision'];p['files_to_read']=['a.py']
            p['edit_plan']['files_to_modify']=[dict(path='a.py',base_sha256=row['source_sha256'],mode='exact_replace',old='BEFORE',new='AFTER')]
            p['post_change_semantic']=[{**row,'responsibility':'Stores the updated fixture value.'}]
            p['verification_commands']=['powershell.exe -NoProfile -NonInteractive -Command "exit 0"']
            f=module.freeze_task_plan(root,p,request_scope=scope());assert f['status']=='PLAN_FROZEN',f
            return root,f
        def semantic_success():
            root,f=semantic_case('semantic_success')
            r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert r['status']=='PASS' and r['semantic_update']['status']=='UPDATED',r
            records=module.load_semantic_map(root)['files']
            assert records[0]['source_sha256']==hashlib.sha256((root/'a.py').read_bytes()).hexdigest()
        probe('source_semantic_success',semantic_success)
        def lexical_temp():
            root,f=semantic_case('semantic_temp')
            target=module.semantic_map_path(root);temp=target.with_name(target.name+'.tmp');temp.write_bytes(b'KEEP_TEMP')
            original_exists=Path.exists
            def exists(path):return False if path==temp else original_exists(path)
            with patch.object(Path,'exists',exists),patch.object(module,'update_semantic_map') as update:
                r=module.execute_frozen_task_plan(root,f['plan_id'],request_scope=scope(2))
            assert update.call_count==0 and r['rolled_back'] is True,r
            assert (root/'a.py').read_bytes()==b"VALUE='BEFORE'\n" and temp.read_bytes()==b'KEEP_TEMP'
        probe('lexical_temp_rejected_even_when_exists_false',lexical_temp)
    print(json.dumps(issues,ensure_ascii=False));assert not issues,'T29 failures'
    print('T29_FROZEN_PLAN_MATRIX_PASS')

if __name__=='__main__':run()
