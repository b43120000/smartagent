"""T31 exact writes and truthful rollback, using isolated owned files only."""
import copy,hashlib,json,os,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.project_access import build_project_capsule
from agent_core.tool_result_contract import decode_result
from agent_core.path_security import PathSecurityError
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_edit_plan_matrix import plan_for


def run():
    with tempfile.TemporaryDirectory(prefix='t31-apply-') as value:
        parent=Path(value).resolve();issues=[]
        def setup(name):
            root=parent/name;root.mkdir();(root/'probe.txt').write_bytes(b'BEFORE\n')
            (root/'sibling.txt').write_bytes(b'KEEP_SIBLING')
            ctx=WebAgentToolContext(parent,interface_name='remote');ctx.begin_run('T31','只改此專案\n'+str(root),[str(root)])
            return root,ctx,plan_for(root)
        def call(ctx,root,plan):return ctx.execute({'tool':'apply_edit_plan','workspace':str(root),'plan':plan})
        def require(name,good,raw):
            if not good:issues.append({'case':name,'actual':str(raw)[:1300]})
        root,ctx,plan=setup('whole')
        plan['files_to_modify']=[{'path':'probe.txt','mode':'whole_file','content':'AFTER\n',
            'base_sha256':hashlib.sha256(b'BEFORE\n').hexdigest(),'post_sha256':hashlib.sha256(b'AFTER\n').hexdigest()}]
        raw=call(ctx,root,plan);p=json.loads(raw)
        require('whole_exact_bytes',p['status']=='APPLIED' and (root/'probe.txt').read_bytes()==b'AFTER\n',raw)
        assert (root/'sibling.txt').read_bytes()==b'KEEP_SIBLING'
        root,ctx,plan=setup('replace');raw=call(ctx,root,plan)
        require('replace_exact_bytes',json.loads(raw)['status']=='APPLIED' and (root/'probe.txt').read_bytes()==b'AFTER\n',raw)
        root,ctx,plan=setup('hardlink');outside=parent/'outside.txt';outside.write_bytes(b'BEFORE\n')
        (root/'probe.txt').unlink();os.link(outside,root/'probe.txt')
        raw=call(ctx,root,plan)
        require('hardlink_no_outside_write',outside.read_bytes()==b'BEFORE\n',raw)
        root,ctx,plan=setup('rollback');(root/'second.txt').write_bytes(b'SECOND')
        plan['base_snapshot_id']=build_project_capsule(root)['snapshot_id']
        plan['files_to_modify']=[{'path':'newdir/new.txt','create':True,'mode':'whole_file','content':'NEW'},
            {'path':'probe.txt','mode':'whole_file','content':'AFTER'},
            {'path':'second.txt','mode':'whole_file','content':'BROKEN','post_sha256':'0'*64}]
        raw=call(ctx,root,plan);p=json.loads(raw)
        require('rollback_files_and_directory',p['status']=='ROLLED_BACK' and p['rolled_back'] is True
            and (root/'probe.txt').read_bytes()==b'BEFORE\n' and (root/'second.txt').read_bytes()==b'SECOND'
            and not (root/'newdir').exists(),raw)
        # Fault only during restoration after the postcondition fault.
        root,ctx,plan=setup('rollback_failure')
        plan['files_to_modify']=[{'path':'probe.txt','mode':'whole_file','content':'AFTER','post_sha256':'0'*64}]
        from agent_core.batch_apply import _write_atomic
        def failed_restore(boundary,path,data,**kwargs):
            if path==root/'probe.txt' and data==b'BEFORE\n':raise PermissionError('T31_RESTORE_DENIED')
            return _write_atomic(boundary,path,data,**kwargs)
        with patch('agent_core.batch_apply._write_atomic',failed_restore):raw=call(ctx,root,plan)
        typed=decode_result(raw) or {}
        require('truthful_incomplete_rollback',typed.get('executed') is True and typed.get('status')=='UNKNOWN'
            and typed.get('data',{}).get('possible_partial_execution') is True,raw)
        root,ctx,plan=setup('create_race')
        plan['files_to_modify']=[{'path':'new.txt','create':True,'mode':'whole_file','content':'OUR_CONTENT'}]
        def concurrent_create(boundary,path,data,**kwargs):
            path.write_bytes(b'OTHER_WRITER')
            return _write_atomic(boundary,path,data,**kwargs)
        with patch('agent_core.batch_apply._write_atomic',concurrent_create):raw=call(ctx,root,plan)
        require('create_does_not_overwrite_or_rollback_other_writer',(root/'new.txt').read_bytes()==b'OTHER_WRITER'
            and json.loads(raw)['status']=='ROLLED_BACK',raw)
        root,ctx,plan=setup('existing_race')
        def concurrent_update(boundary,path,data,**kwargs):
            path.write_bytes(b'OTHER_UPDATE')
            return _write_atomic(boundary,path,data,**kwargs)
        with patch('agent_core.batch_apply._write_atomic',concurrent_update):raw=call(ctx,root,plan)
        require('changed_target_not_blindly_restored',(root/'probe.txt').read_bytes()==b'OTHER_UPDATE'
            and json.loads(raw)['status']=='ROLLED_BACK',raw)
        root,ctx,plan=setup('directory_guard_failure')
        plan['files_to_modify']=[{'path':'newdir/new.txt','create':True,'mode':'whole_file','content':'X','post_sha256':'0'*64}]
        from agent_core.batch_apply import reject_reparse_components
        def blocked_cleanup(path,boundary):
            if path==root/'newdir':raise PathSecurityError('T31_DIRECTORY_CHANGED',str(path))
            return reject_reparse_components(path,boundary)
        with patch('agent_core.batch_apply.reject_reparse_components',blocked_cleanup):raw=call(ctx,root,plan)
        typed=decode_result(raw)
        require('cleanup_security_failure_is_not_unexecuted',typed['status']=='UNKNOWN' and typed['executed'] is True
            and typed['data']['possible_partial_execution'] is True,raw)
        root,ctx,plan=setup('crlf');(root/'probe.txt').write_bytes(b'BEFORE\r\n')
        plan=plan_for(root);plan['files_to_modify'][0].update(old='BEFORE\r\n',new='AFTER\r\n')
        raw=call(ctx,root,plan)
        require('literal_crlf_replacement',json.loads(raw)['status']=='APPLIED' and (root/'probe.txt').read_bytes()==b'AFTER\r\n',raw)
        print(json.dumps(issues,ensure_ascii=False));assert not issues,'T31 matrix failures'
    print('T31_APPLY_MATRIX_PASS exact_bytes_atomic_links_rollback_truth')


if __name__=='__main__':run()
