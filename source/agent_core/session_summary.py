"""Native summary producer for minimal WebAgent contexts; no AI invocation."""
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .security_context import SecurityContext
from .tool_result_contract import attach_runtime_result, encode_result


def save_native_summary(agent, payload):
    summary=payload.get('summary')
    if not isinstance(summary,str) or not summary.strip():
        return encode_result('REJECTED','SUMMARY_REQUIRED',tool='save_session_summary')
    context=SecurityContext.from_agent(agent)
    target=context.require_write(context.workspace_root/'.agents'/'agent_session_summary.md')
    lines=['# SmartAgent Project Session Summary',
        'Updated: '+datetime.now(timezone.utc).isoformat(),
        'Request: '+str(getattr(agent,'current_request_id','')),'','## Summary',summary.strip()]
    for field in ('decisions','modified_files','verification','pending','next_steps'):
        values=payload.get(field,[])
        if isinstance(values,str): values=[values]
        if not isinstance(values,list) or not all(isinstance(item,str) for item in values):
            return encode_result('REJECTED','SUMMARY_FIELD_INVALID',tool='save_session_summary',detail=field)
        lines.extend(['','## '+field]+['- '+item for item in values])
    raw=('\n'.join(lines)+'\n').encode('utf-8')
    if len(raw)>64*1024:
        return encode_result('REJECTED','SUMMARY_SIZE_LIMIT',tool='save_session_summary')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
    replaced=False
    try:
        temporary.write_bytes(raw); temporary.replace(target); replaced=True
        observed=target.read_bytes()
        if observed!=raw: raise OSError('summary_readback_mismatch')
        agent.session_summary_saved_this_task=True
        return attach_runtime_result('[成功] 已保存 project session summary: '+str(target),
            'SUCCEEDED','SESSION_SUMMARY_SAVED',tool='save_session_summary',executed=True,
            resolved_path=str(target),data={'file_saved':True,'verification_status':'PASS',
                'size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    except OSError as exc:
        return encode_result('PARTIAL' if replaced else 'FAILED','SUMMARY_WRITE_FAILED',
            tool='save_session_summary',executed=True,resolved_path=str(target),detail=str(exc),
            data={'file_saved':replaced,'verification_status':'FAIL'})
    finally:
        if temporary.exists(): temporary.unlink()
