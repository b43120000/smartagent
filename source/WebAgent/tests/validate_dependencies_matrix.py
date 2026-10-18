"""T20 dependency evidence is bounded source observation, not guessed graph."""
import json,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext


EXPECTED=[('CMakeLists.txt',1,'cmake_target_definition','app main.cpp'),
          ('CMakeLists.txt',2,'cmake_target_link_libraries','app PRIVATE core'),
          ('a.py',1,'import','os'),('main.cpp',1,'include','core.h')]


def fixture(root):
    root.mkdir(parents=True)
    (root/'a.py').write_bytes(b'import os\n')
    (root/'main.cpp').write_bytes(b'#include "core.h"\n')
    (root/'CMakeLists.txt').write_bytes(b'add_executable(app main.cpp)\ntarget_link_libraries(app PRIVATE core)\n')
    (root/'build.gradle').write_bytes(b'implementation project(":core")\n')


def check(raw):
    assert raw.lstrip().startswith('{'),raw
    p=json.loads(raw);typed=decode_result(raw)
    assert typed['status']=='SUCCEEDED' and typed['executed'] is True,p
    assert [(r['source'],r['evidence_line'],r['relation'],r['target']) for r in p['records']]==EXPECTED,p
    assert not p['warnings'],p
    assert p['unsupported_files']==['build.gradle'],p
    assert p['scanned_files']==['CMakeLists.txt','a.py','main.cpp'],p
    return p


def run():
    with tempfile.TemporaryDirectory(prefix='t20-dependencies-') as value:
        parent=Path(value).resolve();root=parent/'project';fixture(root)
        ctx=WebAgentToolContext(parent,interface_name='remote');ctx.begin_run('T20','只分析此專案\n'+str(root),[str(root)])
        def call(**fields):return ctx.execute({'tool':'extract_project_dependencies','workspace':str(root),**fields})
        expanded=call(workspace=str(parent))
        assert decode_result(expanded)['status']=='REJECTED' and 'workspace_scope:' in expanded,expanded
        check(call())
        original=Path.read_bytes
        def denied(path):
            if path==root/'a.py':raise PermissionError('T20_SYNTHETIC_DENIED')
            return original(path)
        with patch.object(Path,'read_bytes',denied):raw=call()
        p=json.loads(raw);typed=decode_result(raw)
        assert typed['status']=='PARTIAL' and typed['executed'] is True,p
        assert p['warnings']==[{'path':'a.py','warning':'PermissionError'}],p
        with patch.object(Path,'read_bytes',side_effect=PermissionError('T20_ALL_DENIED')):raw=call()
        assert decode_result(raw)['status']=='FAILED',raw
        assert not ctx.take_pending_attachments()
    print('T20_DEPENDENCIES_MATRIX_PASS supported_unsupported_read_failure_scope')


if __name__=='__main__':run()
