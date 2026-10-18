"""T16 native execution/status checks with owned small source fixtures."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from agent_core.tool_result_contract import decode_result
from agent_core.action_loop_state import build_action_loop_state
from WebAgent.tool_context import WebAgentToolContext

QUERIES = [
    {'operation': 'list_tree', 'path': '.', 'depth': 2},
    {'operation': 'read_range', 'path': 'a.py', 'start_line': 1, 'end_line': 999},
    {'operation': 'search_text', 'query': 't16_probe'},
    {'operation': 'find_references', 'symbol': 't16_probe'},
    {'operation': 'read_symbol', 'symbol': 't16_probe'},
    {'operation': 'get_build_configuration'},
    {'operation': 'get_file_metadata', 'path': 'a.py'},
]


def fixture(root):
    root.mkdir(parents=True)
    (root / 'a.py').write_bytes('def t16_probe():\n    return "測試"\n\nvalue = t16_probe()\n'.encode())
    (root / 'CMakeLists.txt').write_bytes(b'project(T16)\n')
    (root / 'notes.md').write_bytes(b'T16 notes\n')
    (root / 'long.py').write_bytes(''.join(f'line_{i:04}\n' for i in range(1, 521)).encode())


def validate_happy(raw, root):
    payload = json.loads(raw)
    assert decode_result(raw)['status'] == 'SUCCEEDED', payload
    assert payload['attachments_uploaded'] == 0 and len(payload['results']) == 7
    assert [r['operation'] for r in payload['results']] == [q['operation'] for q in QUERIES]
    assert all(r['status'] == 'OK' for r in payload['results']), payload
    ranged = payload['results'][1]
    lines = (root / 'a.py').read_text(encoding='utf-8').splitlines()
    assert ranged['content'] == '\n'.join(f'{i}: {s}' for i, s in enumerate(lines, 1))
    assert ranged['truncated'] is False and ranged['next_cursor'] is None
    assert ranged['file_sha256'] == hashlib.sha256((root / 'a.py').read_bytes()).hexdigest()
    assert len(payload['results'][2]['matches']) == 2 and len(payload['results'][3]['matches']) == 2
    assert 'def t16_probe' in payload['results'][4]['content']
    assert 'project(T16)' in payload['results'][5]['items'][0]['content']
    assert payload['results'][6]['file']['sha256'] == ranged['file_sha256']
    return payload


def run():
    with tempfile.TemporaryDirectory(prefix='t16-query-matrix-') as value:
        root = Path(value).resolve() / 'project'
        fixture(root)
        ctx = WebAgentToolContext(root, interface_name='remote')
        ctx.begin_run('T16-NATIVE', '只查詢此專案\n'+str(root), [str(root)])
        def call(queries, **fields):
            return ctx.execute({'tool': 'query_project', 'project_root': str(root), 'queries': queries, 'max_bytes': 16000, **fields})
        validate_happy(call(QUERIES), root)
        pages = []
        cursor = 1
        while cursor is not None:
            p = json.loads(call([{'operation': 'read_range', 'path': 'long.py', 'start_line': cursor, 'end_line': 999}]))
            r = p['results'][0]
            assert r['status'] == 'OK'
            pages.extend(r['content'].splitlines())
            cursor = r['next_cursor']
            assert len(pages) <= 520
        assert pages == [f'{i}: line_{i:04}' for i in range(1, 521)]
        mixed = call([QUERIES[1], {'operation': 'read_range', 'path': 'missing.py'}])
        p = json.loads(mixed)
        assert p['results'][0]['status'] == 'OK' and p['results'][1]['status'] == 'ERROR'
        assert decode_result(mixed)['status'] == 'PARTIAL', mixed
        query_action={'tool':'query_project','action_id':'T16-MIXED'}
        state=build_action_loop_state(action=query_action,result=mixed,execution_status='PARTIAL',
            action_records=[{'action':query_action,'result':mixed,'execution_status':'PARTIAL'}])
        assert state['evidence_state']=='AVAILABLE' and not state['blockers'],state
        for pending_action,pending_result in (
            ({'tool':'write_file','action_id':'T16-WRITE'},mixed),
            (query_action,json.dumps({**p,'response_truncated':True})),
        ):
            pending=build_action_loop_state(action=pending_action,result=pending_result,execution_status='PARTIAL',
                action_records=[{'action':pending_action,'result':pending_result,'execution_status':'PARTIAL'}])
            assert pending['blockers'],pending
        before = (root / 'a.py').stat()
        (root / 'a.py').write_bytes((root / 'a.py').read_bytes().replace(b'value', b'other'))
        os.utime(root / 'a.py', ns=(before.st_atime_ns, before.st_mtime_ns))
        changed = json.loads(call([QUERIES[1]]))
        assert changed['index_context']['recovery_action'] == 'INDEX_REBUILT_ONCE'
        assert changed['results'][0]['file_sha256'] == hashlib.sha256((root / 'a.py').read_bytes()).hexdigest()
        escaped = call([{'operation': 'read_range', 'path': '../outside.py'}])
        assert decode_result(escaped)['status'] in {'REJECTED', 'FAILED'}, escaped
        assert not ctx.take_pending_attachments()
        scoped = WebAgentToolContext(root.parent, interface_name='remote')
        scoped.begin_run('T16-SCOPE', '只查詢此專案\n'+str(root), [str(root)])
        expanded = scoped.execute({'tool':'query_project','project_root':str(root.parent),'queries':[QUERIES[0]]})
        assert decode_result(expanded)['status']=='REJECTED' and decode_result(expanded)['executed'] is False, expanded
    print('T16_QUERY_MATRIX_PASS seven_operations pagination partial stale escape')


if __name__ == '__main__':
    run()
