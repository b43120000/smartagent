"""Rejected deletes are not execution success; recovery remains read-only."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_core.tool_result_contract import decode_result, encode_result, delete_target_recovery
from agent_core.safe_file_operations import build_delete_manifest, execute_delete_manifest, SafeFileOperationError
from agent_core.security_config import SecurityConfig
from agent_core.security_context import SecurityContext
from agent_core.action_loop_state import build_action_loop_state
from agent_core.narrative_bridge import NarrativeDecisionBridge
from agent_core.context_acquisition import context_capabilities
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.tests.validate_narrative_v9_bridge import expected


def run():
    with tempfile.TemporaryDirectory(prefix='delete-recovery-') as temp:
        root = Path(temp)
        target = root / 'probe.txt'; target.write_text('KEEP', encoding='utf-8')
        context = WebAgentToolContext(root)
        loop = WebAgentProtocolLoop(root, lambda *a:'', tool_context=context, progress_root=root)
        action = {'tool':'delete_path','action_id':'D-REL','path':'probe.txt','recursive':False,'reason':'test'}
        result = loop._execute_action(action)
        row = decode_result(result)
        assert row['status']=='REJECTED' and row['reason']=='ABSOLUTE_PATH_REQUIRED' and row['executed'] is False
        assert target.read_text(encoding='utf-8')=='KEEP'
        assert context.preview_tool_scope(action)['allowed'] is False
        evidence = loop._classify_action_evidence(action, result)
        assert evidence['execution_status']=='FAILED' and evidence['result_status']=='REJECTED'
        assert evidence['terminal_eligible'] is False and evidence['executed'] is False
        recovery = delete_target_recovery(action, result)
        state = build_action_loop_state(action=action, result=result,
            execution_status=evidence['execution_status'], pending_recovery=recovery)
        assert state['required_transition']=='PREREQUISITE_REQUIRED'
        assert 'delete_path' in state['blocked_actions'] and 'delete_path' not in state['allowed_next_actions']
        assert not context_capabilities([root], ['list_directory'], {'D':{**evidence,'result':result}})['objective_blockers']
        legacy = loop._classify_action_evidence(action, '[SECURITY_DELETE_REJECTED] absolute_path_required:probe.txt')
        assert legacy['execution_status']=='FAILED' and not legacy['terminal_eligible']
        unknown = loop._classify_action_evidence(action, 'looks done')
        assert unknown['execution_status']=='UNKNOWN' and not unknown['terminal_eligible']
        denied = encode_result('REJECTED','PATH_NOT_AUTHORIZED')
        assert context_capabilities([root], ['list_directory'], {'D':{'execution_status':'FAILED','result':denied}})['objective_blockers']
        # No explicit task target does not disable discovery in default workspace.
        loop.authorized_paths=[]
        exp = expected(root); exp['narrative_recovery_context']['context_capabilities']=loop._context_capabilities()
        bridge = NarrativeDecisionBridge.create(expected=exp, source_text='需要定位絕對路徑', root=root)
        bridge.accept_reply('NEED_MORE_CONTEXT'); bridge.accept_reply('RUNTIME_READ')
        assert bridge.draft.pending_slot=='tool'
        bridge.accept_reply('LIST_DIRECTORY'); bridge.accept_reply(str(root))
        assert bridge.ready
        read = {'tool':'list_directory','action_id':'L-RESOLVE','path':str(root)}
        unrelated = root / 'unrelated'; unrelated.mkdir()
        loop._execute_action({**read, 'action_id':'L-UNRELATED', 'path':str(unrelated)})
        assert loop.pending_result_recovery
        assert 'probe.txt' in loop._execute_action(read)
        assert not loop.pending_result_recovery
        assert loop.resolved_delete_target == str(target)
        mismatch = loop._execute_action({**action,'action_id':'D-WRONG','path':str(unrelated)})
        assert decode_result(mismatch)['reason']=='TARGET_MISMATCH' and unrelated.exists()
        assert not context_capabilities([], ['list_directory'], {})['objective_blockers']
        from agent_core.context_acquisition import context_interrupt_allowed
        caps = loop._context_capabilities()
        assert not context_interrupt_allowed({'kind':'USER_INPUT','detail':'請上傳 source code'}, caps)
        assert context_interrupt_allowed({'kind':'USER_INPUT','detail':'請提供專案完整絕對路徑'}, caps)
        from agent_core.tool_result_contract import rejection_reason
        try:
            try: raise PermissionError('blocked')
            except PermissionError as exc: raise SafeFileOperationError('path_resolution_failed') from exc
        except SafeFileOperationError as exc:
            assert rejection_reason(exc)=='ACCESS_DENIED'
        # Approval and target-change guards remain authoritative.
        sc = SecurityContext(SecurityConfig.build(root))
        for value in (str(root), 'probe.txt'):
            try: build_delete_manifest(sc, value, recursive=True)
            except SafeFileOperationError: pass
            else: raise AssertionError('unsafe root/relative target accepted')
        manifest = build_delete_manifest(sc, str(target))
        target.write_text('CHANGED', encoding='utf-8')
        try: execute_delete_manifest(sc, manifest)
        except SafeFileOperationError: pass
        else: raise AssertionError('changed manifest accepted')
        absolute = {**action,'action_id':'D-ABS','path':str(target)}
        assert context.preview_tool_scope(absolute)['resolved_paths'] == [str(target)]
        with patch('agent_core.tools.SecurityApprovalLedger.authorized_workspace_for_target', return_value=str(root)):
            success = context.execute(absolute)
        row = decode_result(success)
        assert row['status']=='SUCCEEDED' and row['executed'] and not target.exists()
        proof = loop._classify_action_evidence(absolute, success)
        assert proof['execution_status']=='SUCCEEDED' and proof['evidence_state']=='UNVERIFIED'
        loop.resolved_delete_target = str(target)
        def stop_new_request(*args):
            assert loop.resolved_delete_target == ''
            raise RuntimeError('NEW_REQUEST_RESET_OK')
        loop.planner = stop_new_request
        try: loop.run('另一個任務，只做唯讀測試')
        except RuntimeError as exc: assert str(exc)=='NEW_REQUEST_RESET_OK', exc
        else: raise AssertionError('test stop missing')
    print('DELETE_RESULT_RECOVERY_OK')


if __name__=='__main__': run()
