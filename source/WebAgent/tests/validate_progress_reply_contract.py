"""Fixed-schema, minimal round replies materialize only accepted plan state."""
import tempfile
from pathlib import Path

from agent_core.progress_reply_contract import materialize_progress, reply_contract
from agent_core.task_progress import (initialize_progress, record_model_progress,
    read_progress, validate_model_progress, TaskProgressError, format_prompt_context)
from agent_core.protocol_v8 import validate_model_action, ProtocolV8Error
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_progress_terminal_lifecycle import completed_progress


def patch():
    return {'tool': 'report_progress', 'action_id': 'P-PATCH',
            'progress_update': 'PATCH', 'decision': 'CONTINUE',
            'current_focus': '查看下一個目錄', 'next_action': 'list_directory',
            'decision_reason': '本輪需要更多來源資料', 'evidence_refs': ['RUNTIME_STATUS']}


def run():
    with tempfile.TemporaryDirectory(prefix='progress-reply-') as temp:
        root = Path(temp)
        ledger = initialize_progress('T', request_id='T', goal='檢查專案', root=root)
        assert reply_contract(ledger)['preferred_mode'] == 'FULL'
        try:
            materialize_progress(patch(), ledger)
        except ValueError as exc:
            assert 'first_progress_requires_FULL' in str(exc)
        else:
            raise AssertionError('first-round PATCH accepted')
        initial = completed_progress('P-FULL')
        initial.update(decision='CONTINUE', outcome='PENDING', current_step=0.5,
                       steps=[{'step':1,'desc':'檢查來源','status':'IN_PROGRESS'}],
                       matched_condition='編譯程序仍在執行', next_action='list_directory')
        ledger = record_model_progress('T', initial, request_id='T', goal='檢查專案', round_id=1, root=root)
        assert reply_contract(ledger)['preferred_mode'] == 'PATCH'
        assert '[PROGRESS_REPLY_CONTRACT]' in format_prompt_context(ledger)
        normalized = validate_model_progress(patch(), ledger)
        assert normalized['steps'] == ledger.steps
        assert normalized['base_evaluation'] == ledger.base_evaluation
        assert normalized['completion_contract'] == ledger.completion_contract
        assert normalized['decision_reason'] != ledger.decision_reason
        assert normalized['context_request'] == {} and normalized['outcome'] == 'PENDING'
        detached = materialize_progress(patch(), ledger)
        detached['steps'][0]['status'] = 'COMPLETED'
        assert ledger.steps[0]['status'] == 'IN_PROGRESS'
        for field in ('decision', 'evidence_refs', 'decision_reason', 'current_focus', 'next_action'):
            broken = patch(); del broken[field]
            try:
                validate_model_progress(broken, ledger)
            except TaskProgressError:
                pass
            else:
                raise AssertionError('inherited round field: '+field)
        changed = patch(); changed['total_steps'] = 2
        try:
            validate_model_progress(changed, ledger)
        except TaskProgressError:
            pass
        else:
            raise AssertionError('resized plan without steps')
        terminal = patch(); terminal.update(decision='COMPLETE', outcome='SUCCESS')
        try:
            validate_model_progress(terminal, ledger)
        except TaskProgressError:
            pass
        else:
            raise AssertionError('PATCH bypassed unfinished steps')
        loop = WebAgentProtocolLoop(root, lambda *a:'', progress_root=root)
        loop.run_id = loop.task_id = loop.task_epoch = 'T'
        loop.intent_digest = 'a'*64
        loop.authorized_paths = [str(root)]
        loop.progress_ledger = ledger
        calls = [patch(), {'tool':'list_directory','action_id':'A-READ','path':str(root)},
                 {'tool':'turn_commit','action_count':2}]
        accepted, diagnostics = loop._accept_ack(calls, {'ack_web_ack_id':''})
        assert accepted and not diagnostics, diagnostics
        assert accepted[0]['total_steps'] == 1
        ledger = record_model_progress('T', accepted[0], request_id='T', goal='檢查專案', round_id=2, root=root)
        assert read_progress('T', root).steps == initial['steps']
        assert read_progress('T', root).current_focus == patch()['current_focus']
        # A completed plan still cannot borrow stale terminal evidence/outcome.
        done = record_model_progress('DONE', completed_progress('P-DONE'), request_id='DONE',
                                     goal='檢查專案', round_id=1, root=root)
        invalid_terminal = patch(); invalid_terminal.update(decision='COMPLETE')
        try:
            validate_model_progress(invalid_terminal, done)
        except TaskProgressError:
            pass
        else:
            raise AssertionError('inherited old terminal outcome')
        interrupt = patch(); interrupt.update(decision='INTERRUPT', outcome='UNKNOWN')
        try:
            validate_model_progress(interrupt, ledger)
        except TaskProgressError:
            pass
        else:
            raise AssertionError('inherited old blocker context')
        # Shared Phase-1 terminal: evidence_refs is absent from the model
        # only if the trusted V9 caller explicitly authorizes it.
        # completed_progress() is a FAILED compilation fixture. It is
        # intentionally ineligible for Answer-only, even when the gate is on.
        failed_without_refs = completed_progress('P-FAILED-NO-REFS')
        failed_without_refs.pop('evidence_refs')
        try:
            validate_model_progress(
                failed_without_refs, allow_empty_terminal_candidate=True)
        except TaskProgressError:
            pass
        else:
            raise AssertionError('FAILED terminal omitted required evidence')

        # The positive case is a no-tool text explanation with SUCCESS.
        omitted = completed_progress('P-OMIT')
        omitted.update(
            base_evaluation='Explain RAII requires only a text explanation.',
            steps=[{'step':1,'desc':'Explain RAII','status':'COMPLETED'}],
            current_focus='Answer ready',
            next_action='Reply with explanation',
            completion_contract={
                'success':['RAII explanation provided'],
                'failure':['Explanation unavailable'],
                'in_progress':['Preparing RAII explanation'],
                'interrupted':['Cannot continue explaining'],
            },
            outcome='SUCCESS',
            matched_condition='RAII explanation provided',
            decision_reason='Text-only explanation with no Tool actions',
        )
        omitted.pop('evidence_refs')
        try:
            validate_model_progress(omitted)
        except TaskProgressError:
            pass
        else:
            raise AssertionError('evidence omission admitted without Runtime authorization')
        checked = validate_model_progress(
            omitted, allow_empty_terminal_candidate=True)
        assert checked['decision'] == 'COMPLETE'
        assert checked['outcome'] == 'SUCCESS'
        assert checked['evidence_refs'] == []
        assert 'evidence_refs' not in omitted
        explicit = {**omitted, 'evidence_refs':['RUNTIME_STATUS']}
        assert validate_model_progress(
            explicit, allow_empty_terminal_candidate=True,
        )['evidence_refs'] == ['RUNTIME_STATUS']
        assert 'evidence_refs' in reply_contract(ledger)['normally_required']
        assert 'answer_only_terminal' in reply_contract(ledger)['conditional']
        validate_model_action(patch())
        try:
            validate_model_action({'tool':'run_command','action_id':'NO-COMMAND','progress_update':'PATCH'})
        except ProtocolV8Error:
            pass
        else:
            raise AssertionError('PATCH exempted operational fields')
    print('PROGRESS_REPLY_CONTRACT_OK')


if __name__ == '__main__':
    run()
