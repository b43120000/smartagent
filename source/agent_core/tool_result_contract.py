"""Runtime-owned outcome envelope; display text is not success proof."""
import json
import re
from pathlib import Path, PureWindowsPath

SCHEMA = 'SMARTAGENT_TOOL_RESULT_V1'
STATUSES = {'SUCCEEDED', 'FAILED', 'REJECTED', 'UNKNOWN', 'QUEUED', 'RUNNING', 'PARTIAL'}


def encode_result(status, reason, *, executed=False, requested_path='', resolved_path=None, data=None, detail='', tool='delete_path'):
    if status not in STATUSES or type(executed) is not bool:
        raise ValueError('invalid_tool_result')
    payload = dict(schema=SCHEMA, status=status, reason=reason, executed=executed,
                   requested_path=str(requested_path), resolved_path=resolved_path,
                   data=data or {}, detail=str(detail), tool=tool)
    marker = 'SECURITY_DELETE_SUCCESS' if status == 'SUCCEEDED' else 'SECURITY_DELETE_REJECTED' if status == 'REJECTED' else 'SECURITY_DELETE_FAILED'
    if tool != 'delete_path':
        marker = 'TOOL_RESULT'
        payload['tool'] = tool
    return '['+marker+'] '+json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def rejection_reason(detail):
    cause = detail
    seen = set()
    while isinstance(cause, BaseException) and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, PermissionError):
            return 'ACCESS_DENIED'
        cause = cause.__cause__ or cause.__context__
    text = str(detail)
    if 'absolute_path_required' in text:
        return 'ABSOLUTE_PATH_REQUIRED'
    if 'not_found' in text:
        return 'TARGET_NOT_FOUND'
    if 'path_outside' in text or 'not_authorized' in text:
        return 'PATH_NOT_AUTHORIZED'
    if 'PermissionError' in text or '拒絕存取' in text:
        return 'ACCESS_DENIED'
    return 'DELETE_PREFLIGHT_REJECTED'


def decode_result(result):
    text = str(result or '').strip()
    body = re.sub(r'^\[(?:SECURITY_DELETE_(?:SUCCESS|REJECTED|FAILED)|CHUNKED_WRITE_(?:SUCCESS|FAILED)|TOOL_RESULT)\]\s*', '', text)
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = {}
    if isinstance(payload, dict) and isinstance(payload.get('_runtime_result'), dict):
        payload = payload['_runtime_result']
    # Text producers retain their human-readable transcript; the final line is
    # appended by Runtime, never inferred from the model's prose.
    if not (isinstance(payload, dict) and payload.get('schema') == SCHEMA):
        trailer = re.search(r'\n\[RUNTIME_TOOL_RESULT\] (\{[^\r\n]*\})\s*$', text)
        if trailer:
            try:
                payload = json.loads(trailer.group(1))
            except ValueError:
                payload = {}
    if isinstance(payload, dict) and payload.get('schema') == SCHEMA:
        if payload.get('status') not in STATUSES or type(payload.get('executed')) is not bool:
            return {'status':'UNKNOWN', 'reason':'INVALID_RESULT_ENVELOPE', 'executed':False}
        if not isinstance(payload.get('reason'), str) or not isinstance(payload.get('data'), dict):
            return {'status':'UNKNOWN', 'reason':'INVALID_RESULT_ENVELOPE', 'executed':False}
        if payload['status'] == 'REJECTED' and payload['executed']:
            return {'status':'UNKNOWN', 'reason':'REJECTED_WITH_EXECUTION', 'executed':False}
        if payload['status'] == 'SUCCEEDED' and not payload['executed']:
            return {'status':'UNKNOWN', 'reason':'SUCCESS_WITHOUT_EXECUTION', 'executed':False}
        return payload
    if re.match(r'^\[(?:SECURITY_\w+_REJECTED|TOOL_SCOPE_REJECTED|SECURITY_CONFIRMATION_REQUIRED)\]', text):
        return {'status':'REJECTED', 'reason':rejection_reason(text), 'executed':False, 'detail':text}
    if text.startswith('[SECURITY_DELETE_SUCCESS]') and isinstance(payload, dict) and payload.get('status') == 'DELETED':
        return {'status':'SUCCEEDED', 'reason':'DELETED', 'executed':True, 'data':payload}
    return None


def attach_runtime_result(result, status, reason, *, tool, executed=False,
                          data=None, requested_path='', resolved_path=None,
                          request_id='', task_id='', action_id=''):
    """Add an authoritative outcome without moving business fields/schema.

    JSON-backed tools keep status, snapshot/ACK and query/plan fields at their
    original positions. Text producers keep their transcript and add a trailer.
    """
    if status not in STATUSES or type(executed) is not bool:
        raise ValueError('invalid_tool_result')
    outcome = dict(schema=SCHEMA, tool=tool, status=status, reason=reason,
        executed=executed, requested_path=str(requested_path), resolved_path=resolved_path,
        request_id=str(request_id), task_id=str(task_id), action_id=str(action_id), data=data or {})
    if status == 'SUCCEEDED' and not executed:
        raise ValueError('success_without_execution')
    text = str(result)
    text = re.sub(r'\n\[RUNTIME_TOOL_RESULT\] \{[^\r\n]*\}\s*$', '', text)
    prefix = re.match(r'^(\[(?:TOOL_RESULT|SECURITY_DELETE_(?:SUCCESS|FAILED|REJECTED)|CHUNKED_WRITE_(?:SUCCESS|FAILED))\])\s*', text)
    common_body = text[prefix.end():] if prefix else text
    try:
        business = json.loads(common_body)
    except (ValueError, TypeError):
        business = None
    if isinstance(business, dict):
        if business.get('schema') == SCHEMA:
            business.update(outcome)
            return (prefix.group(1)+' ' if prefix else '') + json.dumps(business, ensure_ascii=False, separators=(',', ':'))
        business['_runtime_result'] = outcome
        return (prefix.group(1)+' ' if prefix else '') + json.dumps(business, ensure_ascii=False, separators=(',', ':'))
    return text + '\n[RUNTIME_TOOL_RESULT] ' + json.dumps(outcome, ensure_ascii=False, separators=(',', ':'))


def outcome_committable(tool, outcome, action_id=''):
    """One commit rule for stage, protocol and bundle; prose is never proof."""
    if not outcome or outcome.get('executed') is not True:
        return False
    if outcome.get('tool') != tool:
        return False
    if action_id and outcome.get('action_id') and action_id != outcome['action_id']:
        return False
    status = outcome.get('status')
    data = outcome.get('data') or {}
    if status == 'FAILED':
        return (tool=='run_command' and data.get('process_state')=='EXITED'
            and type(data.get('exit_code')) is int and data['exit_code'] != 0
            and data.get('verification_status')=='FAIL' and data.get('expectation_status')=='PASS')
    if status != 'SUCCEEDED':
        return False
    verification = data.get('verification_status', '')
    if (tool == 'run_command' and data.get('expectation_status') == 'PASS'
            and data.get('expected_verification') and data.get('process_state') == 'EXITED'
            and data.get('exit_code') == 0 and verification == 'FAIL'):
        return True  # test assertion passed; original postcondition still FAIL
    if (tool == 'run_command' and data.get('operation') in {'INSPECT','GIT_INSPECT'}
            and data.get('observation_confirmed') is True
            and data.get('process_state') == 'EXITED' and data.get('exit_code') == 0
            and verification in {'UNVERIFIED','UNKNOWN'}):
        return True  # completed observation, not verified mutation
    if verification in {'FAIL','SPEC_INVALID'}:
        return False
    if tool in {'run_command','web_edit_file'} and verification != 'PASS':
        return False
    return True


def outcome_terminal_eligible(tool, outcome, action_id=''):
    """Action completion is not Task completion (e.g. accepted write chunk)."""
    return outcome_committable(tool, outcome, action_id) and (outcome.get('data') or {}).get('terminal_eligible') is not False


def delete_target_recovery(action, result, roots=()):
    row = decode_result(result)
    if action.get('tool') != 'delete_path' or not row or row.get('status') != 'REJECTED':
        return {}
    if row.get('reason') not in {'ABSOLUTE_PATH_REQUIRED', 'TARGET_NOT_FOUND'}:
        return {}
    return {'active':True, 'reason':'delete_target_requires_resolution',
            'blocked_tool':'delete_path', 'blocking_reason':row['reason'],
            'missing_content_paths':[], 'missing_fields':['absolute_target_path'],
            'prerequisite_satisfied':False, 'forbidden_actions':['delete_path'],
            'requested_target':str(action.get('path', '')), 'discovery_roots':[str(p) for p in roots],
            'next_actions':[{'tool':'list_directory'}, {'tool':'query_project'}]}


def resolved_delete_target(recovery, action, result):
    """Only exact target evidence from a bounded parent listing can unlock it."""
    if action.get('tool') != 'list_directory':
        return ''
    parent = Path(str(action.get('path', '')))
    if not parent.is_absolute():
        return ''
    parent = parent.resolve()
    from .path_security import is_within
    if not any(is_within(parent, Path(root).resolve()) for root in recovery.get('discovery_roots', [])):
        return ''
    requested = PureWindowsPath(recovery.get('requested_target', ''))
    parts = requested.parts
    if not parts or '..' in parts:
        return ''
    name = parts[-1]
    if requested.is_absolute():
        if str(parent).casefold() != str(requested.parent).casefold():
            return ''
    elif len(parts) > 1 and tuple(p.casefold() for p in parent.parts[-len(parts)+1:]) != tuple(p.casefold() for p in parts[:-1]):
        return ''
    outcome = decode_result(result)
    if outcome:
        if outcome.get('status') != 'SUCCEEDED':
            return ''
        result = (outcome.get('data') or {}).get('text', '')
    entry = re.compile(r'^\[(?:DIR|FILE)\]\s+'+re.escape(name)+r'(?: \([0-9.]+KB\))?$')
    if not any(entry.fullmatch(line.strip()) for line in str(result).splitlines()):
        return ''
    target = parent / name
    return str(target) if target.exists() else ''


def adapt_legacy_result(tool, result, requested_path=''):
    """Only known deterministic producer formats are mapped to success."""
    text = str(result)
    if decode_result(text):
        return text
    if text.startswith('[錯誤]'):
        reason = rejection_reason(text)
        if reason == 'PATH_NOT_AUTHORIZED' or any(code in text for code in ('alternate_data_stream_forbidden', 'parent_traversal_forbidden', 'glob_path_forbidden')):
            status = 'REJECTED'
        else:
            status, reason = 'FAILED', 'FILESYSTEM_OPERATION_FAILED'
    elif tool == 'write_file' and text.startswith('[成功]'):
        status, reason = 'SUCCEEDED', 'FILE_WRITTEN'
    elif tool == 'list_directory' and (text == '(空目錄)' or all(line.startswith(('[DIR]', '[FILE]')) for line in text.splitlines()) and text):
        status, reason = 'SUCCEEDED', 'DIRECTORY_LISTED'
    elif tool == 'find_file' and (text.startswith('[未找到]') or (text and all(Path(line).is_absolute() for line in text.splitlines()))):
        status, reason = 'SUCCEEDED', 'SEARCH_COMPLETED'
    else:
        status, reason = 'UNKNOWN', 'UNRECOGNIZED_LEGACY_RESULT'
    return encode_result(status, reason, executed=status in {'SUCCEEDED', 'FAILED'},
                         requested_path=requested_path, data={'text':text}, tool=tool)
