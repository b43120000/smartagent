"""T23-T25 persisted semantic-map contract probes."""
import copy,hashlib,json,os,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.project_sync import inspect_project_scope,save_project_snapshot
from agent_core.semantic_map import inspect_semantic_map,update_semantic_map,update_semantic_map_file,semantic_map_path,load_semantic_map
from agent_core.tools import execute_tool
from agent_core.tool_result_contract import decode_result
from agent_core.security_context import SecurityContext
from WebAgent.tool_context import WebAgentToolContext

def row(root,name):
    return dict(path=name,source_sha256=hashlib.sha256((root/name).read_bytes()).hexdigest(),
        responsibility='Defines constant '+name,public_symbols=[],dependencies=[],flows=[],invariants=[],tests=[])

def payload(root,names):
    return dict(base_snapshot_id=inspect_project_scope(root,verify_cached_content=True)['snapshot_id'],
        required_paths=names,project_summary='Small isolated source fixture',flows=[],files=[row(root,n) for n in names])

def run_t23():
    with tempfile.TemporaryDirectory(prefix='t23-semantic-') as tmp:
        root=Path(tmp);(root/'a.py').write_bytes(b'A = 1\n');(root/'b.py').write_bytes(b'B = 2\n')
        assert inspect_semantic_map(root)['status']=='MISSING'
        assert update_semantic_map(root,payload(root,['a.py','b.py']))['status']=='UPDATED'
        before=semantic_map_path(root).read_bytes()
        assert inspect_semantic_map(root,['a.py'])['status']=='FRESH'
        snap=inspect_project_scope(root,verify_cached_content=True);save_project_snapshot(snap)
        f=root/'a.py';stamp=f.stat();f.write_bytes(b'A = 9\n');os.utime(f,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        stale=inspect_semantic_map(root,['a.py'])
        assert stale['status']=='STALE' and stale['needs_analysis']==['a.py'],stale
        assert inspect_semantic_map(root,['b.py'])['status']=='FRESH'
        try:inspect_semantic_map(root,['b.py','missing.py'])
        except ValueError:pass
        else:raise AssertionError('Missing requested source silently ignored')
        assert semantic_map_path(root).read_bytes()==before
        ctx=WebAgentToolContext(root);ctx._authorized_local_paths=[str(root)]
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root.parent)):
            rejected=decode_result(execute_tool(dict(tool='inspect_semantic_map',workspace=str(root.parent)),ctx))
            assert rejected['status']=='REJECTED' and not rejected['executed'],rejected
        semantic_map_path(root).write_bytes(b'{broken')
        try:inspect_semantic_map(root)
        except ValueError:pass
        else:raise AssertionError('Corrupt map treated as MISSING')
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)):
            failed=decode_result(execute_tool(dict(tool='inspect_semantic_map',workspace=str(root)),ctx))
            assert failed['status']=='FAILED' and failed['executed'] and not failed['data']['possible_partial_execution'],failed
            with patch('agent_core.tools.inspect_semantic_map',side_effect=PermissionError('denied')):
                denied=decode_result(execute_tool(dict(tool='inspect_semantic_map',workspace=str(root)),ctx))
                assert denied['status']=='FAILED' and denied['executed'],denied
    print('T23_SEMANTIC_MATRIX_PASS')

def run_t24():
    with tempfile.TemporaryDirectory(prefix='t24-semantic-') as tmp:
        root=Path(tmp);(root/'a.py').write_bytes(b'A = 1\n');(root/'b.py').write_bytes(b'B = 2\n')
        full=payload(root,['a.py','b.py']);full.update(project_summary='Keep global summary',flows=['Keep global flow'])
        assert update_semantic_map(root,full)['status']=='UPDATED'
        before=semantic_map_path(root).read_bytes();p=payload(root,['a.py'])
        for bad in ({**p,'base_snapshot_id':'wrong'},
                    {**p,'files':[{**p['files'][0],'source_sha256':'0'*64}]},
                    {**p,'files':[{**p['files'][0],'path':'../outside.py'}]},
                    {**p,'files':[p['files'][0],p['files'][0]]}):
            r=update_semantic_map(root,bad);assert r['status'] in ('INVALID','SNAPSHOT_MISMATCH'),r
            assert semantic_map_path(root).read_bytes()==before
        p['files'][0]['responsibility']='New responsibility for a'
        assert update_semantic_map(root,p)['status']=='UPDATED'
        stored=load_semantic_map(root)
        assert stored['project_summary']=='Keep global summary' and stored['flows']==['Keep global flow'],stored
        assert {x['path'] for x in stored['files']}=={'a.py','b.py'},stored
        assert next(x for x in stored['files'] if x['path']=='b.py')==full['files'][1]
        ctx=WebAgentToolContext(root);ctx._authorized_local_paths=[str(root)]
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root.parent)):
            refused=decode_result(execute_tool(dict(tool='update_semantic_map',workspace=str(root.parent),patch=p),ctx))
            assert refused['status']=='REJECTED' and not refused['executed'],refused
        # A producer status alone must not create verified evidence.
        from agent_core.tool_result_policy import structured_outcome
        unverified=structured_outcome('update_semantic_map',{'status':'UPDATED'})
        assert not unverified[3]['terminal_eligible']
        import agent_core.semantic_map as module
        original=module._load; reads=[]
        def changed_readback(workspace):
            reads.append(1);saved=original(workspace)
            return {**saved,'project_summary':'TAMPERED_READBACK'} if len(reads)==3 else saved
        with patch.object(module,'_load',side_effect=changed_readback):
            failed=update_semantic_map(root,p)
            assert failed['status']=='PARTIAL' and failed['verification_status']=='FAIL',failed
            result=structured_outcome('update_semantic_map',failed)
            assert result[0]=='PARTIAL' and result[3]['verification_status']=='FAIL' and not result[3]['terminal_eligible']
            from agent_core.capability_recovery import build_result_evidence_to_action_route
            original=dict(tool='propose_task_plan',plan={})
            blocked=build_result_evidence_to_action_route(dict(tool='update_semantic_map',patch=p),json.dumps(failed),
                project_root_hint=str(root),prior_context={'resume_action':original})
            assert blocked['semantic_persistence_failed'] and not blocked['next_actions']
            still_blocked=build_result_evidence_to_action_route(dict(tool='inspect_semantic_map'),
                json.dumps({'status':'FRESH'}),project_root_hint=str(root),prior_context=blocked)
            assert still_blocked['semantic_persistence_failed'] and not still_blocked['next_actions']
    print('T24_SEMANTIC_MATRIX_PASS')

def run_t25():
    with tempfile.TemporaryDirectory(prefix='t25-semantic-') as tmp:
        root=Path(tmp);(root/'a.py').write_bytes(b'A = 1\n');(root/'b.py').write_bytes(b'B = 2\n')
        assert update_semantic_map(root,payload(root,['a.py','b.py']))['status']=='UPDATED'
        f=root/'.agents'/'patch.json';data=payload(root,['a.py']);data['files'][0]['responsibility']='Loaded from patch file'
        f.write_text(json.dumps(data),encoding='utf-8');digest=hashlib.sha256(f.read_bytes()).hexdigest()
        before=semantic_map_path(root).read_bytes()
        assert update_semantic_map_file(root,str(f),'0'*64)['reason']=='SHA256_MISMATCH'
        assert update_semantic_map_file(root,str(root/'missing.json'))['reason']=='PATCH_FILE_MISSING'
        assert update_semantic_map_file(root,str(root.parent/'outside.json'))['reason']=='PATH_OUTSIDE_WORKSPACE'
        assert semantic_map_path(root).read_bytes()==before
        changed=f.read_bytes()+b' ';f.write_bytes(changed)
        assert update_semantic_map_file(root,str(f),digest)['reason']=='SHA256_MISMATCH'
        f.write_text(json.dumps(data),encoding='utf-8')
        result=update_semantic_map_file(root,'.agents/patch.json',digest)
        assert result['status']=='UPDATED' and result['verification_status']=='PASS',result
        assert {x['path'] for x in load_semantic_map(root)['files']}=={'a.py','b.py'}
        f.write_bytes(b'{bad');assert update_semantic_map_file(root,str(f))['status']=='INVALID'
        with patch('agent_core.semantic_map.SEMANTIC_PATCH_FILE_MAX_BYTES',2):
            assert update_semantic_map_file(root,str(f))['reason']=='PATCH_FILE_TOO_LARGE'
        assert update_semantic_map_file(root,str(f)+':secret')['status']=='INVALID'
        # Root patch is admitted only with exact non-artifact snapshot proof.
        external=root/'semantic_patch.json';data=payload(root,['a.py'])
        external.write_text(json.dumps(data),encoding='utf-8')
        affected=update_semantic_map_file(root,str(external))
        assert affected['status']=='UPDATED' and affected['patch_snapshot_evidence']['original_base_snapshot_id']==data['base_snapshot_id'],affected
    print('T25_SEMANTIC_MATRIX_PASS')

if __name__=='__main__':
    import sys
    run_t25() if 'T25' in sys.argv else run_t24() if 'T24' in sys.argv else run_t23()
