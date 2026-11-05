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
    if kind == 'BLOCKED' and not ref:
        raise ValueError('context_request_blocking_action_id_required')
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


def context_request_repair_diagnostic(raw, *, objective_blocker_ids=()):
    """Safe field-level repair guidance; no Planner prose or guessed blockers."""
    allowed = ("kind", "detail", "blocking_action_id")
    known = [str(ref) for ref in objective_blocker_ids
             if isinstance(ref, str) and ref.strip()][:8]
    issues = []
    def issue(field, code, expected):
        issues.append({"field": field, "code": code, "expected": expected})
    if raw is None or not isinstance(raw, dict):
        issue("context_request", "object_required",
              "object with kind and context-specific detail")
    else:
        if set(raw) - set(allowed):
            issue("context_request", "unsupported_keys",
                  "only kind, detail, blocking_action_id")
        kind = str(raw.get("kind", "") or "").upper().strip()
        if kind not in CONTEXT_KINDS:
            issue("context_request.kind", "kind_invalid",
                  "RUNTIME_READ | USER_INPUT | BLOCKED")
        detail = str(raw.get("detail", "") or "").strip()
        if kind != "RUNTIME_READ" and not detail:
            issue("context_request.detail", "detail_required",
                  "non-empty specific reason, maximum 2000 characters")
        elif len(detail) > 2000:
            issue("context_request.detail", "detail_too_long",
                  "maximum 2000 characters")
        ref = str(raw.get("blocking_action_id", "") or "").strip()
        if len(ref) > 160:
            issue("context_request.blocking_action_id", "reference_too_long",
                  "maximum 160 characters")
        if kind == "BLOCKED" and not ref:
            issue("context_request.blocking_action_id", "objective_blocker_ref_required",
                  "must cite a Runtime objective_blockers action_id")
        if kind == "BLOCKED" and ref and ref not in known:
            issue("context_request.blocking_action_id", "objective_blocker_not_available",
                  "choose an action_id in current objective_blockers")
    if not issues:
        issue("context_request", "normalization_rejected",
              "one of the canonical context_request shapes")
    return {
        "schema": "SMARTAGENT_CONTEXT_REQUEST_FIELD_REPAIR_V1",
        "repair_scope": "CONTEXT_REQUEST_SCHEMA",
        "field_issues": issues[:8],
        "allowed_fields": list(allowed),
        "allowed_kinds": sorted(CONTEXT_KINDS),
        "objective_blocker_ids": known,
        "blocked_requires_runtime_receipt": True,
        "runtime_read_requires_continue": True,
        "fallback_when_no_objective_blocker": "CONTINUE_WITH_AUTHORIZED_READ",
        "model_must_not_invent_refs": True,
    }
