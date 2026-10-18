"""Markdown representation is not a different receiver; identity stays strict."""
import copy
import tempfile
from pathlib import Path
from agent_core.project_sync_message import build_atomic_project_sync
from agent_core.project_sync_protocol import ProjectSyncProtocol, ProjectSyncProtocolError, build_batch_ack_contract


def run():
    with tempfile.TemporaryDirectory(prefix='t15-ack-link-') as value:
        root = Path(value).resolve()
        (root / 'a.py').write_bytes(b'print(1)\n')
        plan = build_atomic_project_sync(root, max_bytes=128000, max_files=10)['transaction']
        url = 'https://chatgpt.com/c/t15-synthetic-conversation'
        binding = {'interface_name': 'remote', 'conversation_id': url, 'session_id': 'T15-SESSION'}
        protocol = ProjectSyncProtocol(root, plan, receiver_binding=binding, request_id='T15-REQUEST')
        notification = protocol.next_notification('T15-SEND')
        ack = build_batch_ack_contract(plan, 1, '', binding, 'T15-SEND')
        ack['web_batch_ack_id'] = 'T15-ACK'
        for change in (
            {'conversation_id': f'[{url}](https://chatgpt.com/c/another)'},
            {'conversation_id': 'https://chatgpt.com/c/another'},
            {'session_id': 'OTHER-SESSION'},
            {'interface_name': 'OTHER-INTERFACE'},
        ):
            wrong = copy.deepcopy(ack)
            wrong['receiver_binding'].update(change)
            try:
                protocol.accept_ack(wrong, send_attempt_id='T15-SEND')
            except ProjectSyncProtocolError as exc:
                assert str(exc) == 'project_sync_ack_receiver_binding_mismatch', exc
            else:
                raise AssertionError('wrong receiver accepted')
        wrong = copy.deepcopy(ack)
        wrong['send_attempt_id'] = 'OTHER-SEND'
        try:
            protocol.accept_ack(wrong, send_attempt_id='T15-SEND')
        except ProjectSyncProtocolError as exc:
            assert str(exc) == 'project_sync_ack_send_attempt_mismatch', exc
        else:
            raise AssertionError('wrong send accepted')
        ack['receiver_binding']['conversation_id'] = f'[{url}]({url})'
        protocol.accept_ack(ack, send_attempt_id=notification['payload']['send_attempt_id'])
        assert protocol.state['status'] == 'PROJECT_SYNC_READY'
        assert protocol.state['acknowledged_batches']['1']['receiver_binding'] == binding
    print('T15_ACK_LINK_IDENTITY_PASS')


if __name__ == '__main__':
    run()
