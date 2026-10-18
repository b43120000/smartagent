"""C-release T16 real Runtime-loop test; no production modifications."""
import json
import uuid
from pathlib import Path
import agent_core
import WebAgent
import RemoteAgent
from agent_core.json_state_io import write_json_atomic
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_query_project_matrix import QUERIES, fixture, validate_happy

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'source'
STATE = ROOT / 'localdata/runtime_it/matrix38/t16_query_project.json'


def packet(progress, action):
    objects = [progress, action, {'tool': 'turn_commit', 'action_count': 2}]
    return '```smartagent_tool\n' + '\n'.join(json.dumps(x, ensure_ascii=False) for x in objects) + '\n```'


def run():
    for package in (agent_core, WebAgent, RemoteAgent):
        assert Path(package.__file__).resolve().is_relative_to(SOURCE), package.__file__
    parent = ROOT / 'localdata/runtime_it/matrix38' / ('T16-' + uuid.uuid4().hex[:12])
    root = parent / 'project'
    fixture(root)
    ctx = WebAgentToolContext(parent, interface_name='remote')
    first = {'tool': 'query_project', 'action_id': 'T16-R-ALL-' + uuid.uuid4().hex[:10], 'project_root': str(root), 'queries': QUERIES, 'max_bytes': 16000}
    second = {'tool': 'query_project', 'action_id': 'T16-R-PARTIAL-' + uuid.uuid4().hex[:10], 'project_root': str(root), 'queries': [{'operation': 'read_range', 'path': 'a.py', 'start_line': 1, 'end_line': 999}, {'operation': 'read_range', 'path': 'missing.py', 'start_line': 1, 'end_line': 10}], 'max_bytes': 16000}
    goal = 'T16 真 Runtime loop 正確查詢七種 operation 並保留合法批次 PARTIAL 結果'
    progress = {**initial(), 'action_id': 'T16-R-P1', 'base_evaluation': 'C release T16 isolated synthetic project', 'next_action': 'query_project', 'completion_contract': {'success': [goal], 'failure': ['T16 Runtime 查詢或結果核對失敗'], 'in_progress': ['等待真 Runtime query_project 證據'], 'interrupted': ['T16 Runtime 環境阻塞']}, 'matched_condition': '等待真 Runtime query_project 證據'}
    verifier = {'tool': 'run_command', 'action_id': 'T16-R-VERIFY-' + uuid.uuid4().hex[:10],
                'operation': 'VERIFY', 'command': "Get-Content -LiteralPath 'a.py' -Raw",
                'working_directory': str(root), 'verifies_action_id': second['action_id'],
                'condition_id': WebAgentProtocolLoop._canonical_condition_id(goal),
                'success_criteria': 'The expected mixed query PARTIAL contains a valid a.py result and a missing.py error; validate fixture postconditions without changing either file.',
                'verify': [{'action': 'file_exists', 'path': 'a.py', 'expect': True},
                           {'action': 'file_exists', 'path': 'missing.py', 'expect': False},
                           {'action': 'file_contains', 'path': 'a.py', 'text': 'def t16_probe', 'expect': True}]}
    turns = []

    def planner(prompt, expected, attachments):
        assert not attachments
        turns.append(prompt)
        assert len(turns) <= 4, 'unexpected Runtime repair or additional action'
        if len(turns) == 1:
            return packet(progress, second)
        if len(turns) == 2:
            raw = loop.action_ledger[second['action_id']]['result']
            payload = json.loads(raw)
            assert decode_result(raw)['status'] == 'PARTIAL', raw
            assert [x['status'] for x in payload['results']] == ['OK', 'ERROR'], raw
            assert payload['attachments_uploaded'] == 0
            updated = {**progress, 'action_id': 'T16-R-P2', 'evidence_refs': [second['action_id']], 'decision_reason': '混合批次 PARTIAL 已完成逐項核對，接著執行完整七種查詢'}
            return packet(updated, first)
        if len(turns) == 3:
            validate_happy(loop.action_ledger[first['action_id']]['result'], root)
            updated = {**progress, 'action_id': 'T16-R-P3', 'evidence_refs': [second['action_id'], first['action_id']], 'decision_reason': '七種查詢均已核對，接著以真實 VERIFY 確認預期 PARTIAL 的檔案後置條件', 'next_action': 'run_command'}
            return packet(updated, verifier)
        checked = decode_result(loop.action_ledger[verifier['action_id']]['result'])
        assert checked['status'] == 'SUCCEEDED' and checked['data']['verification_status'] == 'PASS', checked
        assert loop.action_result_ledger[second['action_id']].get('pending_resolution') == 'SCOPED_VERIFICATION_OBTAINED'
        refs = [second['action_id'], first['action_id'], verifier['action_id']]
        assert loop._terminal_evidence_verdict(refs, matched_condition=goal) == 'PASS'
        done = {**progress, 'action_id': 'T16-R-P4', 'decision': 'COMPLETE', 'outcome': 'SUCCESS', 'current_step': 1, 'steps': [{'step': 1, 'desc': '驗證 query_project 真 Runtime loop', 'status': 'COMPLETED'}], 'matched_condition': goal, 'evidence_refs': refs, 'decision_reason': '預期 PARTIAL、七種查詢與綁定的實際 VERIFY 均已核對通過', 'next_action': 'final_response'}
        return packet(done, {'tool': 'final_response', 'action_id': 'T16-R-FINAL', 'content': 'T16_RUNTIME_PASS'})

    state = {'tool': 'query_project', 'status': 'IN_PROGRESS', 'fixture': str(parent), 'target': str(root), 'phases': {}, 'rca': []}
    try:
        loop = WebAgentProtocolLoop(parent, planner, tool_context=ctx, progress_root=ROOT)
        output = loop.run('只對合成專案進行 T16 query_project 查詢，不修改內容、不上傳。\n' + str(root))
        assert output == 'T16_RUNTIME_PASS', output
        state['phases']['IT-R'] = {'status': 'PASS', 'rounds': len(turns), 'action_ids': [first['action_id'], second['action_id']]}
    except Exception as exc:
        state['phases']['IT-R'] = {'status': 'FAIL', 'error': repr(exc)[:3000], 'rounds': len(turns)}
        write_json_atomic(STATE, state)
        raise
    write_json_atomic(STATE, state)
    print('T16_IT_R_PASS', state['target'], 'rounds=' + str(len(turns)), flush=True)


if __name__ == '__main__':
    run()
