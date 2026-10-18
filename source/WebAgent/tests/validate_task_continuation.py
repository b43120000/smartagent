"""Shared resume/follow-up, durable boundaries, ownership and rendering."""
import copy
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_core.task_continuation import (TaskContinuationStore, ContinuationError,
    parse_continuation, guard_continuation_action, render_task_reply)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_action_scoped_admission import finished
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from WebAgent.controller import build_final_render_prompt
from agent_core.task_state import TaskRecord, TaskStateStore
from agent_core.paths import remote_tasks_path, remote_events_path
from agent_core.json_state_io import write_json_atomic


def rejected(call, expected):
    try:
        call()
    except ContinuationError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError('missing rejection: ' + expected)


def run():
    with tempfile.TemporaryDirectory(prefix='continuation-it-') as temp:
        root = Path(temp)
        store = TaskContinuationStore(root)
        (root / 'negative_probe.txt').write_text('ACTUAL_VALUE')
        mutate = {'tool':'run_command', 'action_id':'ORIGINAL-MUTATE', 'operation':'MUTATE',
            'command':"Write-Output 'ACTUAL_VALUE'", 'working_directory':str(root),
            'success_criteria':'intentional negative check',
            'verify':[{'action':'run_command', 'command':"Get-Content -LiteralPath 'negative_probe.txt' -Raw",
                       'expect_contains':'WRONG_VALUE'}]}
        calls = []
        def capture(command, timeout, capture_root=None, **kwargs):
            calls.append(command)
            return {'exit_code':0, 'stdout':'ACTUAL_VALUE', 'stderr':'', 'timed_out':False}
        rounds = []
        def planner(prompt, expected, attachments):
            rounds.append(prompt)
            if len(rounds) == 1:
                return response({**initial(), 'next_action':'run_command'}, mutate)
            raise RuntimeError('SIMULATED_INTERRUPTION')
        original = WebAgentProtocolLoop(root, planner, progress_root=root,
            continuation_store=store, continuation_owner='local:test',
            event_sink=lambda event, **data: print(event, data.get('diagnostics', '')) if event == 'protocol_rejected' else None)
        with patch('agent_core.tools._run_powershell_capture', side_effect=capture):
            try:
                original.run('probe\n'+str(root), request_id='RR-ORIGINAL', task_id='TASK-ORIGINAL', task_epoch='epoch1')
            except RuntimeError as exc:
                assert 'SIMULATED_INTERRUPTION' in str(exc), str(exc)
        saved = store.get('TASK-ORIGINAL')
        assert saved['state'] == 'INTERRUPTED' and saved['actions']['ORIGINAL-MUTATE']
        assert saved['results']['ORIGINAL-MUTATE']['effective_verification_status'] == 'FAIL'
        assert not saved['prepared']
        wrong = {'tool':'run_command', 'action_id':'VERIFY-WRONG', 'operation':'VERIFY',
            'command':"Get-Content -LiteralPath 'negative_probe.txt' -Raw", 'working_directory':str(root),
            'verifies_action_id':'ORIGINAL-MUTATE', 'success_criteria':'expected negative failure',
            'verify':[{'action':'run_command', 'command':"Get-Content -LiteralPath 'negative_probe.txt' -Raw",
                       'expect_contains':'WRONG_VALUE'}]}
        correct = {**wrong, 'action_id':'VERIFY-CORRECT', 'success_criteria':'actual content matches',
            'verify':[{**wrong['verify'][0], 'expect_contains':'ACTUAL_VALUE'}]}
        rounds.clear(); calls.clear()
        def followup(prompt, expected, attachments):
            rounds.append(prompt)
            if len(rounds) == 1:
                assert 'TASK_CONTINUATION' in prompt and 'ORIGINAL-MUTATE' in prompt
                return response({**initial(), 'next_action':'run_command'}, wrong)
            if len(rounds) == 2:
                return response({**initial(), 'next_action':'run_command'}, correct)
            return response(finished(['VERIFY-CORRECT']),
                {'tool':'final_response', 'action_id':'FINAL', 'content':'HISTORY_PASS'})
        loop = WebAgentProtocolLoop(root, followup, progress_root=root,
            continuation_store=store, continuation_owner='local:test')
        with patch('agent_core.tools._run_powershell_capture', side_effect=capture):
            assert loop.run('後續驗證 TASK-ORIGINAL\n需求：驗證 FAIL→PASS，不得寫入',
                request_id='RR-FOLLOWUP', task_id='TASK-FOLLOWUP', task_epoch='epoch2') == 'HISTORY_PASS'
        assert all("Write-Output" not in command for command in calls), calls
        target = loop.action_result_ledger['ORIGINAL-MUTATE']
        assert target['effective_verification_status'] == 'PASS'
        assert [r['status'] for r in target['verification_history']][-2:] == ['FAIL', 'PASS']
        assert target['source_request_id'] == 'RR-ORIGINAL' and target['request_id'] == 'RR-FOLLOWUP'
        persisted = store.get('TASK-ORIGINAL')
        assert persisted['state'] == 'INTERRUPTED'  # old terminal result is immutable
        assert persisted['results']['ORIGINAL-MUTATE']['effective_verification_status'] == 'PASS'
        assert (root/'negative_probe.txt').read_text() == 'ACTUAL_VALUE'

        # Resume original Progress with a fresh identity; replayed ID is cached,
        # and only the new verifier actually calls the executor.
        resume_turns = []
        resume_exec = []
        def resume_planner(prompt, expected, attachments):
            resume_turns.append(prompt)
            if len(resume_turns) == 1:
                assert 'RR-ORIGINAL' in prompt
                return response({**initial(), 'next_action':'run_command'}, mutate)
            if len(resume_turns) == 2:
                return response({**initial(), 'next_action':'run_command'}, {**correct, 'action_id':'RESUME-VERIFY'})
            return response(finished(['RESUME-VERIFY']),
                {'tool':'final_response','action_id':'RESUME-FINAL','content':'RESUMED'})
        resumed = WebAgentProtocolLoop(root, resume_planner, progress_root=root,
            continuation_store=store, continuation_owner='local:test')
        native_execute = resumed.tools.execute
        def track(action):
            resume_exec.append(action['action_id']); return native_execute(action)
        resumed.tools.execute = track
        with patch('agent_core.tools._run_powershell_capture', side_effect=capture):
            assert resumed.run('接續工作 RR-ORIGINAL', request_id='RR-RESUME',
                task_id='TASK-RESUME', task_epoch='fresh-epoch') == 'RESUMED'
        assert resume_exec == ['RESUME-VERIFY'], resume_exec
        assert resumed.progress_ledger.goal == saved['goal']
        assert resumed.progress_ledger.request_id == 'RR-RESUME'

        # Owner/workspace/instance and ambiguity are not planner decisions.
        command = parse_continuation('接續工作 RR-ORIGINAL')
        rejected(lambda: store.resolve(command, owner='telegram:other:chat', workspace=root, child_task_id='NEW'), 'not_found')
        rejected(lambda: store.resolve(command, owner='local:test', workspace=root/'other', child_task_id='NEW'), 'not_found')
        store.register(task_id='TASK-SECOND-STOPPED', request_id='RR-SECOND-STOPPED', epoch='second',
            goal='second goal', workspace=root, owner='local:test')
        store.start('TASK-SECOND-STOPPED'); store.finish('TASK-SECOND-STOPPED', 'INTERRUPTED')
        rejected(lambda: store.resolve(parse_continuation('接續上一個工作'), owner='local:test', workspace=root, child_task_id='NEW'), 'ambiguous')
        rejected(lambda: store.start('TASK-FOLLOWUP'), 'already_started')

        # Trusted legacy terminal records can be verified, but not replayed.
        old_store = TaskStateStore(remote_tasks_path(root))
        old = TaskRecord(task_id='TASK-LEGACY', request_id='RR-LEGACY', task_epoch='old-epoch',
            state='COMPLETED', request='old probe\n'+str(root), workspace=str(root),
            reply_route={'chat_id':2}, metadata={'transport':'TELEGRAM','sender_id':'1'},
            result_ledger={'final':{'task_outcome':'SUCCESS','action_result_ledger':{'OLD':{
                'action_id':'OLD','tool':'run_command','action_digest':'a'*64,
                'request_id':'RR-LEGACY','task_epoch':'old-epoch','executed':True,
                'execution_status':'SUCCEEDED','verification_status':'FAIL'}}, 'ack_ids':['WEBACK-OLD']}})
        old_store.put(old)
        write_json_atomic(remote_events_path(root), {'events':[{'event_id':'EVT-OLD','task_id':'TASK-LEGACY'}]})
        for ref in ('RR-LEGACY','TASK-LEGACY','EVT-OLD','WEBACK-OLD'):
            child_id='CHILD-'+ref
            store.register(task_id=child_id,request_id='REQUEST-'+ref,epoch=ref,
                goal='check',workspace=root,owner='telegram:1:2')
            resolved = store.resolve(parse_continuation('後續驗證 '+ref+'\n需求：唯讀查核'),
                owner='telegram:1:2',workspace=root,child_task_id=child_id)
            assert resolved['legacy'] and resolved['results']['OLD']['executed'] is True
            # Release test reservation via a real child lifecycle.
            store.start(child_id,source=resolved,mode='FOLLOWUP_VERIFY')
            store.finish(child_id,'INTERRUPTED')
        rejected(lambda: store.resolve(parse_continuation('接續工作 TASK-LEGACY'),
            owner='telegram:1:2',workspace=root,child_task_id='NEW'), 'already_completed')

        # Crash between prepare and execute: no blind replay, even a new ID.
        store.register(task_id='TASK-CRASH', request_id='RR-CRASH', epoch='e3',
            goal='probe\n'+str(root), workspace=root, owner='local:test')
        store.start('TASK-CRASH')
        store.prepare_action('TASK-CRASH', mutate)
        store.finish('TASK-CRASH', 'INTERRUPTED', 'crash')
        crash = store.get('TASK-CRASH')
        rejected(lambda: guard_continuation_action(mutate, crash, 'RESUME'), 'outcome_unknown')
        rejected(lambda: guard_continuation_action({**mutate, 'action_id':'NEW-MUTATE'}, crash, 'RESUME'), 'readonly')
        guard_continuation_action(correct, crash, 'RESUME')
        guard_continuation_action({**correct, 'verify': correct['verify'][0]}, crash, 'RESUME')
        # Web T29 follow-up: executor accepts type aliases, so reconciliation
        # must admit the same read-only predicates without relaxing commands.
        from agent_core.tools import _evaluate_verification_step
        for field in ('action', 'type'):
            checks = [{field:'file_exists', 'path':'negative_probe.txt'},
                      {field:'file_contains', 'path':'negative_probe.txt', 'text':'ACTUAL_VALUE'}]
            guard_continuation_action({**correct, 'verify':checks}, crash, 'FOLLOWUP_VERIFY')
            for check in checks:
                result = _evaluate_verification_step(check, working_directory=root)
                assert result['spec_valid'] and result['passed'], result
            guard_continuation_action({**correct, 'verify':[
                {field:'run_command', 'command':"Get-Content -LiteralPath 'negative_probe.txt'"}]}, crash, 'FOLLOWUP_VERIFY')
            rejected(lambda: guard_continuation_action({**correct, 'verify':[
                {field:'run_command', 'command':"Set-Content -LiteralPath 'negative_probe.txt' 'BAD'"}]}, crash, 'FOLLOWUP_VERIFY'), 'readonly')
        for check in ({'action':'file_exists','type':'run_command','command':"Write-Output 'OK'"},
                      {'action':'run_command','type':'file_exists','path':'negative_probe.txt'},
                      {'type':'unknown'}, {'type':[]}):
            rejected(lambda: guard_continuation_action({**correct,'verify':[check]}, crash, 'FOLLOWUP_VERIFY'), 'readonly')
        rejected(lambda: guard_continuation_action({**correct, 'command':"Set-Content -LiteralPath 'negative_probe.txt' 'BAD'"}, saved, 'FOLLOWUP_VERIFY'), 'readonly')

        # Runtime state, not frozen source text, releases the reconciliation latch.
        probe = WebAgentProtocolLoop(root, lambda *a:'', progress_root=root)
        probe.continuation_source = crash
        probe.continuation_mode = 'RESUME'
        probe.run_id, probe.task_id, probe.task_epoch, probe.task_request = 'RR-PROBE', 'TASK-PROBE', 'e4', crash['goal']
        from agent_core.task_progress import initialize_progress
        probe.progress_ledger = initialize_progress('TASK-PROBE', request_id='RR-PROBE', goal=crash['goal'], root=root)
        probe.pending_command_field_repairs = {}
        probe._restore_continuation()
        assert probe.action_result_ledger['ORIGINAL-MUTATE']['evidence_status'] == 'ACTIVE'
        assert probe._continuation_guard_source()['prepared']
        probe.action_result_ledger['ORIGINAL-MUTATE']['pending_resolution'] = 'SCOPED_VERIFICATION_OBTAINED'
        assert not probe._continuation_guard_source()['prepared']
        guard_continuation_action({**mutate, 'action_id':'REMAINING', 'command':"Write-Output 'NEXT'"},
            probe._continuation_guard_source(), 'RESUME')
        rejected(lambda: guard_continuation_action({**mutate,'action_id':'REPLAY-WITH-NEW-ID'},
            probe._continuation_guard_source(), 'RESUME'), 'duplicate')
        rejected(lambda: guard_continuation_action({**mutate,'action_id':'REPLAY-WITH-CHANGED-VERIFY',
            'success_criteria':'changed words','verify':correct['verify']},
            probe._continuation_guard_source(), 'RESUME'), 'duplicate')
        from agent_core.task_continuation import unresolved_continuation_result
        for status in ('RUNNING','QUEUED'):
            record={'execution_status':status,'pending_resolution':'SCOPED_VERIFICATION_OBTAINED'}
            assert unresolved_continuation_result(record)
            blocked_source={'results':{'PENDING':record},'prepared':{},'actions':{}}
            rejected(lambda: guard_continuation_action({**mutate,'action_id':'ANOTHER'},blocked_source,'RESUME'), 'readonly')

        # A fresh unrelated read cannot hide an unresolved mutation at terminal.
        unknown_turns = []
        rejections = []
        def unknown_planner(prompt, expected, attachments):
            unknown_turns.append(prompt)
            if len(unknown_turns) == 1:
                return response(initial(), {'tool':'list_directory','action_id':'UNRELATED-READ','path':str(root)})
            if len(unknown_turns) == 2:
                return response(finished(['UNRELATED-READ']),
                    {'tool':'final_response','action_id':'FALSE-FINAL','content':'SHOULD_NOT_COMPLETE'})
            raise RuntimeError('STOP_UNKNOWN_TEST')
        unknown = WebAgentProtocolLoop(root, unknown_planner, progress_root=root,
            continuation_store=store, continuation_owner='local:test',
            event_sink=lambda event,**data: rejections.extend(data.get('diagnostics', [])) if event=='protocol_rejected' else None)
        try:
            unknown.run('接續工作 TASK-CRASH',request_id='RR-UNKNOWN-TEST',task_id='TASK-UNKNOWN-TEST',task_epoch='unknown-test')
        except RuntimeError as exc:
            assert str(exc)=='STOP_UNKNOWN_TEST',str(exc)
        else:
            raise AssertionError('unresolved mutation falsely completed')
        assert any(item['reason']=='continuation_execution_reconciliation_required' for item in rejections),rejections

        # Reservation and binding survive as a single transaction. Dead
        # REGISTERED reservations are reclaimed, live ones are not duplicated.
        for child in ('TASK-RESERVATION-A','TASK-RESERVATION-B'):
            store.register(task_id=child, request_id='RR-'+child, epoch=child,
                goal='接續工作 TASK-CRASH', workspace=root, owner='local:test')
        store.prepare_request('TASK-RESERVATION-A', request='接續工作 TASK-CRASH',owner='local:test',workspace=root)
        child = store.get('TASK-RESERVATION-A')
        assert child['source_task_id'] == 'TASK-CRASH' and child['prepared']
        rejected(lambda: store.prepare_request('TASK-RESERVATION-B', request='接續工作 TASK-CRASH',owner='local:test',workspace=root), 'busy')
        with patch('agent_core.process_file_lock._pid_alive', return_value=False):
            store.prepare_request('TASK-RESERVATION-B', request='接續工作 TASK-CRASH',owner='local:test',workspace=root)
        store.finish('TASK-RESERVATION-B','INTERRUPTED')

        # Snapshot failure cannot mask the original failure or strand the lease.
        def failing_cleanup_sink(event, **data):
            if 'cleanup_failed' in event:
                raise RuntimeError('LOGGER_BOOM')
        broken = WebAgentProtocolLoop(root, lambda *a:response({**initial(),'next_action':'run_command'},correct),
            progress_root=root, continuation_store=store, continuation_owner='local:test',event_sink=failing_cleanup_sink)
        with patch.object(store,'snapshot',side_effect=RuntimeError('SNAPSHOT_BOOM')):
            try:
                broken.run('後續驗證 TASK-ORIGINAL\n需求：唯讀驗證', request_id='RR-BROKEN',task_id='TASK-BROKEN',task_epoch='broken')
            except RuntimeError as exc:
                assert str(exc) == 'SNAPSHOT_BOOM', str(exc)
            else:
                raise AssertionError('snapshot failure not surfaced')
        assert store.get('TASK-ORIGINAL')['lease'] == ''
        assert store.get('TASK-BROKEN')['state'] == 'INTERRUPTED'

        # Same cached action never invokes tool executor; changed identity fails.
        replay = WebAgentProtocolLoop(root, lambda *a: '', progress_root=root)
        replay.action_ledger = copy.deepcopy(saved['actions'])
        replay.tools.execute = lambda action: (_ for _ in ()).throw(AssertionError('replayed'))
        replay._execute_action(mutate)
        try:
            replay._execute_action({**mutate, 'command':'different'})
        except RuntimeError as exc:
            assert 'payload mismatch' in str(exc)
        else:
            raise AssertionError('changed cached action accepted')

        common = render_task_reply(original_request='goal', base='base', conclusion='result',
            task_id='TASK-X', request_id='RR-X', state='INTERRUPTED')
        rendered = build_final_render_prompt('result', original_request='goal', base_evaluation='base',
            task_id='TASK-X', request_id='RR-X', state='INTERRUPTED')
        assert '接續工作 TASK-X' in common and '接續工作 TASK-X' in rendered
        assert parse_continuation('列出檔案') is None
        rejected(lambda: parse_continuation('後續驗證 TASK-X'), 'goal_required')
    print('TASK_CONTINUATION_PASS')


if __name__ == '__main__':
    run()
