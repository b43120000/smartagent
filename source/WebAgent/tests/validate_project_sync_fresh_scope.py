"""T15 regression: sync publishes current bytes inside exact request scope."""
import hashlib
import os
import tempfile
from pathlib import Path

from agent_core.project_access import build_project_capsule
from agent_core.project_sync import load_project_snapshot
from agent_core.project_sync_message import build_atomic_project_sync
from agent_core.tools import _project_sync_workspace, execute_tool
from agent_core.tool_result_contract import decode_result
from WebAgent.tests.validate_project_sync_root_resolution import FakeAgent


def run():
    with tempfile.TemporaryDirectory(prefix='t15-fresh-scope-') as value:
        parent = Path(value).resolve()
        project = parent / 'project'
        project.mkdir()
        path = project / 'a.py'
        path.write_bytes(b'A0\n')
        agent = FakeAgent(parent)
        agent._authorized_local_paths = [str(project)]
        assert Path(_project_sync_workspace({}, agent)) == project
        for fields in ({'project_root': str(parent)}, {'workspace': str(parent)}):
            try:
                _project_sync_workspace(fields, agent)
            except ValueError as exc:
                assert 'request_scope' in str(exc), exc
            else:
                raise AssertionError('sync expanded beyond explicit root')
            result = decode_result(execute_tool({'tool': 'project_sync', 'strategy': 'INDEX_ONLY', **fields}, agent))
            assert result['status'] == 'REJECTED' and result['executed'] is False, result
        other = parent / 'other'
        other.mkdir()
        agent._authorized_local_paths.append(str(other))
        try:
            _project_sync_workspace({}, agent)
        except ValueError as exc:
            assert 'root_required' in str(exc), exc
        else:
            raise AssertionError('ambiguous default root was accepted')
        result = decode_result(execute_tool({'tool': 'project_sync', 'strategy': 'INDEX_ONLY'}, agent))
        assert result['status'] == 'REJECTED' and result['executed'] is False, result
        assert Path(_project_sync_workspace({'project_root': str(project)}, agent)) == project
        agent._authorized_local_paths = []
        assert Path(_project_sync_workspace({}, agent)) == parent
        for strategy in ('INDEX_ONLY', 'FULL_BUNDLE', 'DELTA'):
            path.write_bytes(b'A0\n')
            base = build_project_capsule(project)
            old = path.stat()
            path.write_bytes(b'A1\n')
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
            if strategy == 'INDEX_ONLY':
                result = build_project_capsule(project)
            else:
                result = build_atomic_project_sync(project, strategy,
                    base_snapshot_id=base['snapshot_id'], max_bytes=128000, max_files=10)
            assert result['status'] == 'READY', result
            snapshot = load_project_snapshot(project, result['snapshot_id'])
            row = next(r for r in snapshot['files'] if r['path'] == 'a.py')
            assert row['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest(), strategy
            assert result['snapshot_id'] != base['snapshot_id'], strategy
            if strategy == 'DELTA':
                assert result['bundle_count'] == 1, result
    print('T15_FRESH_SCOPE_PASS')


if __name__ == '__main__':
    run()
