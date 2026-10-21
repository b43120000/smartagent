"""Transport-neutral, explicit task continuation and durable action journal.

Text/ACK IDs are lookup hints, never authority. A new attempt has fresh transport
identity; old results retain provenance and are never blindly executed again.
"""
from __future__ import annotations

import copy
import getpass
import hashlib
import json
import os
import re
import time
from pathlib import Path

from .paths import runtime_root, remote_tasks_path, remote_events_path
from .task_state import TaskStateStore, write_json_atomic, read_json_retry


class ContinuationError(ValueError):
    pass


def normalize_task_outcome(value):
    value = str(value or '').strip().upper()
    return value if value in {'SUCCESS', 'FAILED', 'PARTIAL', 'UNKNOWN'} else 'UNKNOWN'


def terminal_heading(state, outcome=''):
    if state == 'COMPLETED':
        value = normalize_task_outcome(outcome)
        return {'SUCCESS': '🟢 已完成', 'FAILED': '🔴 已結束（驗收失敗）',
                'PARTIAL': '🟠 部分完成／結果未確認', 'UNKNOWN': '🟠 部分完成／結果未確認'}[value]
    return '🔴 執行失敗' if state == 'FAILED' else '🟠 任務中斷'


def record_outcome(record):
    return normalize_task_outcome(record.get('task_outcome') or record.get('progress', {}).get('outcome'))


def resume_instructions(task_id, request_id=''):
    reference = task_id or request_id
    return (f'接續方式：接續工作 {reference}\n'
            '可直接貼上完整通知；若要調整任務，請在最後另起一行填寫「調整需求：…」。\n調整需求：') if reference else ''


def parse_continuation(text: str) -> dict | None:
    text = str(text).replace('\r\n', '\n').replace('\r', '\n').strip()
    match = re.match(r"^(接續上一個工作或指定 ACK ID 工作|接續(?:上一個工作|工作|任務)?|後續驗證|補驗證|resume|followup_verify)(?:\s|[:：]|$)", text, re.I)
    id_pattern = r'(?:RR-[A-Z0-9_-]+|TASK-[A-Z0-9_-]+|WA-[A-Z0-9_-]+|EVT-[A-Z0-9_-]+|WEBACK-[A-Z0-9_-]+|ACK-[A-Z0-9_-]+)'
    pasted = bool(re.match(r'^(?:🔴|🟠)', text)) or bool(re.search(
        r'^任務結果：(?:🔴|🟠)', text, re.M))
    direct_id = bool(re.fullmatch(id_pattern, text.split('\n', 1)[0].strip(), re.I))
    if not match and not pasted and not direct_id:
        return None
    first_line = text.split('\n', 1)[0]
    ids = list(dict.fromkeys(re.findall(id_pattern, first_line, re.I))) if match or direct_id else []
    # Body prose may quote other tasks. Only the current notification's footer
    # selects a task; pasted text is never authorization or trusted progress.
    footer_pattern = r'^(?:task_id|request_id|event_id):[ \t]*(' + id_pattern + r')[ \t]*$'
    footer = list(re.finditer(footer_pattern, text, re.I | re.M))
    # Freeze routing at our notification's adjustment template. Everything
    # after it is opaque new human goal text, including any nested ID/labels.
    template = re.search(r'^接續方式：接續工作 [^\n]+\n可直接貼上完整通知[^\n]+\n調整需求：[ \t]*', text, re.M)
    explicit_goal = None
    if template:
        explicit_goal = text[template.end():].strip()
        # A copied empty template followed by the human's own label.
        leading = re.match(r'^(?:調整需求|新需求|需求|要求|goal)[ \t]*[:：][ \t]*', explicit_goal, re.I)
        if leading:
            explicit_goal = explicit_goal[leading.end():].strip()
        footer = list(re.finditer(footer_pattern, text[:template.start()], re.I | re.M))
    markers = list(re.finditer(r'^(?:調整需求|新需求|需求|要求|goal)[ \t]*[:：][ \t]*', text, re.I | re.M))
    markers = [row for row in markers if match or direct_id or not footer or row.start() > footer[0].end()]
    marker = markers[0] if markers else None
    if marker and not template:
        footer = list(re.finditer(footer_pattern, text[:marker.start()], re.I | re.M))
    if not ids and footer:
        selected = [row for row in footer if row[0].lower().startswith('task_id:')] or [
            row for row in footer if row[0].lower().startswith('request_id:')] or footer
        ids = [selected[-1][1]]
    if not ids and match:
        routing_text = text[:template.start()] if template else text[:marker.start()] if marker else text
        ids = list(dict.fromkeys(re.findall(id_pattern, routing_text, re.I)))
    if not ids and pasted:
        raise ContinuationError('continuation_reference_required: 請保留 task_id 或 request_id')
    mode = 'FOLLOWUP_VERIFY' if match and match[1].casefold() in {'後續驗證', '補驗證', 'followup_verify'} else 'RESUME'
    # Pasted error/progress is not an instruction or a source of authorized paths.
    goal = ''
    if explicit_goal is not None:
        goal = explicit_goal
    elif marker:
        goal = text[marker.end():].strip()
        if re.match(r'^(?:task_id|request_id|event_id):', goal, re.I):
            goal = ''  # copied blank template followed by transport metadata
        if goal in {'填寫要查核的條件', '填寫調整後需求（可省略）'}:
            goal = ''
    if mode == 'FOLLOWUP_VERIFY':
        if not goal:
            raise ContinuationError('followup_goal_required: 請另起一行填寫「需求：…」')
    return {'mode': mode, 'ids': ids, 'goal': goal}


def local_owner() -> str:
    return 'local:' + getpass.getuser().casefold()


def task_owner(task) -> str:
    meta = dict(task.metadata or {})
    route = dict(task.reply_route or meta.get('reply_route') or {})
    if str(meta.get('transport', '')).upper() == 'TELEGRAM':
        sender, chat = str(meta.get('sender_id', '')), str(route.get('chat_id', ''))
        if not sender or not chat:
            raise ContinuationError('continuation_owner_missing')
        return f'telegram:{sender}:{chat}'
    if str(meta.get('transport', '')).upper() == 'LOCAL_TEST':
        return local_owner()
    raise ContinuationError('continuation_owner_unsupported')


def _same_path(a, b):
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


class TaskContinuationStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = runtime_root(self.root) / 'task_continuations.json'
        self.lock = TaskStateStore(self.path)
        self.instance = hashlib.sha256((str(self.root).casefold() + '|' + os.environ.get('SMARTAGENT_INSTANCE_ID', '')).encode()).hexdigest()

    def _load(self):
        if not self.path.exists():
            return {'schema': 'SMARTAGENT_TASK_CONTINUATION_V1', 'records': {}}
        if self.path.stat().st_size > 64 * 1024 * 1024:
            raise ContinuationError('continuation_journal_capacity_reached')
        data = read_json_retry(self.path)
        if data.get('schema') != 'SMARTAGENT_TASK_CONTINUATION_V1' or not isinstance(data.get('records'), dict):
            raise ContinuationError('continuation_store_invalid')
        return data

    def _save(self, data):
        if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > 64 * 1024 * 1024:
            raise ContinuationError('continuation_journal_capacity_reached: 請封存紀錄後再執行')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, data)

    def register(self, *, task_id, request_id, epoch, goal, workspace, owner):
        if not owner or not task_id or not request_id or not epoch:
            raise ContinuationError('continuation_identity_required')
        with self.lock.process_lock():
            data = self._load()
            existing = data['records'].get(task_id)
            identity = {'task_id': task_id, 'request_id': request_id, 'epoch': epoch,
                        'goal': goal, 'workspace': str(Path(workspace).resolve()),
                        'owner': owner, 'instance': self.instance}
            if existing:
                if any(existing.get(k) != v for k, v in identity.items()):
                    raise ContinuationError('continuation_identity_changed')
                return copy.deepcopy(existing)
            record = {**identity, 'state': 'REGISTERED', 'created_at': time.time(), 'pid': os.getpid(),
                      'actions': {}, 'results': {}, 'prepared': {}, 'progress': {},
                      'pending': {}, 'ack_ids': [], 'event_ids': [], 'summary': '', 'lease': ''}
            data['records'][task_id] = record
            self._save(data)
            return copy.deepcopy(record)

    def register_task(self, task):
        return self.register(task_id=task.task_id, request_id=task.request_id,
            epoch=task.task_epoch, goal=task.request, workspace=task.workspace, owner=task_owner(task))

    def prepare_request(self, task_id, *, request, owner, workspace):
        record = self.get(task_id)
        if not record or record.get('owner') != owner or not _same_path(record['workspace'], workspace):
            raise ContinuationError('continuation_attempt_identity_mismatch')
        if record.get('source_task_id'):
            source = self.get(record['source_task_id'])
            if (not source or source.get('lease') != task_id or source.get('owner') != owner or
                    source.get('instance') != self.instance or not _same_path(source['workspace'], workspace)):
                raise ContinuationError('continuation_reservation_mismatch')
            source['_reset_progress'] = bool(record.get('reset_progress'))
            source['_revised_goal'] = record.get('effective_goal', '') if record.get('goal_changed') else ''
            return source, record['mode'], record['effective_goal']
        command = parse_continuation(request)
        if not command:
            return None, '', request
        source = self.resolve(command, owner=owner, workspace=workspace, child_task_id=task_id)
        goal = (command['goal'] or source.get('effective_goal') or source['goal']) if command['mode'] == 'RESUME' else command['goal']
        return source, command['mode'], goal

    def _legacy_records(self, owner, workspace):
        """Migrate trusted terminal TaskState results, never scrape model prose."""
        store = TaskStateStore(remote_tasks_path(self.root))
        with store.process_lock():
            tasks = list(store.load().values())
        records = []
        for task in tasks:
            try:
                allowed = task_owner(task) == owner and _same_path(task.workspace, workspace)
            except (ContinuationError, OSError, ValueError):
                allowed = False
            if not allowed:
                continue
            configured_instance = os.environ.get('SMARTAGENT_INSTANCE_ID', '')
            if configured_instance and str((task.metadata or {}).get('instance_id', '')) != configured_instance:
                continue
            result = task.result_ledger.get('final') or task.result_ledger.get('interrupted') or {}
            if not isinstance(result, dict):
                result = {}
            records.append({'task_id': task.task_id, 'request_id': task.request_id,
                'epoch': task.task_epoch, 'goal': task.request, 'workspace': task.workspace,
                'owner': owner, 'instance': self.instance, 'state': task.state,
                'created_at': task.created_at, 'results': copy.deepcopy(result.get('action_result_ledger') or {}),
                'actions': {}, 'prepared': {}, 'progress': {}, 'pending': {},
                'ack_ids': list(result.get('ack_ids') or []), 'event_ids': [],
                'summary': str(result.get('response', '')), 'task_outcome': normalize_task_outcome(result.get('task_outcome')),
                'legacy': True, 'lease': ''})
        # Events only identify an owning task; they do not grant authorization.
        event_path = remote_events_path(self.root)
        if event_path.exists():
            event_data = read_json_retry(event_path)
            events = event_data.get('events', [])
            if isinstance(events, dict):
                events = list(events.values())
            by_id = {r['task_id']: r for r in records}
            for event in events:
                if isinstance(event, dict) and event.get('task_id') in by_id:
                    by_id[event['task_id']]['event_ids'].append(str(event.get('event_id', '')))
        return records

    def resolve(self, command, *, owner, workspace, child_task_id):
        legacy = self._legacy_records(owner, workspace)
        with self.lock.process_lock():
            data = self._load()
            for record in legacy:
                existing = data['records'].setdefault(record['task_id'], record)
                if existing.get('owner') == owner and existing.get('instance') == self.instance:
                    existing['event_ids'] = list(dict.fromkeys(existing.get('event_ids', []) + record['event_ids']))
            records = [r for r in data['records'].values() if r['task_id'] != child_task_id
                and r.get('owner') == owner and r.get('instance') == self.instance
                and _same_path(r['workspace'], workspace)]
            ids = command['ids']
            if ids:
                candidates = [r for r in records if all(identifier.upper() in
                    {str(v).upper() for v in [r['task_id'], r['request_id'], *r.get('ack_ids', []), *r.get('event_ids', [])]} for identifier in ids)]
            else:
                # "Previous" is unambiguous only when exactly one eligible task exists.
                candidates = [r for r in records if r.get('state') not in {'RUNNING', 'REGISTERED', 'QUEUED', 'DISPATCHED'}
                    and not (command['mode'] == 'RESUME' and r.get('state') == 'COMPLETED' and record_outcome(r) == 'SUCCESS')]
            if len(candidates) != 1:
                raise ContinuationError('continuation_not_found_or_ambiguous: 請指定原 task_id 或 request_id')
            parent = candidates[0]
            from .process_file_lock import _pid_alive
            if parent.get('state') in {'RUNNING', 'REGISTERED'} and parent.get('pid') and not _pid_alive(parent['pid']):
                parent['state'] = 'INTERRUPTED'
            if parent.get('lease'):
                child = data['records'].get(parent['lease'], {})
                if child.get('state') in {'RUNNING', 'REGISTERED'} and child.get('pid') and not _pid_alive(child['pid']):
                    child['state'] = 'INTERRUPTED'
                    parent['lease'] = ''
            if parent.get('state') in {'RUNNING', 'REGISTERED', 'QUEUED', 'DISPATCHED'} or parent.get('lease'):
                raise ContinuationError('continuation_task_busy: 原任務或接續工作仍在執行，不會啟動第二份')
            if command['mode'] == 'RESUME' and parent.get('state') == 'COMPLETED' and record_outcome(parent) == 'SUCCESS':
                raise ContinuationError('continuation_already_completed: 請改用「後續驗證」')
            if command['mode'] == 'RESUME' and parent.get('legacy'):
                raise ContinuationError('legacy_resume_journal_missing: 舊版缺少執行前紀錄，只能後續唯讀驗證，不能安全重播')
            parent['lease'] = child_task_id
            child = data['records'].get(child_task_id)
            if not child or child.get('owner') != owner or child.get('state') != 'REGISTERED' or not _same_path(child['workspace'], workspace):
                raise ContinuationError('continuation_child_not_registered')
            # Reservation AND child source binding must be one atomic commit.
            prior_goal = parent.get('effective_goal') or parent['goal']
            goal = (command['goal'] or prior_goal) if command['mode'] == 'RESUME' else command['goal']
            changed = command['mode'] == 'RESUME' and goal != prior_goal
            reset = changed or parent.get('state') == 'COMPLETED' or command['mode'] == 'FOLLOWUP_VERIFY'
            child.update(source_task_id=parent['task_id'], mode=command['mode'],
                effective_goal=goal, goal_changed=changed, reset_progress=reset,
                actions=copy.deepcopy(parent.get('actions', {})), results=copy.deepcopy(parent.get('results', {})),
                prepared=copy.deepcopy(parent.get('prepared', {})),
                pending_actions=[] if changed else copy.deepcopy(parent.get('pending_actions', [])),
                pending={} if reset else copy.deepcopy(parent.get('pending', {})),
                progress={} if reset else copy.deepcopy(parent.get('progress', {})),
                authorized_paths=copy.deepcopy(parent.get('authorized_paths', [])))
            self._save(data)
            restored = copy.deepcopy(parent)
            restored['_reset_progress'] = reset
            restored['_revised_goal'] = goal if changed else ''
            return restored

    def start(self, task_id, *, source=None, mode=''):
        with self.lock.process_lock():
            data = self._load(); record = data['records'][task_id]
            if record['state'] != 'REGISTERED':
                raise ContinuationError('continuation_attempt_already_started: 必須建立明確的新接續工作')
            record.update(state='RUNNING', source_task_id=(source or {}).get('task_id', ''), mode=mode,
                          pid=os.getpid(), started_at=time.time())
            if source:
                record.update(actions=copy.deepcopy(source.get('actions', {})),
                    results=copy.deepcopy(source.get('results', {})), prepared=copy.deepcopy(source.get('prepared', {})))
            self._save(data)

    def prepare_action(self, task_id, action):
        with self.lock.process_lock():
            data = self._load(); record = data['records'][task_id]
            record['prepared'][action['action_id']] = {'action': copy.deepcopy(action), 'state': 'EXECUTING'}
            self._save(data)

    def pending_batch(self, task_id, actions):
        with self.lock.process_lock():
            data = self._load(); record = data['records'][task_id]
            record['pending_actions'] = copy.deepcopy(actions)
            self._save(data)

    def snapshot(self, loop):
        with self.lock.process_lock():
            data = self._load(); record = data['records'][loop.task_id]
            record['actions'] = copy.deepcopy(loop.action_ledger)
            record['results'] = copy.deepcopy(loop.action_result_ledger)
            for action_id in loop.action_ledger:
                record['prepared'].pop(action_id, None)
            record['pending_actions'] = [action for action in record.get('pending_actions', [])
                                         if action.get('action_id') not in loop.action_ledger]
            record['progress'] = loop.progress_ledger.to_dict() if loop.progress_ledger else {}
            record['authorized_paths'] = list(loop.authorized_paths)
            record['pending'] = {key: copy.deepcopy(getattr(loop, key, {})) for key in
                ('pending_result_recovery', 'pending_command_field_repairs', 'deferred_action_repairs', 'pending_verification_requirement')}
            record['ack_ids'] = list(loop.accepted_ack_ids)
            self._save(data)

    def finish(self, task_id, state, summary='', *, outcome=None):
        with self.lock.process_lock():
            data = self._load(); record = data['records'][task_id]
            record.update(state=state, summary=str(summary), finished_at=time.time())
            if state == 'COMPLETED':
                record['task_outcome'] = normalize_task_outcome(outcome) if outcome is not None else record_outcome(record)
            parent_id = record.get('source_task_id', '')
            if not parent_id:
                parent_id = next((key for key, value in data['records'].items() if value.get('lease') == task_id), '')
            if parent_id:
                parent = data['records'][parent_id]
                if parent.get('lease') == task_id:
                    parent['lease'] = ''
                    # Only verification updates, not the original execution/outcome.
                    if record.get('mode') == 'FOLLOWUP_VERIFY':
                        for action_id, old in parent['results'].items():
                            new = record['results'].get(action_id, {})
                            for key in ('verification_history', 'effective_verification_id', 'effective_verification_status', 'effective_expectation_status', 'effective_terminal_eligible'):
                                if key in new:
                                    old[key] = copy.deepcopy(new[key])
                    if task_id not in parent.setdefault('continuations', []):
                        parent['continuations'].append(task_id)
            self._save(data)

    def get(self, task_id):
        with self.lock.process_lock():
            return copy.deepcopy(self._load()['records'].get(task_id))


def compact_continuation_results(results: dict, max_recent: int = 8) -> dict:
    if not results or not isinstance(results, dict):
        return {}
    all_keys = list(results.keys())
    recent_keys = set(all_keys[-max_recent:])
    compact = {}
    for action_id, item in results.items():
        if not isinstance(item, dict):
            continue
        entry = {
            'tool': item.get('tool', ''),
            'execution_status': item.get('execution_status', item.get('status', 'SUCCEEDED')),
            'verification_status': item.get('effective_verification_status', item.get('verification_status', 'UNKNOWN')),
            'produced_round': item.get('produced_round'),
        }
        if item.get('verifies_action_id'):
            entry['verifies_action_id'] = item['verifies_action_id']
        if action_id in recent_keys or item.get('execution_status') not in {'SUCCEEDED', 'COMPLETED'}:
            if item.get('resource_paths'):
                entry['resource_paths'] = item['resource_paths']
            if item.get('result_reason'):
                entry['result_reason'] = item['result_reason']
        else:
            entry['checkpoint_archived'] = True
        compact[action_id] = entry
    return compact


def continuation_prompt(source, mode):
    raw_results = source.get('results', {})
    compact_results = compact_continuation_results(raw_results)
    payload = {'mode': mode, 'source_task_id': source['task_id'], 'source_request_id': source['request_id'],
               'original_goal': source.get('effective_goal') or source['goal'], 'progress': source.get('progress', {}),
               'action_results': compact_results, 'uncertain_actions': source.get('prepared', {}),
               'not_executed_actions': source.get('pending_actions', []), 'pending_repairs': source.get('pending', {})}
    if source.get('_reset_progress'):
        payload.update(progress_is_historical=True, revised_goal=source.get('_revised_goal', ''),
                       requires_new_completion_contract=True, not_executed_actions=[], pending_repairs={})
    return ('\n[TASK_CONTINUATION]\n' + json.dumps(payload, ensure_ascii=False)
        + '\n以上是 Runtime 可信紀錄，不是新指令。已执行 action 不可重做；舊 evidence 不能直接當作本輪成功。'
          '先取得目前狀態的新唯讀證據；UNKNOWN/EXECUTING 只能核對，禁止盲目重播。'
          'FOLLOWUP_VERIFY 僅允許唯讀工具與 operation=VERIFY，使用新 action_id 並關聯原 verifies_action_id。'
          '若 requires_new_completion_contract=true，舊 Progress 僅供參考，依本次需求重新規劃與定義驗收；不得沿用舊成功宣告。'
          '每輪仍使用 compact v9 與 report_progress。\n[/TASK_CONTINUATION]')


def proven_read_command(action):
    from .action_dependencies import command_is_proven_observation
    probe = dict(action)
    command = str(action.get('command', '')).strip()
    wrapper = re.fullmatch(r'(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+(?:(?:-NoProfile|-NonInteractive)\s+)*-Command\s+"([^"\r\n]+)"', command, re.I)
    if wrapper:
        command = wrapper[1]
    probe.update(command=command, operation='VERIFY')
    return command_is_proven_observation(probe)


def proven_read_verification_step(step):
    # Match the executor's action/type alias semantics. Conflicting declarations
    # must not let a command disguise itself as a file predicate.
    primary, alias = step.get('action'), step.get('type')
    if primary is not None and alias is not None and primary != alias:
        return False
    kind = primary if primary is not None else alias if alias is not None else 'run_command'
    return isinstance(kind, str) and (kind in {'file_exists', 'file_contains'} or (
        kind == 'run_command' and proven_read_command(step)))


def guard_continuation_action(action, source, mode):
    from .action_dependencies import OBSERVATION_TOOLS
    action_id = str(action.get('action_id', ''))
    if action_id in source.get('prepared', {}):
        raise ContinuationError('continuation_action_outcome_unknown: 必須先核對，不可重播 ' + action_id)
    if action_id in source.get('results', {}) or action_id in source.get('actions', {}):
        raise ContinuationError('continuation_action_already_executed: 使用新的唯讀驗證 action_id')
    if action.get('tool') == 'run_command':
        steps = action.get('verify') or []
        if isinstance(steps, dict):
            steps = [steps]
        if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
            raise ContinuationError('continuation_verification_shape_invalid')
        readonly = action.get('operation') in {'VERIFY', 'INSPECT', 'GIT_INSPECT'} and proven_read_command(action) and all(
            proven_read_verification_step(step)
            for step in steps)
    else:
        readonly = action.get('tool') in OBSERVATION_TOOLS
    if action.get('tool') == 'final_response':
        return
    uncertain = source.get('prepared') or any(unresolved_continuation_result(r) for r in source.get('results', {}).values())
    if (mode == 'FOLLOWUP_VERIFY' or uncertain) and not readonly:
        raise ContinuationError('continuation_readonly_reconciliation_required: 只允許可證明的唯讀檢查')
    # Changing only action_id cannot evade completed-action replay protection.
    if not readonly:
        comparable = effect_identity(action)
        for cached in [*source.get('actions', {}).values(), *source.get('original_prepared', source.get('prepared', {})).values()]:
            old = effect_identity(cached.get('action', {}))
            if comparable == old:
                raise ContinuationError('continuation_duplicate_operation_forbidden')


def effect_identity(action):
    metadata = {'action_id','depends_on','replaces_action_id','success_criteria','verify',
                'condition_id','verifies_action_id','expected_failure','expected_verification'}
    if action.get('tool') == 'run_command':
        # operation/verification labels do not change the command's effect.
        return {'tool':'run_command','command':str(action.get('command','')).strip(),
                'working_directory':action.get('working_directory')}
    return {key:value for key,value in action.items() if key not in metadata}


def unresolved_continuation_result(record):
    execution = record.get('execution_status')
    if execution in {'RUNNING','QUEUED'} or record.get('result_status') in {'RUNNING','QUEUED'}:
        # A file-exists check does not prove a process exited or a queued
        # Telegram delivery completed. Require a dedicated Runtime signal.
        return record.get('pending_resolution') not in {'PROCESS_EXIT_CONFIRMED','DELIVERY_CONFIRMED'}
    return execution in {'UNKNOWN','PARTIAL'} and record.get('pending_resolution') != 'SCOPED_VERIFICATION_OBTAINED'


def render_task_reply(*, original_request, base, conclusion, task_id, request_id, state, source_task_id='', ack_id='', outcome=''):
    return (f'使用者原始需求：\n{original_request}\n\nBase：\n{base or "（尚無已接受的評估）"}'
            f'\n\n結果／中斷原因：\n{conclusion}\n\n狀態：{state}'
            + (f'\n驗收結果：{outcome}' if outcome else '')
            + f'\n任務結果：{terminal_heading(state, outcome)}'
            + f'\ntask_id: {task_id}\nrequest_id: {request_id}'
            + (f'\nack_id: {ack_id}' if ack_id else '')
            + (f'\nsource_task_id: {source_task_id}' if source_task_id else '')
            + (f'\n{resume_instructions(task_id, request_id)}' if state != 'COMPLETED' or normalize_task_outcome(outcome) != 'SUCCESS'
               else f'\n後續查核：後續驗證 {task_id}\n需求：填寫要查核的條件'))
