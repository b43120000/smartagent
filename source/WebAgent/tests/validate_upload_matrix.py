"""T32/T33 actual staging plus Runtime-owned delivery evidence, no network."""
import tempfile
from pathlib import Path
from unittest.mock import patch
from agent_core.attachment_transaction import AttachmentTransaction
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext


def run(tool):
    with tempfile.TemporaryDirectory(prefix=tool+'-') as value:
        root=Path(value).resolve()
        files=[root/'one.txt',root/'two.txt']
        for n,p in enumerate(files):p.write_text('UNIQUE_'+str(n),encoding='utf-8')
        ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.configure_web_file_capabilities(lambda:{'browser_upload':True,'browser_download':True})
        ctx.begin_run('UPLOAD-TEST','Upload only\n'+str(root),[str(root)])
        action=dict(tool=tool,action_id='UPLOAD-ACT',**({'path':str(files[0])} if tool=='upload_file' else {'paths':[str(p) for p in files]}))
        raw=ctx.execute(action);out=decode_result(raw)
        assert out['status']=='QUEUED' and out['executed'],out
        assert out['tool']==tool and out['action_id']==action['action_id'],out
        # A sibling action dedupes against the pending batch, but still needs
        # the original staged identity so its receipt can resolve independently.
        sibling={**action,'action_id':'UPLOAD-SIBLING'}
        sibling_raw=ctx.execute(sibling);sibling_out=decode_result(sibling_raw)
        assert sibling_out['status']=='QUEUED' and sibling_out['data']['queued_count']==0
        assert sibling_out['data']['artifacts']==out['data']['artifacts']
        staged=ctx.take_pending_attachments()
        count=1 if tool=='upload_file' else 2
        assert len(staged)==count
        assert [Path(p).read_bytes() for p in staged]==[p.read_bytes() for p in files[:count]]
        loop=WebAgentProtocolLoop(root,lambda *args:'',tool_context=ctx)
        loop.run_id='UPLOAD-TEST';loop.task_epoch='EPOCH';loop.task_id='TASK-UPLOAD';loop.turn_id=2
        evidence=loop._classify_action_evidence(action,raw)
        loop.action_ledger[action['action_id']]={'action':action,'result':raw}
        loop.action_result_ledger[action['action_id']]={**evidence,'request_id':loop.run_id,'task_epoch':loop.task_epoch}
        loop.action_ledger[sibling['action_id']]={'action':sibling,'result':sibling_raw}
        loop.action_result_ledger[sibling['action_id']]={**loop._classify_action_evidence(sibling,sibling_raw),
            'request_id':loop.run_id,'task_epoch':loop.task_epoch}
        tx=AttachmentTransaction(root,clock=lambda:100.0)
        records=[tx.begin(request_id=loop.run_id,task_epoch=loop.task_epoch,conversation_id='CONVERSATION',source_path=p) for p in staged]
        with patch('agent_core.paths.source_root',return_value=root):
            loop._reconcile_read_attachment_delivery(staged,99.0,{'task_epoch':loop.task_epoch})
            assert decode_result(loop.action_ledger[action['action_id']]['result'])['status']=='QUEUED'
            for index,record in enumerate(records):
                for state in ('STAGED','UPLOADING','VISIBLE','READY','STABLE','SUBMITTED','CONSUMED'):
                    tx.transition(record.attachment_id,state)
                if len(records)>1 and index==0:
                    loop._reconcile_read_attachment_delivery(staged,99.0,{'task_epoch':loop.task_epoch})
                    assert decode_result(loop.action_ledger[action['action_id']]['result'])['status']=='QUEUED', 'one receipt cannot complete two files'
            loop._reconcile_read_attachment_delivery(staged,99.0,{'task_epoch':'WRONG'})
            assert decode_result(loop.action_ledger[action['action_id']]['result'])['status']=='QUEUED'
            loop._reconcile_read_attachment_delivery(staged,99.0,{'task_epoch':loop.task_epoch})
        updated=decode_result(loop.action_ledger[action['action_id']]['result'])
        assert updated['status']=='SUCCEEDED',updated
        assert len(updated['data']['delivery_receipts'])==count and not updated['data']['content_read']
        assert loop.action_ledger[action['action_id']]['transport_history'][0]['status']=='QUEUED'
        sibling_final=decode_result(loop.action_ledger[sibling['action_id']]['result'])
        assert sibling_final['status']=='SUCCEEDED' and sibling_final['action_id']==sibling['action_id'],sibling_final
        assert sibling_final['data']['delivery_receipts']==updated['data']['delivery_receipts']
        # Staging failure and duplicate handling must not claim delivery.
        ctx.begin_run('UPLOAD-NEG','Upload only\n'+str(root),[str(root)])
        bad=ctx.execute(dict(tool=tool,action_id='BAD',**({'path':str(root/'absent.txt')} if tool=='upload_file' else {'paths':[str(files[0]),str(root/'absent.txt')]})))
        result=decode_result(bad)
        assert result['status'] in {'FAILED','PARTIAL','REJECTED'},result
        assert result['data'].get('delivery_status')!='DELIVERED_TO_CONVERSATION'
        if tool=='upload_files':
            assert result['status']=='PARTIAL' and len(result['data']['errors'])==1,result
            partial_paths=ctx.take_pending_attachments()
            assert len(partial_paths)==1
            partial_action=dict(tool=tool,action_id='BAD',paths=[str(files[0]),str(root/'absent.txt')])
            loop.run_id='UPLOAD-NEG';loop.task_epoch='PARTIAL-EPOCH'
            loop.action_ledger={'BAD':{'action':partial_action,'result':bad}}
            loop.action_result_ledger={'BAD':{**loop._classify_action_evidence(partial_action,bad),
                'request_id':loop.run_id,'task_epoch':loop.task_epoch}}
            tx=AttachmentTransaction(root,clock=lambda:110.0)
            rec=tx.begin(request_id=loop.run_id,task_epoch=loop.task_epoch,conversation_id='PARTIAL-CONVERSATION',source_path=partial_paths[0])
            for state in ('STAGED','UPLOADING','VISIBLE','READY','STABLE','SUBMITTED','CONSUMED'):
                tx.transition(rec.attachment_id,state)
            with patch('agent_core.paths.source_root',return_value=root):
                loop._reconcile_read_attachment_delivery(partial_paths,109.0,{'task_epoch':loop.task_epoch})
            partial=decode_result(loop.action_ledger['BAD']['result'])
            assert partial['status']=='PARTIAL' and partial['data']['delivery_status']=='PARTIAL_DELIVERY',partial
            assert partial['data']['errors']==result['data']['errors'] and len(partial['data']['delivery_receipts'])==1
            assert not loop.action_result_ledger['BAD']['effective_terminal_eligible']
            # Unrelated path/SHA cannot borrow the consumed receipt.
            from agent_core.web_attachment_delivery import confirmed_delivery
            assert not confirmed_delivery(root,request_id=loop.run_id,task_epoch=loop.task_epoch,
                paths=partial_paths,started_at=109.0,
                artifacts=[{**result['data']['artifacts'][0],'sha256':'0'*64}])
            dup=decode_result(ctx.execute(dict(tool=tool,action_id='DUP',paths=[str(files[0]),str(files[0])])))
            assert dup['data']['queued_count']==1 and len(dup['data']['duplicates'])==1,dup
            assert len(ctx.take_pending_attachments())==1
    print(tool.upper()+'_MATRIX_PASS')


if __name__=='__main__':
    import sys
    run(sys.argv[1])
