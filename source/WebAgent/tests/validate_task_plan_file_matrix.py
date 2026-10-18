"""T27: actual file import, trusted request scope and no source mutation."""
import hashlib
import json
import tempfile
from pathlib import Path

from agent_core.task_plan import freeze_task_plan_file
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_task_plan_matrix import task_plan


def fixture(parent):
    root = parent / 'project'
    root.mkdir(parents=True)
    (root / 'probe.txt').write_bytes(b'BEFORE\n')
    source = root / '.agents' / 'input_plan.json'
    source.parent.mkdir()
    plan = task_plan(root)
    raw = json.dumps(plan).encode()
    source.write_bytes(raw)
    return root, source, hashlib.sha256(raw).hexdigest()


def run():
    with tempfile.TemporaryDirectory(prefix='t27-') as value:
        parent = Path(value).resolve()
        root, source, digest = fixture(parent)
        ctx = WebAgentToolContext(parent, interface_name='remote')
        ctx.begin_run('T27', 'Freeze only\n' + str(root), [str(root)])
        ctx.current_task_epoch = 'trusted-epoch'
        ctx.current_task_id = 'trusted-task'
        ctx.current_intent_digest = 'trusted-digest'
        ctx._protocol_turn_seq = 1
        action = dict(tool='propose_task_plan_file', workspace=str(root),
                      path=str(source), expected_sha256=digest)
        result = json.loads(ctx.execute(action))
        assert result['status'] == 'PLAN_FROZEN', result
        stored = json.loads(Path(result['path']).read_text(encoding='utf-8'))
        assert stored['request_scope']['request_id'] == 'T27', stored
        assert stored['request_scope']['task_epoch'] == 'trusted-epoch', stored
        frozen_before = {p.name: p.read_bytes() for p in (root / '.agents' / 'task_plans').glob('*.json')}
        for override in ({'workspace':str(parent)}, {'path':'.agents/input_plan.json'},
                         {'path':str(root/'*.json')}, {'path':str(source)+':stream'}):
            denied=ctx.execute({**action, **override})
            assert 'REJECTED' in denied, denied
        for path, sha in ((source, '0'*64), (root/'absent.json', digest)):
            bad = freeze_task_plan_file(root, str(path), sha)
            assert bad['status'] != 'PLAN_FROZEN', bad
        source.write_text('{broken', encoding='utf-8')
        assert freeze_task_plan_file(root, str(source))['status'] == 'INVALID_PLAN_FILE'
        source.write_text('[]', encoding='utf-8')
        assert freeze_task_plan_file(root, str(source))['status'] == 'INVALID_PLAN'
        outside = parent/'outside.json'
        outside.write_text('{}', encoding='utf-8')
        assert freeze_task_plan_file(root, str(outside))['status'] == 'INVALID_PLAN_FILE'
        assert {p.name:p.read_bytes() for p in (root/'.agents'/'task_plans').glob('*.json')} == frozen_before
        assert (root/'probe.txt').read_bytes() == b'BEFORE\n'
    print('T27_TASK_PLAN_FILE_MATRIX_PASS')


if __name__ == '__main__':
    run()
