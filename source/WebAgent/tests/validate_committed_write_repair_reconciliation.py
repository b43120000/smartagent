"""Closed single-chunk proof resolves stale repairs without replaying bytes."""
import copy
import hashlib
import json
import tempfile
from pathlib import Path

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_flexible_progress_admission import initial
from WebAgent.tests.validate_progress_terminal_lifecycle import response


def run():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);dest=root/'proof.txt';text='PROVEN\n中文\n';wid='WRITE-RECONCILE'
        chunk={'tool':'write_file_chunk','write_id':wid,'content':text}
        commit={'tool':'commit_file_write','write_id':wid}
        goal='目的檔與指定內容相符'
        progress={**initial(),'next_action':'begin_file_write',
            'completion_contract':{'success':[goal],'failure':['失敗'],'in_progress':['進行中'],'interrupted':['阻塞']},
            'matched_condition':'進行中'}
        actions=[{'tool':'begin_file_write','action_id':'NEW-BEGIN','write_id':wid,'path':str(dest),'encoding':'utf-8','overwrite':False},
                 {**chunk,'action_id':'NEW-CHUNK'},{**commit,'action_id':'NEW-COMMIT'},
                 {'tool':'run_command','action_id':'VERIFY','operation':'VERIFY','command':"Get-Content -LiteralPath 'proof.txt' -Raw",
                  'working_directory':str(root),'verifies_action_id':'NEW-COMMIT','success_criteria':goal,
                  'verify':[{'action':'file_contains','path':'proof.txt','text':'PROVEN','expect':True}]}]
        turns=[]
        def planner(prompt,expected,attachments):
            turns.append(prompt);n=len(turns)
            if n==1:
                loop.deferred_action_repairs={key:{'action_id':key,'executed':False,'action':{**body,'action_id':key},'diagnostics':[{'reason':'action_dependency_unsatisfied'}]}
                    for key,body in [('OLD-CHUNK',chunk),('OLD-COMMIT',commit)]}
            if n<=len(actions):return response({**progress,'next_action':actions[n-1]['tool']},actions[n-1])
            assert not loop.deferred_action_repairs
            return response({**progress,'decision':'COMPLETE','outcome':'SUCCESS','current_step':1,
                'steps':[{'step':1,'desc':'寫入及驗證','status':'COMPLETED'}],
                'matched_condition':goal,'evidence_refs':['NEW-COMMIT','VERIFY']},
                {'tool':'final_response','action_id':'FINAL','content':'RECONCILE_PASS'})
        loop=WebAgentProtocolLoop(root,planner,progress_root=root)
        assert loop.run('只寫入並驗證\n'+str(dest)+'\n'+str(root))=='RECONCILE_PASS'
        assert dest.read_bytes()==text.encode()
        assert set(loop.action_ledger)=={'NEW-BEGIN','NEW-CHUNK','NEW-COMMIT','VERIFY'}
        archive=loop.action_ledger['NEW-COMMIT']['resolved_admission_repairs']
        assert {r['action_id'] for r in archive}=={'OLD-CHUNK','OLD-COMMIT'}
        assert all(r['executed'] is False and r['status']=='SUPERSEDED' for r in archive)
        # Negative proofs must keep repairs pending; no safety/identity guessing.
        base_ledger=copy.deepcopy(loop.action_ledger);base_evidence=copy.deepcopy(loop.action_result_ledger)
        receipt={'state':'COMMITTED','write_id':wid,'received_chunks':1,'received_bytes':len(text.encode()),'output_sha256':hashlib.sha256(text.encode()).hexdigest()}
        for variant in ('multi','wrong_hash','wrong_request','duplicate','failed','other_write'):
            loop.action_ledger=copy.deepcopy(base_ledger);loop.action_result_ledger=copy.deepcopy(base_evidence)
            pending={**chunk,'action_id':'PENDING'}
            if variant=='other_write':pending['write_id']='OTHER'
            loop.deferred_action_repairs={'PENDING':{'action_id':'PENDING','executed':False,'action':pending}}
            body=dict(receipt);producer={'status':'SUCCEEDED','executed':True}
            if variant=='multi':body['received_chunks']=2
            if variant=='wrong_hash':body['output_sha256']='0'*64
            if variant=='wrong_request':loop.action_result_ledger['NEW-CHUNK']['request_id']='OTHER'
            if variant=='duplicate':loop.deferred_action_repairs['PENDING2']={'executed':False,'action':dict(pending)}
            if variant=='failed':producer['status']='FAILED'
            loop.action_ledger['NEW-COMMIT']['result']='[CHUNKED_WRITE_SUCCESS] '+json.dumps(body)
            loop._reconcile_committed_write_repairs(actions[2],producer)
            assert 'PENDING' in loop.deferred_action_repairs,variant
    print('COMMITTED_WRITE_REPAIR_RECONCILIATION_PASS')


if __name__=='__main__':run()
