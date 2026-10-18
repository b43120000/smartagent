"""T30 validation is a truthful, request-scoped, non-mutating observation."""
import copy,hashlib,json,os,tempfile
from pathlib import Path
from agent_core.project_access import build_project_capsule
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext


def plan_for(root):
    return {'base_snapshot_id':build_project_capsule(root)['snapshot_id'],
        'files_to_modify':[{'path':'probe.txt','base_sha256':hashlib.sha256((root/'probe.txt').read_bytes()).hexdigest(),
            'mode':'exact_replace','old':'BEFORE','new':'AFTER','modification_intent':'fixture replacement'}],
        'verification_commands':[],'expected_observable_result':'probe contains AFTER',
        'rollback_condition':'any write or postcondition failure'}


def run():
    with tempfile.TemporaryDirectory(prefix='t30-validation-') as value:
        parent=Path(value).resolve();root=parent/'project';root.mkdir();p=root/'probe.txt';p.write_bytes(b'BEFORE\n')
        ctx=WebAgentToolContext(parent,interface_name='remote');ctx.begin_run('T30','只驗證此專案，不改檔\n'+str(root),[str(root)])
        plan=plan_for(root);issues=[]
        def case(name,expected='VALID',workspace=None,**fields):
            candidate=copy.deepcopy(plan)
            for key,v in fields.items():candidate[key]=v
            before=p.read_bytes()
            raw=ctx.execute({'tool':'validate_edit_plan','workspace':str(workspace or root),'plan':candidate})
            assert p.read_bytes()==before,'validation changed source'
            typed=decode_result(raw) or {}
            if expected=='SCOPE_REJECTED':good=typed.get('status')=='REJECTED' and 'workspace_scope:' in raw
            else:
                payload=json.loads(raw) if raw.lstrip().startswith('{') else {}
                good=payload.get('status')==expected and typed.get('status')=='SUCCEEDED' and typed.get('executed') is True
                good=good and typed.get('data',{}).get('mutation_applied') is False
            if not good:issues.append({'case':name,'expected':expected,'actual':raw[:1500]})
        case('valid')
        spaced=copy.deepcopy(plan);spaced['files_to_modify'][0]['path']=' probe.txt'
        raw=ctx.execute({'tool':'validate_edit_plan','workspace':str(root),'plan':spaced})
        assert json.loads(raw)['files_to_modify'][0]['path']=='probe.txt',raw
        bad=copy.deepcopy(plan['files_to_modify']);bad[0]['old']='ABSENT'
        case('wrong_before','PRECONDITION_FAILED',files_to_modify=bad)
        case('parent_scope','SCOPE_REJECTED',workspace=parent)
        bad=copy.deepcopy(plan['files_to_modify']);bad[0]['base_sha256']='0'*64
        case('wrong_sha','BASE_HASH_MISMATCH',files_to_modify=bad)
        case('stale_snapshot','PLAN_BASE_MISMATCH',base_snapshot_id='0'*64)
        for path in ('../outside.txt','new.txt:stream','/probe.txt'):
            case('unsafe_'+path,'INVALID_PATH',files_to_modify=[{'path':path,'create':True,'content':'X','mode':'whole_file'}])
        case('duplicate','INVALID_PLAN',files_to_modify=plan['files_to_modify']*2)
        for encoding in ('unknown-codec',123,'hex'):
            bad=copy.deepcopy(plan['files_to_modify']);bad[0]['encoding']=encoding
            case('invalid_codec_'+str(encoding),'INVALID_PLAN',files_to_modify=bad)
        old=p.stat();p.write_bytes(b'CHANGED');os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
        case('fresh_content_not_cached','PLAN_BASE_MISMATCH')
        assert not ctx.take_pending_attachments()
        print(json.dumps(issues,ensure_ascii=False))
        assert not issues,'T30 validation matrix failures'
    print('T30_EDIT_PLAN_MATRIX_PASS no_mutation_before_sha_snapshot_scope')


if __name__=='__main__':run()
