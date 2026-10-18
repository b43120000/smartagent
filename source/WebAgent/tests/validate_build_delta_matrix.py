"""T19 actual delta bytes, tombstones, empty result, and request boundaries."""
import hashlib,json,os,subprocess,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.project_access import build_project_capsule
from agent_core.project_bundle import reconstruct_bundle
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext


def check(raw,root,baseline,changed=True):
    assert raw.lstrip().startswith('{'),raw
    p=json.loads(raw);typed=decode_result(raw)
    assert typed['status']=='SUCCEEDED' and typed['executed'] is True,p
    assert p['base_snapshot_id']==baseline and p['status']=='READY',p
    assert typed['data']['delivery_status']=='NOT_ATTEMPTED',typed
    assert typed['data']['verification_status']=='PASS',typed
    expected=['a.py','added.md','gone.md'] if changed else []
    assert p['delta']['changed_paths']==expected,p
    assert p['tombstones']==([{'path':'gone.md','deleted':True}] if changed else []),p
    contents={}
    for b in p['bundle']['bundles']:
        path=Path(b['path']);assert path.resolve().is_relative_to(root.resolve())
        assert path.stat().st_size==b['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==b['sha256']
        contents.update(reconstruct_bundle(path))
    assert contents==({name:(root/name).read_bytes() for name in ('a.py','added.md')} if changed else {}),contents
    return p


def run():
    with tempfile.TemporaryDirectory(prefix='t19-delta-') as value:
        parent=Path(value).resolve();root=parent/'project';root.mkdir()
        a=root/'a.py';a.write_bytes(b'AA0\n');(root/'gone.md').write_bytes(b'gone\n')
        baseline=build_project_capsule(root)['snapshot_id']
        ctx=WebAgentToolContext(parent,interface_name='remote');ctx.begin_run('T19','只建此專案delta\n'+str(root),[str(root)])
        def call(**fields):return ctx.execute({'tool':'build_project_delta','workspace':str(root),'base_snapshot_id':baseline,**fields})
        # Parent workspace must be rejected before any inventory write.
        expanded=call(workspace=str(parent))
        assert decode_result(expanded)['status']=='REJECTED' and 'workspace_scope:' in expanded and not (parent/'.agents').exists(),expanded
        check(call(),root,baseline,False)
        old=a.stat();a.write_bytes(b'AA1\n');os.utime(a,ns=(old.st_atime_ns,old.st_mtime_ns))
        (root/'added.md').write_bytes('新增\n'.encode());(root/'gone.md').unlink()
        changed=check(call(),root,baseline)
        assert changed['delta']['added']==['added.md'] and changed['delta']['modified']==['a.py']
        assert changed['delta']['deleted']==['gone.md']
        for sid in ('0'*64,'../outside',''):
            raw=call(base_snapshot_id=sid);typed=decode_result(raw)
            assert typed['status']=='REJECTED' and typed['executed'] is False,raw
        latest=build_project_capsule(root)['snapshot_id'];a.unlink()
        deleted=json.loads(call(base_snapshot_id=latest))
        assert deleted['delta']['changed_paths']==['a.py'] and deleted['bundle']['bundle_count']==0,deleted
        assert deleted['tombstones']==[{'path':'a.py','deleted':True}],deleted
        assert deleted['_runtime_result']['data']['verification_status']=='PASS',deleted
        with patch('agent_core.project_bundle.verify_delta_replay',return_value={'status':'FAIL','mismatches':['a.py']}):
            failed=call(base_snapshot_id=latest)
        assert decode_result(failed)['status']!='SUCCEEDED' and 'delta_replay_mismatch' in failed,failed
        # A real Windows outward junction under the otherwise valid .agents.
        linked=root/'linked_project';linked.mkdir();(linked/'a.py').write_bytes(b'LINK_TEST')
        linked_base=build_project_capsule(linked)['snapshot_id']
        external=parent/'external_output';external.mkdir()
        junction=linked/'.agents'/'project_context'/'deltas'
        proc=subprocess.run(['cmd','/d','/c','mklink','/J',str(junction),str(external)],capture_output=True)
        assert proc.returncode==0,proc.stderr
        try:
            raw=call(workspace=str(linked),base_snapshot_id=linked_base)
            assert decode_result(raw)['status']=='REJECTED' and not list(external.iterdir()),raw
        finally:junction.rmdir() # Removes this owned junction, not its target.
        assert not ctx.take_pending_attachments()
    print('T19_BUILD_DELTA_MATRIX_PASS bytes_hash_empty_tombstone_scope')


if __name__=='__main__':run()
