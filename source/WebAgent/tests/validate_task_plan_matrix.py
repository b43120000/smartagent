"""T26/T28 persisted-plan contract probes; no real project mutations."""
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from agent_core.task_plan import freeze_task_plan, repair_task_plan, _hash, _plan_hash_basis
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_edit_plan_matrix import plan_for


def task_plan(root):
    return dict(schema='TASK_PLAN_V1', base_snapshot_id=plan_for(root)['base_snapshot_id'],
        semantic_map_revision='', goal='Fixture BEFORE to AFTER', affected_flows=['fixture'],
        files_to_read=['probe.txt'], edit_plan=plan_for(root),
        verification_commands=['findstr /X /C:AFTER probe.txt'],
        acceptance_criteria=['probe.txt equals AFTER'], rollback_condition='verification not PASS',
        post_change_semantic=[])


def run():
    issues=[]
    with tempfile.TemporaryDirectory(prefix='t26-plan-') as value:
        parent=Path(value).resolve(); root=parent/'project';root.mkdir()
        p=root/'probe.txt';p.write_bytes(b'BEFORE\n');plan=task_plan(root)
        ctx=WebAgentToolContext(parent, interface_name='remote')
        ctx.begin_run('T26','Only freeze this project\n'+str(root),[str(root)])
        def probe(name, fn):
            try:fn()
            except Exception as exc:issues.append((name,type(exc).__name__,str(exc)[:600]))
        def freeze():
            raw=ctx.execute(dict(tool='propose_task_plan',workspace=str(root),plan=plan))
            result=json.loads(raw);assert result['status']=='PLAN_FROZEN',result
            stored=json.loads(Path(result['path']).read_text(encoding='utf-8'))
            assert stored['plan_sha256']==_hash(_plan_hash_basis(stored))
            assert stored['plan_id']=='PLAN-'+stored['plan_sha256'][:20].upper()
            assert p.read_bytes()==b'BEFORE\n'
            return result
        probe('freeze_hash_and_no_mutation',freeze)
        def parent_scope():
            raw=ctx.execute(dict(tool='propose_task_plan',workspace=str(parent),plan=plan))
            assert 'workspace_scope:' in raw and 'REJECTED' in raw,raw
        probe('parent_scope',parent_scope)
        for field in ('base_snapshot_id','base_sha256','old'):
            def invalid_source(field=field):
                candidate=copy.deepcopy(plan)
                if field=='base_snapshot_id':candidate[field]='0'*64
                else:candidate['edit_plan']['files_to_modify'][0][field]='ABSENT'
                result=freeze_task_plan(root,candidate)
                assert result['status']!='PLAN_FROZEN',result
                assert p.read_bytes()==b'BEFORE\n'
            probe('invalid_source_'+field,invalid_source)
        for bad in (3,['x'],True):
            def malformed(bad=bad):
                result=freeze_task_plan(root,bad)
                assert result['status']=='INVALID_PLAN',result
            probe('malformed_plan_'+repr(bad),malformed)
        def malformed_edit():
            result=freeze_task_plan(root,{**plan,'edit_plan':3})
            assert result['status']=='INVALID_PLAN',result
        probe('malformed_edit',malformed_edit)
        def malformed_alias():
            result=repair_task_plan(root,{**plan,'schema':[]})
            assert result['status']=='INVALID_PLAN',result
        probe('malformed_schema',malformed_alias)
        def temp_hardlink():
            candidate={**plan,'goal':'new hardlink safety fixture'}
            checked=__import__('agent_core.task_plan',fromlist=['validate_task_plan']).validate_task_plan(root,candidate)
            plan_id='PLAN-'+_hash(_plan_hash_basis(checked['plan']))[:20].upper()
            outside=parent/'outside.txt';outside.write_bytes(b'KEEP_PARENT')
            predictable=root/'.agents'/'task_plans'/(plan_id+'.json.tmp')
            os.link(outside,predictable)
            result=freeze_task_plan(root,candidate)
            assert result['status']=='PLAN_FROZEN',result
            assert outside.read_bytes()==b'KEEP_PARENT'
        probe('predictable_temp_hardlink_not_followed',temp_hardlink)
        def forged_scope():
            ctx.current_task_epoch='trusted-epoch';ctx.current_intent_digest='trusted-digest'
            ctx.current_task_id='trusted-task';ctx._protocol_turn_seq=1
            supplied=dict(request_id='forged',task_id='forged',task_epoch='forged',
                intent_digest='forged',request_phase='PLANNER_ROUND_PENDING',continuation_seq=1)
            raw=ctx.execute(dict(tool='propose_task_plan',workspace=str(root),plan={**plan,'request_scope':supplied}))
            assert json.loads(raw)['status']=='PLAN_SCOPE_MISMATCH',raw
        probe('forged_scope_rejected',forged_scope)
        def tamper_replay():
            result=freeze_task_plan(root,plan);path=Path(result['path'])
            stored=json.loads(path.read_text(encoding='utf-8'));stored['goal']='TAMPERED'
            path.write_text(json.dumps(stored),encoding='utf-8')
            replay=freeze_task_plan(root,plan)
            assert replay['status']!='PLAN_FROZEN',replay
        probe('tamper_replay',tamper_replay)
        assert p.read_bytes()==b'BEFORE\n'
    print(json.dumps(issues,ensure_ascii=False));assert not issues,'T26 failures'
    print('T26_TASK_PLAN_MATRIX_PASS')


if __name__=='__main__':run()
