"""T21/T22: actual persisted history, read failures and filter boundaries."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.project_ledger import (append_project_event, inspect_project_ledger,
    query_project_history, project_ledger_path, project_ledger_index_path, create_ledger_entry)
from agent_core.tools import execute_tool
from agent_core.tool_result_contract import decode_result
from agent_core.security_context import SecurityContext
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import extract_authorized_paths
from agent_core.path_security import canonicalize_path, PathSecurityError


def run():
    json_path=r'E:\card\runtime_it\project'
    assert extract_authorized_paths(json.dumps({'workspace':json_path,'limit':1}))==[json_path]
    for bad in (json_path+':secret',json_path+'\\x:stream'):
        for payload in (json.dumps({'workspace':bad,'limit':1}),json.dumps({'workspace':bad}).replace(':secret',r'\u003asecret')):
            candidate=extract_authorized_paths(payload)
            assert candidate==[bad],candidate
            try:canonicalize_path(candidate[0],require_absolute=True)
            except PathSecurityError:pass
            else:raise AssertionError('JSON ADS accepted')
    with tempfile.TemporaryDirectory(prefix='matrix-ledger-') as temp:
        root=Path(temp); a=root/'a'; b=root/'b'; a.mkdir(); b.mkdir()
        assert inspect_project_ledger(a)['status']=='EMPTY'
        assert not project_ledger_path(a).exists()
        rows=[append_project_event(a,kind,snapshot_id=snap,changed_files=[path],timestamp=stamp)
            for kind,snap,path,stamp in [('sync_completed','S1','src/a.py',100),
                ('query_completed','S1','src/b.py',200),('sync_completed','S2','SRC/a.py',300)]]
        append_project_event(b,'sync_completed',snapshot_id='S1',timestamp=400)
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in
            (project_ledger_path(a),project_ledger_index_path(a))}
        info=inspect_project_ledger(a,limit=2)
        assert info['event_count']==3 and info['latest_snapshot_id']=='S2'
        assert [r['event_id'] for r in info['recent_events']]==[r['event_id'] for r in rows[:0:-1]]
        import agent_core.project_ledger as module
        good=module._load_index(a,strict=True)
        with patch.object(module,'_load_index',side_effect=[{**good,'event_count':2},good]) as read_index:
            recovered=inspect_project_ledger(a)
            assert recovered['event_count']==3 and len(recovered['recent_events'])==3 and read_index.call_count==2
        empty_index={**good,'event_count':0,'latest_event_id':'STALE'}
        with patch.object(module,'_load_index',return_value=empty_index),patch.object(module,'_read_events',return_value=[]):
            pending=inspect_project_ledger(a)
            assert pending['status']=='PARTIAL' and pending['latest_event_id']==''
        # Writer stays in its publication window beyond every bounded retry.
        with patch.object(module,'_load_index',return_value={**good,'event_count':2}):
            pending=inspect_project_ledger(a)
            assert pending['status']=='PARTIAL' and pending['event_count']==len(pending['recent_events'])==3
        cases=[({},rows[::-1]),({'event_types':['sync_completed']},[rows[2],rows[0]]),
            ({'snapshot_id':'S1'},[rows[1],rows[0]]),({'path_contains':'src\\A.py'},[rows[2],rows[0]]),
            ({'since':200},[rows[2],rows[1]]),({'since':301},[]),({'snapshot_id':'missing'},[]),
            ({'limit':0},[rows[2]]),({'limit':999},rows[::-1]),
            ({'event_types':['sync_completed'],'snapshot_id':'S1','since':100,'path_contains':'a.py'},[rows[0]])]
        ctx=WebAgentToolContext(a);ctx.current_request_id='T21-T22';ctx.current_task_id='MATRIX'
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)):
            for fields,expected in cases:
                raw=execute_tool(dict(tool='query_project_history',workspace=str(a),**fields),ctx)
                p=json.loads(raw);result=decode_result(raw)
                assert result['executed'] and result['status']=='SUCCEEDED',result
                assert [r['event_id'] for r in p['events']]==[r['event_id'] for r in expected],p
            raw=execute_tool(dict(tool='inspect_project_ledger',workspace=str(a),limit=1),ctx)
            assert decode_result(raw)['status']=='SUCCEEDED'
        assert before=={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before}
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)):
            for tool in ('inspect_project_ledger','query_project_history'):
                with patch('agent_core.tools.'+tool,side_effect=PermissionError('denied')):
                    failed=decode_result(execute_tool(dict(tool=tool,workspace=str(a)),ctx))
                    assert failed['status']=='FAILED' and failed['executed'] and not failed['data']['possible_partial_execution'],failed
        with patch.object(Path,'read_text',side_effect=PermissionError('test denied')):
            try:inspect_project_ledger(a)
            except PermissionError:pass
            else:raise AssertionError('Unreadable ledger falsely accepted')
        index=project_ledger_index_path(a);index.unlink()
        pending=inspect_project_ledger(a)
        assert pending['status']=='PARTIAL' and pending['event_count']==3
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)):
            pending=decode_result(execute_tool(dict(tool='inspect_project_ledger',workspace=str(a)),ctx))
            assert pending['status']=='PARTIAL' and pending['executed'] and not pending['data']['terminal_eligible']
        project_ledger_path(a).write_text('{broken',encoding='utf-8')
        try:query_project_history(a)
        except ValueError as exc:assert 'corrupt_line' in str(exc)
        else:raise AssertionError('Corruption falsely accepted')
        project_ledger_index_path(b).parent.mkdir(parents=True,exist_ok=True)
        project_ledger_index_path(b).write_text('{}',encoding='utf-8')
        try:inspect_project_ledger(b)
        except ValueError as exc:assert 'index_invalid' in str(exc)
        else:raise AssertionError('Corrupt empty index accepted')
        for bad_index in ({'schema':'PROJECT_LEDGER_INDEX_V1','event_count':0,'latest_event_id':''},
                          {**good,'event_count':[]},{**good,'event_type_counts':[]},
                          {**good,'event_count':True}):
            project_ledger_index_path(b).write_text(json.dumps(bad_index),encoding='utf-8')
            with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)):
                failed=decode_result(execute_tool(dict(tool='inspect_project_ledger',workspace=str(b)),ctx))
                assert failed['status']=='FAILED' and failed['reason']=='LEDGER_READ_FAILED',failed
        for func in (inspect_project_ledger,query_project_history):
            try:func(root/'missing')
            except ValueError as exc:assert 'workspace_not_directory' in str(exc)
            else:raise AssertionError('Missing project accepted')
        large=root/'large';large.mkdir();file=project_ledger_path(large);file.parent.mkdir(parents=True)
        file.write_text('\n'.join(json.dumps(create_ledger_entry(large,event_type='fixture_event',timestamp=i)) for i in range(505)),encoding='utf-8')
        capped=query_project_history(large,limit=999)
        assert capped['match_count']==500 and capped['events'][0]['timestamp']==504 and capped['events'][-1]['timestamp']==5
    print('PROJECT_LEDGER_MATRIX_PASS')

if __name__=='__main__':run()
