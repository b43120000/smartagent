"""T17 compares a persisted baseline to current bytes without scope expansion."""
import json
import os
import tempfile
from pathlib import Path
from agent_core.project_access import build_project_capsule
from agent_core.project_sync import load_project_snapshot
from agent_core.tool_result_contract import decode_result
from agent_core.protocol_v8 import validate_model_action
from WebAgent.tool_context import WebAgentToolContext


def run():
    with tempfile.TemporaryDirectory(prefix='t17-compare-') as value:
        parent=Path(value).resolve();root=parent/'project';root.mkdir()
        path=root/'a.py';path.write_bytes(b'AA0\n');(root/'gone.md').write_bytes(b'gone\n')
        baseline=build_project_capsule(root)['snapshot_id']
        admitted=validate_model_action({'tool':'compare_project_snapshot','action_id':'T17-ID-ADMISSION',
                                       'workspace':str(root),'known_snapshot':baseline})
        assert admitted['known_snapshot']==baseline
        ctx=WebAgentToolContext(parent,interface_name='remote')
        ctx.begin_run('T17','只比對此專案\n'+str(root),[str(root)])
        def call(**fields):
            return ctx.execute({'tool':'compare_project_snapshot','workspace':str(root),'known_snapshot':baseline,**fields})
        same=json.loads(call());assert same['freshness']=='FRESH' and same['changed_paths']==[]
        old=path.stat();path.write_bytes(b'AA1\n');os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
        (root/'added.md').write_bytes(b'added\n');(root/'gone.md').unlink()
        changed=json.loads(call())
        assert changed['freshness']=='CHANGED' and changed['changed_paths']==['a.py','added.md','gone.md'],changed
        for fields in ({'known_snapshot':123},{'known_snapshot':'0'*64},{'workspace':str(parent)},
                       {'known_snapshot':{'files':[{}]}}, {'known_snapshot':{'files':[{'path':[]}]}},
                       {'known_snapshot':{'files':[],'workspace_root':{}}}):
            raw=call(**fields);typed=decode_result(raw)
            assert typed['status']=='REJECTED' and typed['executed'] is False,raw
        outside=parent/'outside.json';outside.write_text('{"sentinel":"OUTSIDE_REGISTRY"}',encoding='utf-8')
        assert load_project_snapshot(root,str(outside.with_suffix('')))=={},'snapshot ID accepted an absolute filename'
        assert not ctx.take_pending_attachments()
    print('T17_COMPARE_MATRIX_PASS fresh_changed_invalid_scope')


if __name__=='__main__':run()
