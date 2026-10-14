"""Batched Progress diagnostics and narrowly scoped read-only fallback."""
from copy import deepcopy
import math
import json
import hashlib

from .progress_reply_contract import has_accepted_plan, ROUND_FIELDS

READ_ONLY_TOOLS = frozenset({'list_directory', 'inspect_directory', 'find_file', 'query_project'})
DISPLAY_FIELDS = frozenset({'total_steps', 'current_step', 'steps', 'base_evaluation', 'current_focus', 'next_action'})


def repair_diagnostic(payload, ledger, error, known_refs=None):
    from .task_progress import PROGRESS_DECISIONS, PROGRESS_OUTCOMES, MAX_EVIDENCE_REFS
    issues = []
    def add(field, expected, actual=None):
        if not any(issue['field'] == field and issue['expected'] == expected for issue in issues):
            issues.append({'field':field, 'expected':expected, 'actual_type':type(actual).__name__})
    for field in ('total_steps', 'current_step'):
        if field not in payload and has_accepted_plan(ledger):
            continue
        value = payload.get(field)
        if type(value) not in {int, float} or not math.isfinite(value):
            add(field, 'finite number', value)
    required = list(ROUND_FIELDS)
    required.append('next_action' if payload.get('decision') == 'CONTINUE' else 'outcome')
    for field in required:
        value = payload.get(field)
        if field == 'evidence_refs':
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_EVIDENCE_REFS or any(not isinstance(v, str) or not v.strip() for v in value):
                add(field, 'non-empty list of current Runtime evidence IDs', value)
            elif known_refs is not None and any(ref not in known_refs for ref in value):
                add(field, 'references selected from available_evidence_refs', value)
        elif not isinstance(value, str) or not value.strip():
            add(field, 'non-empty string', value)
    decision = str(payload.get('decision', '') or '').strip().upper()
    outcome = str(payload.get('outcome', '') or '').strip().upper()
    if decision not in PROGRESS_DECISIONS:
        add('decision', 'CONTINUE | COMPLETE | INTERRUPT', payload.get('decision'))
    if outcome not in PROGRESS_OUTCOMES:
        add('outcome', 'PENDING | SUCCESS | FAILED | PARTIAL | UNKNOWN', payload.get('outcome'))
    elif decision == 'CONTINUE' and outcome != 'PENDING':
        add('outcome', 'CONTINUE requires PENDING', payload.get('outcome'))
    elif decision == 'COMPLETE' and outcome not in {'SUCCESS', 'FAILED', 'PARTIAL'}:
        add('outcome', 'COMPLETE requires SUCCESS | FAILED | PARTIAL', payload.get('outcome'))
    elif decision == 'INTERRUPT' and outcome not in {'FAILED', 'UNKNOWN'}:
        add('outcome', 'INTERRUPT requires FAILED | UNKNOWN', payload.get('outcome'))
    steps = payload.get('steps')
    if isinstance(steps, list):
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                add(f'steps[{index}]', 'step object', step)
                continue
            value = step.get('step')
            if type(value) not in {int, float} or not math.isfinite(value):
                add(f'steps[{index}].step', 'finite number', value)
            if step.get('status') not in {'PENDING', 'IN_PROGRESS', 'COMPLETED'}:
                add(f'steps[{index}].status', 'PENDING | IN_PROGRESS | COMPLETED', step.get('status'))
    detail = str(error)
    field = getattr(error, 'field', '') or next((key for key in sorted(DISPLAY_FIELDS) if key in detail), 'progress')
    if not issues:
        add(field, detail)
    return {'marker':'[WEBAGENT_PROGRESS_REJECTED]', 'reason':'invalid_progress_payload',
            'detail':detail, 'repair_mode':'BATCH_FIELDS', 'field_issues':issues,
            'available_evidence_refs': sorted(known_refs or []),
            'evidence_reference_policy': {
                'initial_continue': 'Select REQUEST_ACCEPTED or RUNTIME_STATUS when no tool result exists; do not invent an action result.',
                'terminal': 'Request/status references alone do not prove SUCCESS; cite actual current evidence required by the completion contract.',
            },
            'runtime_known_values':{key:deepcopy(getattr(ledger, key, None)) for key in DISPLAY_FIELDS} if has_accepted_plan(ledger) else {},
            'suggestion':'一次補齊 field_issues 中必要欄位。已接受的計畫欄位省略即可沿用，不必標記 PATCH；不得沿用舊 decision/evidence 或重做已執行 action。'}


def read_only_progress_fallback(payload, ledger, actions, error, known_refs):
    if not has_accepted_plan(ledger) or getattr(ledger, 'decision', '') != 'CONTINUE':
        return None
    if not actions or any(action.get('tool') not in READ_ONLY_TOOLS for action in actions):
        return None
    if str(payload.get('decision', '')).upper() != 'CONTINUE' or payload.get('outcome', 'PENDING') != 'PENDING':
        return None
    if payload.get('context_request') or payload.get('next_phase') == 'TERMINAL':
        return None
    if 'completion_contract' in payload and payload['completion_contract'] != ledger.completion_contract:
        return None
    refs = payload.get('evidence_refs')
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in known_refs for ref in refs):
        return None
    if not isinstance(payload.get('decision_reason'), str) or not payload['decision_reason'].strip():
        return None
    field = getattr(error, 'field', '') or next((key for key in sorted(DISPLAY_FIELDS) if key in str(error)), '')
    if field not in DISPLAY_FIELDS:
        return None
    fallback = {key:deepcopy(getattr(ledger, key)) for key in DISPLAY_FIELDS}
    fallback.update(tool='report_progress', action_id=payload.get('action_id', ''),
                    completion_contract=deepcopy(ledger.completion_contract),
                    decision='CONTINUE', outcome='PENDING', evidence_refs=list(refs),
                    decision_reason=payload['decision_reason'],
                    next_action=', '.join(sorted({action['tool'] for action in actions})))
    choices = ledger.completion_contract.get('in_progress') or []
    if not choices:
        return None
    fallback['matched_condition'] = ledger.matched_condition if ledger.matched_condition in choices else choices[0]
    return fallback


def repair_signature(diagnostic, evidence_generation):
    # Track the validator's concrete reason as well as ALL unresolved fields.
    # No action IDs, prose, focus or decision_reason: wording changes are not
    # progress. Fixing outcome while evidence remains missing is progress.
    detail = str(diagnostic.get('detail', ''))
    for prefix in ('invalid progress outcome:', 'invalid progress decision:'):
        if detail.startswith(prefix):
            detail = prefix.rstrip(':')
            break
    return (detail,
            tuple(sorted((issue['field'], issue['expected']) for issue in diagnostic.get('field_issues', []))),
            evidence_generation)


def read_fact_digest(action, result):
    text = str(result)
    if action.get('tool') in {'list_directory','find_file'}:
        from .tool_result_contract import decode_result
        payload = decode_result(text)
        if payload and payload.get('schema') == 'SMARTAGENT_TOOL_RESULT_V1':
            # Canonical request/action identity changes on each invocation;
            # it is not evidence that directory/search facts changed.
            text = json.dumps({key:payload.get(key) for key in
                ('status','reason','executed','requested_path','resolved_path','data')},
                ensure_ascii=False,sort_keys=True)
    if action.get('tool') == 'inspect_directory':
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        def without_timings(value):
            if isinstance(value, dict):
                return {k:without_timings(v) for k,v in value.items() if k not in {'elapsed_ms','_runtime_result'}}
            if isinstance(value, list):
                return [without_timings(v) for v in value]
            return value
        if isinstance(payload, dict):
            text = json.dumps(without_timings(payload), ensure_ascii=False, sort_keys=True)
    if action.get('tool') == 'query_project':
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            # Query IDs/index bootstrap timings are bookkeeping, not new source.
            payload = {key:payload.get(key) for key in ('status','error','project_root','project_handle','snapshot_id','results')}
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
