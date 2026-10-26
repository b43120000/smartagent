"""Runtime-owned capability and blocking evidence for missing-context recovery.

No action is generated or executed here. The planner still selects a canonical
read operation, whose existing scope/permission checks remain authoritative.
"""
import re

CONTEXT_KINDS = {'RUNTIME_READ', 'USER_INPUT', 'BLOCKED'}
READ_TOOLS = ('list_directory', 'inspect_directory', 'query_project', 'read_file')
_BLOCKING_ERRORS = re.compile(
    r'PermissionError|access.denied|permission.denied|workspace_not_authorized|'
    r'TOOL_SCOPE_REJECTED|path_outside_authorized_roots|process_identity_lost|'
    r'capability_unavailable|service_not_ready', re.I)


def normalize_context_request(raw):
    if not raw:
        return {}
    if not isinstance(raw, dict) or set(raw) - {'kind', 'detail', 'blocking_action_id'}:
        raise ValueError('context_request_schema_invalid')
    kind = str(raw.get('kind', '') or '').upper().strip()
    detail = str(raw.get('detail', '') or '').strip()
    ref = str(raw.get('blocking_action_id', '') or '').strip()
    if kind not in CONTEXT_KINDS or len(detail) > 2000 or len(ref) > 160:
        raise ValueError('context_request_invalid')
    if kind != 'RUNTIME_READ' and not detail:
        raise ValueError('context_request_detail_required')
    return {'kind': kind, 'detail': detail, 'blocking_action_id': ref}


def context_capabilities(paths, tools, results):
    blockers = []
    from .tool_result_contract import decode_result, completed_unknown_fact_observation
    for action_id, entry in dict(results or {}).items():
        # Only current, Runtime-issued receipts may justify interruption.
        # A completed indeterminate filesystem observation is not an unfinished
        # execution, nor does it prove a file is absent.
        result = decode_result(entry.get('result', ''))
        if (entry.get('evidence_status', 'ACTIVE') == 'ACTIVE'
                and completed_unknown_fact_observation(result, action_id)):
            blockers.append({'action_id':action_id, 'reason':'FACT_OBSERVATION_INDETERMINATE',
                             'observation_completed':True})
            continue
        # A missing guessed filename is not evidence that all reading is blocked.
        if str(entry.get('execution_status', '')).upper() != 'FAILED':
            continue
        if not result and entry.get('result_reason'):
            result = {'reason':entry['result_reason']}
        if result and result.get('reason') in {'PATH_NOT_AUTHORIZED', 'ACCESS_DENIED', 'USER_APPROVAL_REQUIRED', 'USER_APPROVAL_REJECTED', 'USER_APPROVAL_CANCELLED', 'USER_APPROVAL_EXPIRED'}:
            blockers.append({'action_id':action_id, 'reason':result['reason']})
            continue
        match = _BLOCKING_ERRORS.search(str(entry.get('result', '') or ''))
        if match:
            blockers.append({'action_id': action_id, 'reason': match.group(0)})
    return {'schema': 'SMARTAGENT_CONTEXT_ACQUISITION_V1',
            'authorized_paths': [str(p) for p in paths if str(p).strip()],
            'read_tools': [tool for tool in READ_TOOLS if tool in set(tools)],
            'objective_blockers': blockers}


def context_interrupt_allowed(request, capabilities):
    kind = request.get('kind', '')
    if kind == 'BLOCKED':
        return request.get('blocking_action_id') in {
            row['action_id'] for row in capabilities.get('objective_blockers', [])}
    if kind == 'USER_INPUT':
        detail = request.get('detail', '')
        # Do not disguise Runtime-readable source requests as user uploads.
        read_request = re.search(r'上傳|貼上|提供.{0,12}(?:檔案|程式碼|目錄|專案)|'
                                 r'upload|paste|file content|source code|Gradle|CMake', detail, re.I)
        path_question = bool(re.search(r'絕對路徑|完整路徑|absolute path|full path', detail, re.I))
        return bool(detail) and not (read_request and not path_question and (capabilities.get('authorized_paths') or capabilities.get('discovery_paths')) and capabilities.get('read_tools'))
    if kind == 'RUNTIME_READ':
        return False
    # Compatibility for non-context aborts. Scoped missing-context requests
    # cannot use a bare INTERRUPT to avoid trying an available read capability.
    return not (capabilities.get('authorized_paths') and capabilities.get('read_tools'))
