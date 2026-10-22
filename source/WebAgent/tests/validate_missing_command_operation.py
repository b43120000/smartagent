"""Regression checks for missing run_command operation diagnostics and repair."""
from WebAgent.protocol_loop import WebAgentProtocolLoop


def run():
    for action in ({'tool': 'run_command', 'command': 'Get-Location'},
                   {'tool': 'run_command', 'command': 'Get-Location', 'operation': 'NOT_VALID'}):
        detail = WebAgentProtocolLoop._run_command_verification_detail(action)
        assert 'invalid=operation:' in detail, detail
    assert not WebAgentProtocolLoop._run_command_verification_detail(
        {'tool': 'run_command', 'operation': 'INSPECT', 'command': 'Get-Location'})
    print('COMMAND_OPERATION_DIAGNOSTICS_PASS')


if __name__ == '__main__':
    run()
