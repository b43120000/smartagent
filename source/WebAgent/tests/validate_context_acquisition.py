"""Missing local evidence continues; user choices and objective blockers pause."""
import tempfile
from pathlib import Path

from agent_core.context_acquisition import context_capabilities, context_interrupt_allowed
from agent_core.narrative_bridge import NarrativeDecisionBridge
from agent_core.protocol_v9 import parse_v9_tool_transport
from agent_core.task_progress import initialize_progress, read_progress
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_narrative_v9_bridge import expected
from WebAgent.tests.validate_progress_terminal_lifecycle import completed_progress, response


def bind(loop, root, name='T'):
    loop.run_id = loop.task_id = loop.task_epoch = name
    loop.intent_digest = 'a' * 64
    loop.authorized_paths = [str(root)]
    loop.progress_ledger = initialize_progress(name, request_id=name, goal='評估專案，不修改', root=root)


def interrupted(request=None):
    p = completed_progress('P-STOP')
    p.update(decision='INTERRUPT', outcome='UNKNOWN', matched_condition='Runtime 無法取得編譯程序結果')
    if request is not None:
        p['context_request'] = request
    return [p, {'tool':'final_response','action_id':'F-STOP','content':'需要更多資料'},
            {'tool':'turn_commit','action_count':2}]


def run():
    with tempfile.TemporaryDirectory(prefix='context-acquisition-') as temp:
        root = Path(temp)
        caps = context_capabilities([root], ['list_directory', 'query_project'], {})
        assert not context_interrupt_allowed({}, caps)
        assert not context_interrupt_allowed({'kind':'USER_INPUT','detail':'請上傳 Gradle 檔案'}, caps)
        assert context_interrupt_allowed({'kind':'USER_INPUT','detail':'你偏好哪種輸出格式？'}, caps)
        not_found = context_capabilities([root], ['list_directory'], {'MISSING':{'execution_status':'FAILED','result':'FileNotFoundError'}})
        assert not not_found['objective_blockers']
        blocked = context_capabilities([root], ['query_project'], {'DENIED':{'execution_status':'FAILED','result':'PermissionError: access denied'}})
        assert context_interrupt_allowed({'kind':'BLOCKED','detail':'客觀拒絕','blocking_action_id':'DENIED'}, blocked)
        assert not context_interrupt_allowed({'kind':'BLOCKED','detail':'模型聲稱受阻','blocking_action_id':'FAKE'}, blocked)
        exp = expected(root)
        exp['narrative_recovery_context'].update(authorized_paths=[str(root)], context_capabilities=caps)
        bridge = NarrativeDecisionBridge.create(expected=exp, source_text='缺少 Gradle 設定，下一步需要查看專案目錄', root=root)
        bridge.accept_reply('NEED_MORE_CONTEXT')
        assert bridge.draft.pending_slot == 'context_kind'
        assert 'RUNTIME_READ' in bridge.next_prompt()
        bridge.accept_reply('RUNTIME_READ')
        assert bridge.draft.pending_slot == 'tool' and not bridge.ready
        bridge.accept_reply('LIST_DIRECTORY')
        bridge.accept_reply(str(root))
        calls, errors = parse_v9_tool_transport(bridge.canonical_response())
        assert not errors and calls[0]['decision']=='CONTINUE'
        assert calls[0]['context_request']['kind']=='RUNTIME_READ'
        assert calls[1]['tool']=='list_directory' and calls[1]['path']==str(root)
        # Bridge never executes reads; one model-selected read enters the loop.
        (root / 'HdrCore').mkdir()
        prompts = []
        def planner(prompt, expected_commit, attachments):
            prompts.append(prompt)
            if len(prompts)==1:
                return bridge.canonical_response()
            assert 'HdrCore' in prompt and 'TOOL_SCOPE_REJECTED' not in prompt
            raise RuntimeError('TEST_READ_RESULT_RETURNED')
        loop = WebAgentProtocolLoop(root, planner, progress_root=root)
        actual = []
        execute_original = loop._execute_action
        def execute(action):
            actual.append(action)
            result = execute_original(action)
            assert 'HdrCore' in result and 'TOOL_SCOPE_REJECTED' not in result, result
            return result
        loop._execute_action = execute
        try:
            loop.run('評估 HdrCore，只讀不修改。\n專案路徑：\n'+str(root)+'\n',
                     trusted_authorized_paths=[str(root)])
        except RuntimeError as exc:
            assert str(exc)=='TEST_READ_RESULT_RETURNED', exc
        else:
            raise AssertionError('test stop missing')
        assert len(actual)==1 and actual[0]['tool']=='list_directory'
        assert read_progress(loop.task_id, root).context_request['kind']=='RUNTIME_READ'
        # Canonical INTERRUPT cannot bypass the same current-scope guard.
        loop = WebAgentProtocolLoop(root, lambda *a:'', progress_root=root)
        bind(loop, root, 'SCOPED')
        accepted, diagnostics = loop._accept_ack(interrupted(), {'ack_web_ack_id':''})
        assert not accepted and diagnostics[0]['reason']=='missing_context_requires_runtime_read'
        accepted, diagnostics = loop._accept_ack(interrupted({'kind':'USER_INPUT','detail':'請選擇輸出格式'}), {'ack_web_ack_id':''})
        assert accepted and not diagnostics
        loop.action_result_ledger['DENIED']={'execution_status':'FAILED'}
        loop.action_ledger['DENIED']={'result':'PermissionError: access denied'}
        accepted, diagnostics = loop._accept_ack(interrupted({'kind':'BLOCKED','detail':'權限拒絕','blocking_action_id':'DENIED'}), {'ack_web_ack_id':loop.pending_web_ack_id})
        assert accepted and not diagnostics, diagnostics
        # User-input bridge collects an explicit question before interrupting.
        human = NarrativeDecisionBridge.create(expected=exp, source_text='請選擇輸出格式', root=root)
        human.accept_reply('NEED_MORE_CONTEXT'); human.accept_reply('USER_INPUT')
        assert not human.ready
        human.accept_reply('你偏好 HTML 還是 Markdown？')
        c, err = parse_v9_tool_transport(human.canonical_response())
        assert not err and c[0]['decision']=='INTERRUPT' and c[0]['context_request']['kind']=='USER_INPUT'
        # A persisted pre-change NEED_MORE_CONTEXT draft is re-questioned.
        human.draft.context_request={}; human.draft.state='READY_FOR_VALIDATION'; human._save()
        resumed= NarrativeDecisionBridge.create(expected=exp, source_text='請選擇輸出格式', root=root)
        assert resumed.draft.pending_slot=='context_kind' and not resumed.ready
        # A blocked bridge must reference actual Runtime failure, never a claim.
        exp_blocked = expected(root)
        exp_blocked['narrative_recovery_context']['context_capabilities'] = blocked
        denied = NarrativeDecisionBridge.create(expected=exp_blocked, source_text='讀取被拒絕', root=root)
        denied.accept_reply('NEED_MORE_CONTEXT'); denied.accept_reply('BLOCKED')
        denied.accept_reply('讀取工具實際回報 PermissionError')
        assert denied.draft.pending_slot == 'context_blocker'
        denied.accept_reply('FAKE')
        assert not denied.ready
        denied.accept_reply('DENIED')
        c, err = parse_v9_tool_transport(denied.canonical_response())
        assert not err and c[0]['context_request']['blocking_action_id'] == 'DENIED'
    print('CONTEXT_ACQUISITION_OK')


if __name__=='__main__':
    run()
