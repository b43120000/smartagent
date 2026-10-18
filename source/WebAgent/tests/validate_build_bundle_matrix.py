"""T18 materialization, content identity, boundaries, and local-only delivery."""
import hashlib,json,os,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.project_access import build_project_capsule
from agent_core.project_bundle import reconstruct_bundle
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext


def check(raw,root):
    assert raw.lstrip().startswith('{'),raw
    p=json.loads(raw);typed=decode_result(raw)
    assert typed['status']=='SUCCEEDED' and typed['executed'] is True,p
    assert typed['data']['verification_status']=='PASS' and typed['data']['delivery_status']=='NOT_ATTEMPTED',typed
    assert p['complete'] and p['source_file_count']==2,p
    contents={}
    for item in p['bundles']:
        path=Path(item['path']);assert path.is_file()
        assert path.stat().st_size<=p['policy']['max_bytes_per_bundle']
        assert item['file_count']<=p['policy']['max_files_per_bundle']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
        contents.update(reconstruct_bundle(path))
    assert contents=={name:(root/name).read_bytes() for name in ('a.py','b.md')},contents
    return p


def run():
    with tempfile.TemporaryDirectory(prefix='t18-bundle-') as value:
        parent=Path(value).resolve();root=parent/'project';root.mkdir()
        a=root/'a.py';a.write_bytes(b'AA0\n');(root/'b.md').write_bytes('合成文件\n'.encode())
        secret=root/'localdata'/'secure';secret.mkdir(parents=True);(secret/'tokens.json').write_bytes(b'{"token":"SYNTHETIC_DO_NOT_BUNDLE"}')
        build_project_capsule(root)
        ctx=WebAgentToolContext(parent,interface_name='remote');ctx.begin_run('T18','只打包此專案\n'+str(root),[str(root)])
        def call(**fields):return ctx.execute({'tool':'build_project_bundle','workspace':str(root),'max_bytes':128000,'max_files':1,**fields})
        p=check(call(),root);assert p['bundle_count']==2
        old=a.stat();a.write_bytes(b'AA1\n');os.utime(a,ns=(old.st_atime_ns,old.st_mtime_ns))
        check(call(),root)
        bad_dir=parent/'forbidden_output'
        denied=call(output_dir=str(bad_dir));assert decode_result(denied)['status']=='REJECTED' and not bad_dir.exists(),denied
        expanded=call(workspace=str(parent));assert decode_result(expanded)['status']=='REJECTED',expanded
        # Self-consistent forged payload/header must not pass physical readback.
        original_replace=os.replace
        def corrupt(src,dst):
            if Path(dst).name.startswith('source_part_'):
                path=Path(src);data=path.read_bytes()
                path.write_bytes(data.replace(hashlib.sha256(b'AA1\n').hexdigest().encode(),hashlib.sha256(b'ZZ9\n').hexdigest().encode()).replace(b'AA1\n',b'ZZ9\n'))
            return original_replace(src,dst)
        with patch('agent_core.project_bundle.os.replace',corrupt):
            failed=call()
        assert decode_result(failed)['status']!='SUCCEEDED' and 'bundle_readback_mismatch' in failed,failed
        output=root/'.agents'/'hardlink_output';output.mkdir(parents=True)
        external=parent/'external_sentinel.txt';external.write_bytes(b'KEEP_EXTERNAL')
        os.link(external,output/'source_part_001.bundle.txt')
        check(call(output_dir=str(output)),root)
        assert external.read_bytes()==b'KEEP_EXTERNAL'
        assert not ctx.take_pending_attachments()
    print('T18_BUILD_BUNDLE_MATRIX_PASS materialized_hash_scope_no_upload')


if __name__=='__main__':run()
