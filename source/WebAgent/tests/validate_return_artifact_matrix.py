"""T35 real local queue, identity, failure atomicity and completion checks."""
import json,tempfile
from pathlib import Path
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext
from RemoteAgent.telegram_artifacts import prepare_telegram_completion

def run():
    failures=[]
    with tempfile.TemporaryDirectory(prefix='t35-queue-') as temp:
        root=Path(temp);target=root/'probe.txt';target.write_bytes(b'T35_QUEUE_ONLY')
        ctx=WebAgentToolContext(root,interface_name='remote');ctx.current_request_id='T35-REQ';ctx.current_task_id='T35-TASK'
        first=decode_result(ctx.queue_outbound_artifact(str(target)))
        assert first['status']=='QUEUED' and first['executed'] and first['data']['delivery_status']=='NOT_DELIVERED',first
        queued=ctx._outbound_artifacts[0]
        if not all(queued.get(k)==v for k,v in {'request_id':'T35-REQ','task_id':'T35-TASK','artifact_id':queued['sha256']}.items()):
            failures.append('queue record loses request/task/artifact identity')
        again=decode_result(ctx.queue_outbound_artifact(str(target)))
        assert again['status']=='QUEUED' and again['data']['deduplicated'] and len(ctx._outbound_artifacts)==1,again
        assert 'REJECTED' in ctx.queue_outbound_artifact(str(root/'missing.txt'))
        assert 'REJECTED' in ctx.queue_outbound_artifact(str(root.parent/'outside.txt'))
        original=ctx.take_outbound_artifacts()
        ready=prepare_telegram_completion(request='queue only',summary='queued, not delivered',workspace=root,artifacts=original)
        assert len(ready['artifacts'])==1
        if ready['artifacts'][0].get('request_id')!='T35-REQ':failures.append('completion strips queued identity')
        target.write_bytes(b'CHANGED_AFTER_QUEUE')
        invalid=prepare_telegram_completion(request='queue only',summary='',workspace=root,artifacts=original)
        assert not invalid['artifacts'] and invalid['artifact_rejections'],invalid

        # A source disappears after hashing, before queue publication. A retry
        # must not claim deduped QUEUED when no queue entry was ever created.
        target.write_bytes(b'T35_QUEUE_ONLY');ctx2=WebAgentToolContext(root,interface_name='remote')
        native_hash=ctx2._file_sha256
        def disappear(path):
            digest=native_hash(path);path.unlink();return digest
        ctx2._file_sha256=disappear
        try:ctx2.queue_outbound_artifact(str(target))
        except FileNotFoundError:pass
        assert not ctx2._outbound_artifacts
        ctx2._file_sha256=native_hash;target.write_bytes(b'T35_QUEUE_ONLY')
        retried=decode_result(ctx2.queue_outbound_artifact(str(target)))
        if len(ctx2._outbound_artifacts)!=1 or retried['data'].get('deduplicated'):
            failures.append('failed enqueue poisons dedup ledger: retry falsely QUEUED without entry')
    print(json.dumps({'T35_failures':failures}))
    assert not failures,failures
    print('T35_RETURN_ARTIFACT_MATRIX_PASS')
if __name__=='__main__':run()
