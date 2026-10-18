"""T38 real persistence, negative IO results and workspace isolation."""
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from WebAgent.tool_context import WebAgentToolContext
from agent_core.tool_result_contract import decode_result


def payload(marker):
    return dict(tool='save_session_summary',action_id='SUMMARY-IT',summary=marker,
        decisions=['DECISION_'+marker],modified_files=[],verification=['VERIFIED_'+marker],
        pending=['PENDING_'+marker],next_steps=['NEXT_'+marker])


def verify(raw,root,action):
    out=decode_result(raw);target=root/'.agents'/'agent_session_summary.md'
    assert out['status']=='SUCCEEDED' and out['executed'],out
    data=target.read_bytes()
    assert out['data']['sha256']==hashlib.sha256(data).hexdigest()
    assert out['data']['size_bytes']==len(data) and out['data']['verification_status']=='PASS'
    for key in ('summary','decisions','modified_files','verification','pending','next_steps'):
        assert ('## '+('Summary' if key=='summary' else key)).encode() in data
        for value in ([action[key]] if key=='summary' else action[key]):
            assert value.encode() in data,(key,data)
    assert ('Request: '+out['request_id']).encode() in data
    return out


def run():
    with tempfile.TemporaryDirectory(prefix='t38-') as value:
        parent=Path(value).resolve();root=parent/'workspace';other=parent/'other'
        root.mkdir();other.mkdir();(other/'sentinel.txt').write_bytes(b'KEEP')
        ctx=WebAgentToolContext(root,interface_name='remote');ctx.begin_run('T38','Save summary',[])
        action=payload('T38_UNIQUE');verify(ctx.execute(action),root,action)
        target=root/'.agents'/'agent_session_summary.md';before=target.read_bytes()
        for fields in ({'summary':''},{'pending':3},{'summary':'X'*65537}):
            out=decode_result(ctx.execute({**action,**fields}))
            assert out['status']=='REJECTED' and not out['executed'],out
            assert target.read_bytes()==before
        original_read=Path.read_bytes
        def denied_read(path):
            if path==target:raise PermissionError('T38_READBACK_DENIED')
            return original_read(path)
        with patch.object(Path,'read_bytes',denied_read):
            out=decode_result(ctx.execute(payload('WRITTEN_BUT_UNVERIFIED')))
        assert out['status']=='PARTIAL' and out['data']['file_saved'] and out['data']['verification_status']=='FAIL',out
        assert b'WRITTEN_BUT_UNVERIFIED' in target.read_bytes()
        before=target.read_bytes();original_write=Path.write_bytes
        def denied_write(path,raw):
            if path.name.endswith('.tmp') and path.parent==target.parent:raise PermissionError('T38_WRITE_DENIED')
            return original_write(path,raw)
        with patch.object(Path,'write_bytes',denied_write):
            out=decode_result(ctx.execute(payload('NOT_WRITTEN')))
        assert out['status']=='FAILED' and not out['data']['file_saved'],out
        assert target.read_bytes()==before and not list(target.parent.glob('*.tmp'))
        other_ctx=WebAgentToolContext(other,interface_name='remote');other_ctx.begin_run('OTHER','Save summary',[])
        verify(other_ctx.execute(payload('OTHER_ONLY')),other,payload('OTHER_ONLY'))
        assert target.read_bytes()==before and (other/'sentinel.txt').read_bytes()==b'KEEP'
    print('T38_SESSION_SUMMARY_MATRIX_PASS')


if __name__=='__main__':run()
