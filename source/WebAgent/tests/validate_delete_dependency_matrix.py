"""Delete/linked verify/dependent write with isolated filesystem+approval fixture."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_action_scoped_admission import finished
from WebAgent.tests.validate_progress_terminal_lifecycle import response
from agent_core.security_approval import SecurityApprovalLedger


def run():
    with tempfile.TemporaryDirectory(prefix='delete-matrix-') as temp:
        root=Path(temp); target=root/'disposable_delete_probe'; marker=root/'delete_verified.txt'
        turns=[]; approvals=[]; commands=[]
        create={'tool':'run_command','action_id':'CREATE','operation':'MUTATE',
            'command':"New-Item -ItemType Directory -Path 'disposable_delete_probe'",'working_directory':str(root),
            'success_criteria':'test directory exists','verify':[{'action':'file_exists','path':'disposable_delete_probe'}]}
        delete={'tool':'delete_path','action_id':'DELETE','path':str(target),'recursive':False,
                'reason':'delete only the fresh empty test directory','depends_on':['CREATE']}
        verify={'tool':'run_command','action_id':'DELETE-VERIFY','operation':'VERIFY',
            'command':"Test-Path -LiteralPath 'disposable_delete_probe'",'working_directory':str(root),
            'success_criteria':'test directory absent','verifies_action_id':'DELETE',
            'verify':[{'action':'file_exists','path':'disposable_delete_probe','expect':False}]}
        write={'tool':'write_file','action_id':'MARKER','path':str(marker),'content':'DELETE_VERIFIED',
            'depends_on':['DELETE-VERIFY']}
        marker_verify={'tool':'run_command','action_id':'MARKER-VERIFY','operation':'VERIFY',
            'command':"Get-Content -LiteralPath 'delete_verified.txt' -Raw",'working_directory':str(root),
            'success_criteria':'marker exists with expected content after verified deletion',
            'verifies_action_id':'MARKER','verify':[
                {'action':'file_contains','path':'delete_verified.txt','text':'DELETE_VERIFIED'},
                {'action':'file_exists','path':'disposable_delete_probe','expect':False}]}
        sequence=[create,delete,verify,write,marker_verify]
        def planner(prompt,expected,attachments):
            turns.append(prompt)
            if len(turns)<=5:
                action=sequence[len(turns)-1]
                if action['action_id']=='MARKER':
                    assert not target.exists() and loop.action_result_ledger['DELETE']['effective_verification_status']=='PASS'
                return response({**initial(),'next_action':action['tool']},action)
            return response(finished(['DELETE','MARKER-VERIFY']),
                {'tool':'final_response','action_id':'FINAL','content':'DELETE_CHAIN_PASS'})
        loop=WebAgentProtocolLoop(root,planner,progress_root=root,
            event_sink=lambda event,**data:print('REJECTED',data.get('diagnostics')) if event=='protocol_rejected' else None)
        loop.tools.security_approval_ledger_root=root
        def approve_fixture(record, manifest):
            # Simulated approval is ONLY for a fresh empty temp test directory.
            assert manifest['target']==str(target) and target.exists() and not list(target.iterdir())
            approvals.append(record['approval_id'])
            SecurityApprovalLedger(root).decide(record['approval_id'],approve=True,chat_id='',actor='unit-test-fixture')
        loop.tools.configure_security_approval(approve_fixture)
        def capture(command,timeout,capture_root=None,**kwargs):
            commands.append(command)
            if command.startswith('New-Item'):
                target.mkdir(); out=str(target)
            else:
                out=str(target.exists())
            return {'exit_code':0,'stdout':out,'stderr':'','timed_out':False}
        with patch('agent_core.tools._run_powershell_capture',side_effect=capture):
            assert loop.run('isolated delete test\n'+str(root))=='DELETE_CHAIN_PASS'
        assert approvals and marker.read_text()=='DELETE_VERIFIED' and not target.exists()
        assert loop.action_result_ledger['DELETE']['execution_status']=='SUCCEEDED'
        assert loop.action_result_ledger['DELETE']['effective_verification_status']=='PASS'
    print('DELETE_DEPENDENCY_MATRIX_PASS')


if __name__=='__main__':run()
