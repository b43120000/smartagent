"""T02: actual bounded directory IO and truthful failure/scope outcomes."""
import tempfile
from pathlib import Path
from unittest.mock import patch
from WebAgent.tool_context import WebAgentToolContext
from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope


def invoke(context,path,case):
    action={'tool':'list_directory','action_id':'T02-'+case,'path':str(path)}
    valid,detail=validate_tool_envelope(action);assert valid,detail
    observed=[];real_iter=Path.iterdir
    def listing(p):
        observed.append(str(p));return real_iter(p)
    with patch.object(Path,'iterdir',listing):
        raw=context.execute(action)
    result=decode_result(raw);assert result and result['tool']=='list_directory',raw
    return action,raw,result,observed


def run():
    with tempfile.TemporaryDirectory(prefix='matrix38-t02-') as temp:
        root=Path(temp);target=root/'listed';target.mkdir()
        (target/'a.txt').write_bytes(b'T02_ALPHA')
        (target/'中文.txt').write_bytes(b'T02_UTF8_NAME')
        (target/'sub').mkdir();(target/'sub'/'nested_not_listed.txt').write_bytes(b'NO_RECURSE')
        (target/'empty').mkdir()
        context=WebAgentToolContext(root,interface_name='remote')
        context.begin_run('REQ-T02','列出以下目錄第一層：\n'+str(target),[str(target)])
        action,raw,result,seen=invoke(context,target,'NORMAL')
        assert seen==[str(target)] and result['status']=='SUCCEEDED' and result['executed'],result
        text=result['data']['text']
        for name in ('a.txt','中文.txt','sub','empty'):
            assert name in text
        assert 'nested_not_listed.txt' not in text and not context.take_pending_attachments()
        assert WebAgentProtocolLoop._classify_action_evidence(action,raw)['terminal_eligible']
        from agent_core.progress_recovery import read_fact_digest
        _,repeat,_,_=invoke(context,target,'NORMAL-NEW-ID')
        assert read_fact_digest(action,raw)==read_fact_digest(action,repeat), 'identity is not a new filesystem fact'
        (target/'a.txt').write_bytes(b'T02_ALPHA_CONTENT_CHANGED')
        _,changed,_,_=invoke(context,target,'NORMAL-CHANGED')
        assert read_fact_digest(action,raw)!=read_fact_digest(action,changed), 'file size change must be new evidence'
        _,_,empty,seen=invoke(context,target/'empty','EMPTY')
        assert empty['status']=='SUCCEEDED' and empty['data']['text']=='(空目錄)'
        _,_,missing,seen=invoke(context,target/'missing','MISSING')
        assert missing['status']=='FAILED' and missing['reason']=='DIRECTORY_NOT_FOUND' and not missing['executed'],missing
        _,_,file,seen=invoke(context,target/'a.txt','FILE')
        assert file['status']=='REJECTED' and file['reason']=='NOT_A_DIRECTORY' and not file['executed'],file
        _,_,scope,seen=invoke(context,root,'WIDEN')
        assert scope['status']=='REJECTED' and not scope['executed'] and not seen,scope
        with patch.object(Path,'iterdir',side_effect=PermissionError('T02 denied')):
            denied=decode_result(context.execute({'tool':'list_directory','action_id':'T02-DENIED','path':str(target)}))
        assert denied['status']=='REJECTED' and denied['reason']=='ACCESS_DENIED' and not denied['executed'],denied
        real_stat=Path.stat
        for error in (FileNotFoundError('T02 child vanished'),PermissionError('T02 child denied')):
            def fail_child(p,*args,**kwargs):
                if p==target/'a.txt':raise error
                return real_stat(p,*args,**kwargs)
            with patch.object(Path,'stat',fail_child):
                partial=decode_result(context.execute({'tool':'list_directory','action_id':'T02-PARTIAL','path':str(target)}))
            assert partial['status']=='PARTIAL' and partial['reason']=='DIRECTORY_LIST_INCOMPLETE' and partial['executed'],partial
            assert partial['data']['terminal_eligible'] is False
        print('MATRIX38_T02_UT_PASS: first-level/empty/missing/file/denied/scope/no-upload')


if __name__=='__main__':run()
