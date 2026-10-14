"""Action-scoped dependency policy; declarations never grant tool authority."""
from __future__ import annotations
import re

OBSERVATION_TOOLS = frozenset({
    'read_file', 'list_directory', 'find_file', 'inspect_directory',
    'query_project', 'inspect_project_scope', 'inspect_project_working_set',
    'inspect_semantic_map', 'inspect_project_ledger', 'query_project_history',
    'compare_project_snapshot', 'extract_project_dependencies',
})


def dependency_diagnostic(action, earlier_ids, result_ledger):
    deps = action.get('depends_on', [])
    if not isinstance(deps, list) or len(deps) > 32 or any(
        not isinstance(ref, str) or not ref.strip() for ref in deps
    ) or len(set(deps)) != len(deps):
        return {'reason': 'action_dependencies_invalid', 'detail': 'depends_on must contain at most 32 unique non-empty action IDs'}
    missing = [ref for ref in deps if ref == action.get('action_id') or ref not in earlier_ids and ref not in result_ledger]
    if missing:
        return {'reason': 'action_dependency_unknown_or_forward', 'detail': ','.join(missing)}
    return None


def unsatisfied_dependencies(action, result_ledger, request_id, task_epoch):
    blocked = []
    for ref in action.get('depends_on', []):
        record = result_ledger.get(ref) or {}
        if (record.get('request_id') != request_id
            or record.get('task_epoch') != task_epoch
            or record.get('action_id') != ref or not record.get('tool')
            or record.get('evidence_status', 'ACTIVE') != 'ACTIVE'
            or record.get('execution_status') != 'SUCCEEDED'
            or record.get('executed') is not True
            or record.get('effective_verification_status',record.get('verification_status')) in {'UNVERIFIED','SPEC_INVALID'}
            or record.get('effective_verification_status', record.get('verification_status')) == 'FAIL'
            or record.get('result_status') != 'SUCCEEDED'):
            blocked.append(ref)
    return blocked


def command_is_proven_observation(action):
    """Narrow positive grammar, not the model's operation label or a blacklist.

    Complex commands remain available through normal admission, but cannot
    bypass unresolved side-effect/prerequisite guards by saying INSPECT.
    """
    if action.get('operation') not in {'INSPECT','GIT_INSPECT','VERIFY'}:
        return False
    command = str(action.get('command','')).strip()
    wrapper = re.fullmatch(r'(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+(?:(?:-NoProfile|-NonInteractive)\s+)*-Command\s+"([^"\r\n]+)"', command, re.I)
    if wrapper:
        command = wrapper[1]
    literal = r"'(?:[^'\r\n]|'')*'"
    patterns = (
        r'Get-Location',
        r'(?:Write-Output\s+)?\(Get-Location\)\.Path',
        rf'(?:Test-Path|Get-Item|Get-Content)\s+-LiteralPath\s+{literal}(?:\s+-Raw)?',
        r'Get-Process\s+-Id\s+[0-9]+',
        r'\$PSVersionTable\.PSVersion',
        rf'Write-Output\s+{literal}',
        rf'git\s+-C\s+{literal}\s+(?:status\s+--short|rev-parse\s+HEAD|branch\s+--show-current|log\s+-1\s+--oneline)',
        r'git\s+(?:status(?:\s+--short)?|rev-parse\s+HEAD|branch\s+--show-current|log\s+-1\s+--oneline)',
    )
    return any(re.fullmatch(pattern, command, re.IGNORECASE) for pattern in patterns)


def is_observation(action):
    return action.get('tool') in OBSERVATION_TOOLS or (
        action.get('tool') == 'run_command' and command_is_proven_observation(action))
