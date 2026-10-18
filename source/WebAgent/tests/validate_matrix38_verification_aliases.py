"""Actual native file predicates must not degrade to command exit-code checks."""
import argparse,tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.tools import _evaluate_verification_step
from agent_core.smartagent_protocol import validate_tool_envelope

def run(legacy=False):
    with tempfile.TemporaryDirectory(prefix='matrix38-verify-alias-') as temp:
        root=Path(temp);target=root/'probe.txt';target.write_bytes(b'ACTUAL')
        def evaluate(step):return _evaluate_verification_step(step,working_directory=root)
        absence={'type':'file_exists','path':str(target),'expect':False,'command':f"Test-Path -LiteralPath '{target}'"}
        wrong={'type':'file_contains','path':str(target),'contains':'WRONG','expect':True,'command':f"Get-Content -Raw -LiteralPath '{target}'"}
        a=evaluate(absence);b=evaluate(wrong)
        if legacy:
            assert a['passed'] and b['passed'] and a['action']==b['action']=='run_command',(a,b)
            print('BEFORE: reproduced two false PASS results from ignored type/contains');return
        assert not a['passed'] and a['action']=='file_exists' and a['exists'] is True,a
        assert not b['passed'] and b['action']=='file_contains' and b['text']=='WRONG',b
        assert evaluate({**absence,'expect':True})['passed']
        assert evaluate({**wrong,'contains':'ACTUAL'})['passed']
        assert evaluate({'action':'file_contains','path':str(target),'text':'ACTUAL','expect':True})['passed']
        assert evaluate({'type':'file_exists','path':str(root/'missing.txt'),'expect':False})['passed']
        # T29 Web: admission must use the same alias grammar as the executor,
        # not treat a file predicate without command as empty_command.
        envelope = {'tool':'run_command','action_id':'ALIAS-VERIFY','operation':'VERIFY',
                    'command':"Get-Content -LiteralPath 'probe.txt'",'working_directory':str(root),
                    'verifies_action_id':'ORIGINAL','success_criteria':'probe is ACTUAL'}
        for field in ('action','type'):
            checks = [{field:'file_exists','path':'probe.txt'},
                      {field:'file_contains','path':'probe.txt','text':'ACTUAL'}]
            ok, diagnostic = validate_tool_envelope({**envelope,'verify':checks})
            assert ok, diagnostic
            ok, diagnostic = validate_tool_envelope({**envelope,'verify':[
                {field:'run_command','command':"Remove-Item -Recurse -LiteralPath 'probe.txt'"}]})
            assert not ok, diagnostic
        for check in ({'action':'file_exists','type':'run_command'}, {'type':'unknown'}, {'type':[]}):
            ok, diagnostic = validate_tool_envelope({**envelope,'verify':[check]})
            assert not ok and diagnostic['reason']=='verification_action_invalid', diagnostic
        for malformed in (
            {'action':'file_exists','type':'file_contains','path':str(target)},
            {'type':'file_contains','text':'ACTUAL','contains':'WRONG','path':str(target)},
            {'type':'file_contains','path':str(target)},
            {'type':'file_contains','text':'','path':str(target)},
            {'type':'file_exists','path':''},
            {'type':'file_exists','path':str(target),'expect':'false'},
            {'type':'not_supported','command':"Write-Output 'ACTUAL'"},
        ):
            r=evaluate(malformed);assert not r['passed'] and not r['spec_valid'],r
        with patch.object(Path,'stat',side_effect=PermissionError('T09_ACCESS_DENIED')):
            r=evaluate({'type':'file_exists','path':str(target),'expect':False})
            assert not r['passed'] and r.get('error') and r.get('exists') is None,r
    print('VERIFICATION_ALIAS_NEGATIVE_AND_POSITIVE_PASS')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--legacy',action='store_true');run(p.parse_args().legacy)
