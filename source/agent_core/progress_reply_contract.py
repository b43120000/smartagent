"""Fixed Progress schema with Runtime-selected FULL/PATCH reply requirements.

Only accepted planning state is inherited. Decisions, evidence and blockers are
always supplied afresh; materialization does not imply acceptance or execution.
"""
from copy import deepcopy
import re

INHERITED_FIELDS = ('base_evaluation', 'total_steps', 'current_step', 'steps',
                    'completion_contract')
ROUND_FIELDS = ('decision', 'current_focus', 'decision_reason', 'evidence_refs')


def has_accepted_plan(ledger):
    return bool(ledger and getattr(ledger, 'total_steps', 0) > 0
                and getattr(ledger, 'steps', None)
                and getattr(ledger, 'completion_contract', None)
                and getattr(ledger, 'base_evaluation', ''))


def reply_contract(ledger):
    patch = has_accepted_plan(ledger)
    return {
        'schema': 'SMARTAGENT_PROGRESS_REPLY_CONTRACT_V1',
        'preferred_mode': 'PATCH' if patch else 'FULL',
        'always_required': list(ROUND_FIELDS),
        'initial_required': [*INHERITED_FIELDS, 'outcome'] if not patch else [],
        'continue_required': ['next_action'],
        'terminal_required': ['outcome'],
        'interrupt_required': ['outcome', 'context_request'],
        'inherited_if_omitted': list(INHERITED_FIELDS) if patch else [],
        'conditional': {
            'changed_total_steps': ['steps'],
            'ambiguous_condition': ['matched_condition_id'],
            'runtime_blocker': ['context_request.blocking_action_id'],
        },
        'never_inherit': ['decision', 'outcome', 'evidence_refs', 'decision_reason',
                          'context_request', 'matched_condition_id', 'next_phase',
                          'selected_action', 'runtime_state_ref'],
    }


def materialize_progress(payload, ledger):
    result = normalize_progress_fields(payload)
    mode = result.get('progress_update', 'PATCH' if has_accepted_plan(ledger) else 'FULL')
    if mode not in {'FULL', 'PATCH'}:
        raise ValueError('progress_update must be FULL or PATCH')
    if mode == 'FULL' and not has_accepted_plan(ledger):
        return result
    if not has_accepted_plan(ledger):
        raise ValueError('first_progress_requires_FULL')
    required = list(ROUND_FIELDS)
    decision = str(result.get('decision', '')).upper()
    if decision == 'CONTINUE':
        required.append('next_action')
        result.setdefault('outcome', 'PENDING')
    elif decision in {'COMPLETE', 'INTERRUPT'}:
        required.append('outcome')
        if decision == 'INTERRUPT':
            required.append('context_request')
    missing = [field for field in required if field not in result]
    if missing:
        raise ValueError('progress_patch_missing:' + ','.join(missing))
    if 'total_steps' in result and result['total_steps'] != ledger.total_steps and 'steps' not in result:
        raise ValueError('changing total_steps requires a complete steps list')
    for field in INHERITED_FIELDS:
        if field not in result:
            result[field] = deepcopy(getattr(ledger, field))
    return result


def normalize_progress_fields(payload):
    """Normalize spelling/types only when the interpretation is unique."""
    result = deepcopy(dict(payload))
    for alias, canonical in {'current step':'current_step', 'total steps':'total_steps',
                              'current focus':'current_focus', 'next action':'next_action',
                              'base evaluation':'base_evaluation'}.items():
        if alias in result:
            if canonical in result and result[canonical] != result[alias]:
                raise ValueError('conflicting_progress_alias:'+canonical)
            result[canonical] = result.pop(alias)
    def number(value):
        if isinstance(value, str) and re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value.strip()):
            return float(value.strip()) if '.' in value else int(value.strip())
        return value
    for key in ('current_step', 'total_steps'):
        if key in result:
            result[key] = number(result[key])
    if isinstance(result.get('steps'), list):
        for step in result['steps']:
            if not isinstance(step, dict):
                continue
            if 'step' in step:
                step['step'] = number(step['step'])
            if isinstance(step.get('status'), str):
                status = step['status'].strip().upper().replace(' ', '_')
                step['status'] = 'COMPLETED' if status == 'COMPLETE' else status
    return result
