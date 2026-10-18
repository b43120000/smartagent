"""T34 saved-result integrity, rejection and producer exception contracts."""
import hashlib,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agent_core.tools import execute_tool
from agent_core.security_context import SecurityContext
from agent_core.tool_result_contract import decode_result
from WebAgent.tool_context import WebAgentToolContext

def run():
    with tempfile.TemporaryDirectory(prefix='t34-contract-') as temp:
        root=Path(temp);target=root/'fresh.png';target.write_bytes(b'KNOWN_TEST_BYTES')
        before=target.read_bytes();ctx=WebAgentToolContext(root,interface_name='remote')
        ctx.current_request_id='T34-REQUEST';ctx.current_task_id='T34-TASK'
        ctx.artifact_delivery_mode='telegram'
        manager=SimpleNamespace()
        def call(result):
            manager.download_latest_artifact=lambda *a,**k:result
            return decode_result(execute_tool({'tool':'download_artifact','action_id':'T34-DOWNLOAD',
                'output_path':str(target)},ctx))
        with patch('agent_core.tools.SecurityContext.from_agent',return_value=SecurityContext.for_workspace(root)), \
             patch('agent_core.web_runtime.get_manager',return_value=manager):
            good={'status':'ARTIFACT_DOWNLOAD_SUCCESS','path':str(target),'artifact_id':'T34-IMAGE',
                  'sha256':hashlib.sha256(before).hexdigest(),'size':len(before)}
            row=call(good)
            assert row['status']=='SUCCEEDED' and row['executed'] and row['data']['file_saved'],row
            assert row['request_id']=='T34-REQUEST' and row['action_id']=='T34-DOWNLOAD',row
            assert row['data']['delivery_status']=='NOT_DELIVERED',row
            assert row['data']['verification_status']=='PASS',row
            ctx.take_outbound_artifacts()
            for result,error in (({**good,'sha256':'0'*64},'ARTIFACT_CONTENT_HASH_MISMATCH'),
                                 ({**good,'size':len(before)+1},'ARTIFACT_CONTENT_SIZE_MISMATCH'),
                                 ({**good,'path':str(root/'missing.png')},None),
                                 ({'status':'ARTIFACT_DOWNLOAD_FAILED','error':'ORIGINAL_FETCH_FAILURE'},None)):
                row=call(result)
                assert row['status']=='FAILED' and not row['data']['file_saved'],row
                assert not ctx.take_outbound_artifacts()
                if error:assert row['data']['artifact']['error']==error,row
                assert target.read_bytes()==before
            row=call({k:v for k,v in good.items() if k not in {'sha256','size'}})
            assert row['data']['verification_status']=='UNVERIFIED',row
            ctx.take_outbound_artifacts()
            def fail(*a,**k):raise PermissionError('T34_SAVE_DENIED')
            manager.download_latest_artifact=fail
            row=decode_result(execute_tool({'tool':'download_artifact','output_path':str(target)},ctx))
            assert row['status']=='FAILED' and row['executed'] and 'T34_SAVE_DENIED' in str(row),row
            assert target.read_bytes()==before
            def partial(*a,**k):
                target.write_bytes(b'PARTIAL_BYTES')
                raise OSError('T34_AFTER_WRITE_FAILURE')
            manager.download_latest_artifact=partial
            row=decode_result(execute_tool({'tool':'download_artifact','output_path':str(target)},ctx))
            assert row['status']=='PARTIAL' and row['executed'] and row['data']['output_changed'],row
            assert not row['data']['file_saved'] and not ctx.take_outbound_artifacts(),row
            directory=root/'directory';directory.mkdir()
            manager.download_latest_artifact=fail
            row=decode_result(execute_tool({'tool':'download_artifact','output_path':str(directory)},ctx))
            assert row['status']=='UNKNOWN' and row['executed'] and row['data']['output_changed'] is None,row
            # File can disappear during persisted hash/size verification too,
            # after the producer has already returned success.
            target.write_bytes(before);ctx.artifact_delivery_mode=''
            native_hash=ctx._file_sha256;calls=[]
            def disappear_after_hash(path):
                digest=native_hash(path);calls.append(path)
                if len(calls)==2:path.unlink()  # pre-snapshot first, saved hash second
                return digest
            ctx._file_sha256=disappear_after_hash
            row=call(good)
            assert row['executed'] and row['status'] in ('PARTIAL','UNKNOWN') and not row['data']['file_saved'],row
    print('T34_DOWNLOAD_ARTIFACT_CONTRACT_PASS')
if __name__=='__main__':run()
