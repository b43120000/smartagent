"""T01 fixed legal actions: real file IO, checked outcome, no web network."""
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tool_result_contract import decode_result, outcome_committable
from agent_core.result_exchange import prepare_tool_result


def read_action(context, path, case):
    context.current_request_id='REQ-T01-'+case
    context.current_task_id='TASK-T01-'+case
    action={'tool':'read_file','action_id':'T01-'+case,'path':str(path)}
    valid,detail=validate_tool_envelope(action)
    assert valid,detail
    read_calls=[];stat_calls=[]
    real_open=Path.open;real_stat=Path.stat
    def read(probe,*args,**kwargs):
        if (args[0] if args else kwargs.get('mode','r'))=='rb':
            read_calls.append(str(probe))
        return real_open(probe,*args,**kwargs)
    def stat(probe,*args,**kwargs):
        stat_calls.append(str(probe));return real_stat(probe,*args,**kwargs)
    with patch.object(Path,'open',read),patch.object(Path,'stat',stat):
        result=context.execute(action)
    row=decode_result(result)
    assert row and row['tool']=='read_file' and row['action_id']==action['action_id'],row
    assert row['request_id']==context.current_request_id and row['task_id']==context.current_task_id,row
    assert stat_calls, 'preflight checker was never executed'
    return action,result,row,read_calls


def run():
    with tempfile.TemporaryDirectory(prefix='matrix38-t01-') as temp:
        root=Path(temp)
        native=WebAgentToolContext(root)
        native._models_registry[native.planner_key]['provider']='native-test'
        fixtures={'UTF8':'T01_UTF8_OK\r\n中文與海鷗🐦\n','EMPTY':'','BOUNDARY':'X'*1_000_000}
        for case,text in fixtures.items():
            p=root/(case+'.txt');p.write_bytes(text.encode('utf-8'))
            action,result,row,calls=read_action(native,p,case)
            assert row['status']=='SUCCEEDED' and row['executed'] and row['data']['content_read'],row
            assert str(p) in calls and row['data']['text']==text
            assert row['data']['bytes_read']==p.stat().st_size
            assert outcome_committable('read_file',row,action['action_id'])
            evidence=WebAgentProtocolLoop._classify_action_evidence(action,result)
            assert evidence['execution_status']=='SUCCEEDED' and evidence['terminal_eligible'],evidence
            if case=='BOUNDARY':
                prepared=prepare_tool_result(action,result,native)
                assert 'LOCAL_REF' in prepared and len(prepared.encode('utf-8'))<32768
                assert not native.take_pending_attachments()
            print('T01',case,'E=PASS V=PASS R=PASS')
        cases=[('MISSING',root/'absent.txt','FAILED','FILE_NOT_FOUND'),
               ('DIRECTORY',root,'FAILED','NOT_A_FILE')]
        big=root/'over_limit.txt';big.write_bytes(b'X'*1_000_001)
        cases.append(('OVER_LIMIT',big,'REJECTED','READ_SIZE_LIMIT'))
        for case,p,status,reason in cases:
            action,result,row,calls=read_action(native,p,case)
            assert row['status']==status and row['reason']==reason and not row['executed'],row
            assert not calls and not outcome_committable('read_file',row,action['action_id'])
            print('T01',case,'E=EXPECTED_NOT_EXECUTED V=PASS R=PASS')
        p=root/'UTF8.txt'
        with patch('agent_core.tools._read_bounded_bytes',side_effect=PermissionError('T01 injected denial')) as checker:
            row=decode_result(native.execute({'tool':'read_file','action_id':'T01-DENIED','path':str(p)}))
        assert checker.called and row['status']=='REJECTED' and row['reason']=='ACCESS_DENIED' and not row['executed'],row
        print('T01','DENIED_UNIT','V=PASS R=PASS')

        # The actual Remote/Web provider branch must not silently send an
        # ordinary small text read into attachment queue instead of content.
        web=WebAgentToolContext(root,interface_name='remote')
        action,result,row,calls=read_action(web,p,'WEB_TEXT')
        assert row['status']=='SUCCEEDED' and row['data'].get('content_read'),json.dumps(row,ensure_ascii=False)
        assert str(p) in calls and row['data']['text']==fixtures['UTF8']
        assert not web.take_pending_attachments(), 'unexpected attachment upload for a small text read'
        print('T01','WEB_TEXT','E=PASS V=PASS R=PASS')
        empty=root/'EMPTY.txt'
        action,result,row,calls=read_action(web,empty,'WEB_EMPTY')
        assert row['status']=='SUCCEEDED' and row['data']['text']=='' and row['data']['bytes_read']==0
        assert str(empty) in calls and not web.take_pending_attachments()
        # Real staging is tested here; this is NOT external upload IT.
        action,result,row,calls=read_action(web,big,'WEB_LARGE')
        assert row['status']=='QUEUED' and not row['data']['content_read']
        assert not outcome_committable('read_file',row,action['action_id'])
        staged=web.take_pending_attachments()
        assert len(staged)==1 and Path(staged[0]).read_bytes()==big.read_bytes()
        evidence=WebAgentProtocolLoop._classify_action_evidence(action,result)
        assert not evidence['terminal_eligible'] and evidence['result_status']=='QUEUED'
        print('T01','WEB_LARGE_STAGE','E=PASS V=PASS R=QUEUED_NOT_DELIVERED')
        from agent_core.attachment_transaction import AttachmentTransaction
        from agent_core.web_attachment_delivery import confirmed_delivery
        tx=AttachmentTransaction(root,clock=lambda:100.0)
        epoch='T01-EPOCH';req=row['request_id']
        record=tx.begin(request_id=req,task_epoch=epoch,conversation_id='T01-CONVERSATION',source_path=staged[0])
        receipt_args=dict(request_id=req,task_epoch=epoch,paths=staged,started_at=99.0,artifacts=row['data']['artifacts'])
        assert not confirmed_delivery(root,**receipt_args), 'STAGED is not delivered'
        for state in ('STAGED','UPLOADING','VISIBLE','READY','STABLE','SUBMITTED'):
            tx.transition(record.attachment_id,state)
        assert not confirmed_delivery(root,**receipt_args), 'SUBMITTED alone is not consumed'
        tx.transition(record.attachment_id,'CONSUMED')
        assert len(confirmed_delivery(root,**receipt_args))==1
        for overrides in ({'request_id':'OTHER'},{'task_epoch':'OTHER'},{'paths':[big]},
                          {'started_at':101.0},{'artifacts':[dict(row['data']['artifacts'][0],sha256='bad')]}):
            assert not confirmed_delivery(root,**{**receipt_args,**overrides})
        loop=WebAgentProtocolLoop(root,lambda *args:'',tool_context=web)
        loop.run_id=req;loop.task_epoch=epoch;loop.task_id='TASK-T01-RECEIPT';loop.turn_id=2
        loop.action_ledger[action['action_id']]={'action':action,'result':result}
        loop.action_result_ledger[action['action_id']]={**evidence,'request_id':req,'task_epoch':epoch}
        with patch('agent_core.paths.source_root',return_value=root):
            loop._reconcile_read_attachment_delivery(staged,99.0,{'task_epoch':epoch})
        updated=decode_result(loop.action_ledger[action['action_id']]['result'])
        assert updated['status']=='SUCCEEDED' and updated['data']['delivery_status']=='DELIVERED_TO_CONVERSATION'
        assert updated['data']['content_read'] is False, 'transport receipt cannot claim local/semantic read'
        assert loop.action_result_ledger[action['action_id']]['terminal_eligible']
        assert loop.action_ledger[action['action_id']]['transport_history'][0]['status']=='QUEUED'
        Path(staged[0]).write_bytes(b'changed after staging')
        assert not confirmed_delivery(root,**receipt_args), 'changed artifact cannot reuse receipt'
        print('T01 WEB_DELIVERY_RECEIPT V=PASS R=PASS')
        pdf=root/'fixture.pdf';pdf.write_bytes(b'%PDF-1.4\nSynthetic ASCII PDF fixture\n%%EOF\n')
        action,result,row,calls=read_action(web,pdf,'WEB_DOCUMENT')
        assert row['status']=='QUEUED' and not row['data']['content_read']
        staged=web.take_pending_attachments()
        assert len(staged)==1 and Path(staged[0]).read_bytes()==pdf.read_bytes()
        failed_context=WebAgentToolContext(root,interface_name='remote')
        with patch.object(failed_context,'queue_attachments',return_value='[WEBAGENT_ATTACHMENT_ERROR] synthetic failure') as stage:
            _,_,row,_=read_action(failed_context,big,'WEB_STAGE_FAIL')
        assert stage.called and row['status']=='FAILED' and row['reason']=='ATTACHMENT_QUEUE_FAILED'
        assert not row['data']['content_read']
        print('T01','WEB_STAGE_FAIL','V=PASS R=PASS')
        for context in (native,web):
            with patch('agent_core.tools._read_bounded_bytes',return_value=b'X'*1_000_001):
                row=decode_result(context.execute({'tool':'read_file','action_id':'T01-GREW','path':str(root/'UTF8.txt')}))
            assert row['status']=='FAILED' and row['reason']=='READ_SIZE_LIMIT_EXCEEDED_DURING_READ' and row['executed'],row
        from io import BytesIO
        from agent_core.tools import _read_bounded_bytes
        from agent_core.project_artifact_policy import _looks_text
        with patch.object(Path,'open',return_value=BytesIO(b'X'*2_000_000)):
            assert len(_read_bounded_bytes(root/'UTF8.txt'))==1_000_001
        class BudgetStream(BytesIO):
            def read(self,size=-1):
                assert size==8192, 'classification sample must be bounded'
                return super().read(size)
        with patch.object(Path,'open',return_value=BudgetStream(b'X'*8191+'中'.encode('utf-8'))):
            assert _looks_text(root/'unknown.extension')
        for case,p,status,reason in cases[:2]:
            action,result,row,calls=read_action(web,p,'WEB_'+case)
            assert row['status']==status and row['reason']==reason and not row['executed'],row
            assert not calls and not web.take_pending_attachments()
    print('MATRIX38_T01_UT_PASS')


if __name__=='__main__':
    run()
