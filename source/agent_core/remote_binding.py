"""Operator-owned RemoteAgent binding and process-lifetime snapshot."""
import json
import os
import time
import uuid
from pathlib import Path
from .workspace import normalize_workspace_path, normalize_chatgpt_url
from .conversation_identity import conversation_id
from .json_state_io import read_json_retry, write_json_atomic
from .process_file_lock import exclusive_process_lock
from .paths import (
    remote_binding_external_update_path,
    remote_binding_path,
    install_root,
)

ROOT = install_root()
LOCAL_SKILL_PATH = ROOT / "localdata" / "secure" / "windows_security" / "skills"
DEFAULT_SKILL_PATH = (
    LOCAL_SKILL_PATH
    if LOCAL_SKILL_PATH.is_dir()
    else Path.home() / ".codex" / "skills"
)


def normalize_skill_path(raw, *, require_exists=False):
    value = str(raw or DEFAULT_SKILL_PATH).strip().strip('"')
    path = Path(value).expanduser().resolve()
    if require_exists and (not path.exists() or not path.is_dir()):
        raise ValueError(f'remote_skill_path_unavailable:{path}')
    return str(path)

def validate(binding):
    workspace = normalize_workspace_path(binding['workspace'])
    url = normalize_chatgpt_url(binding['gpt_url'])
    if not conversation_id(url):
        raise ValueError('remote_binding_requires_conversation_url')
    skill_path = normalize_skill_path(binding.get('skill_path', ''))
    return {'workspace': workspace, 'gpt_url': url, 'skill_path': skill_path}

def load(root=ROOT):
    path = remote_binding_path(root)
    return validate(json.loads(path.read_text(encoding='utf-8')))

def save(binding, root=ROOT):
    binding = validate(binding)
    write_json_atomic(remote_binding_path(root), binding)
    from RemoteAgent.local_telegram_config import default_store
    store = default_store(root)
    if store.path.is_file():
        store.update_workspace(binding['workspace'])
    return binding

def _update_path(root=ROOT):
    return remote_binding_external_update_path(root)

def submit_external_update(binding, root=ROOT):
    from .workspace_access import require_writable_workspace
    candidate = validate(binding)
    candidate['workspace'] = require_writable_workspace(candidate['workspace'])
    path = _update_path(root); lock = path.with_name(path.name + '.lock')
    with exclusive_process_lock(lock, timeout_sec=5.0, label='external remote binding'):
        current = read_json_retry(path) if path.is_file() else {}
        if str(current.get('status', '')).upper() in {'PENDING', 'CLAIMED'}:
            raise RuntimeError('remote_binding_external_update_busy')
        now = time.time()
        row = {'version': 1, 'request_id': 'RBUP-'+uuid.uuid4().hex.upper(), 'status': 'PENDING', 'binding': candidate, 'created_at': now, 'updated_at': now}
        write_json_atomic(path, row)
    return row

def process_external_update(runtime_apply, root=ROOT):
    path = _update_path(root); lock = path.with_name(path.name + '.lock')
    with exclusive_process_lock(lock, timeout_sec=5.0, label='external remote binding'):
        if not path.is_file(): return None
        row = read_json_retry(path)
        if str(row.get('status', '')).upper() != 'PENDING': return None
        row.update(status='CLAIMED', updated_at=time.time()); write_json_atomic(path, row)
    try:
        runtime_apply(validate(row['binding']))
        row.update(status='DONE', updated_at=time.time(), completed_at=time.time(), error='')
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        if 'remote_binding_update_requires_idle_runtime' in str(exc):
            row.update(status='PENDING', updated_at=time.time(), last_error=error)
        else:
            row.update(status='FAILED', updated_at=time.time(), completed_at=time.time(), error=error)
    with exclusive_process_lock(lock, timeout_sec=5.0, label='external remote binding'):
        write_json_atomic(path, row)
    return row

def activate(root=ROOT):
    binding = load(root)
    os.environ['SMARTAGENT_REMOTE_BINDING_SNAPSHOT'] = json.dumps(binding)
    os.environ['SMARTAGENT_TELEGRAM_WORKSPACE'] = binding['workspace']
    os.environ['SMARTAGENT_REMOTE_EXECUTION_URL'] = binding['gpt_url']
    os.environ['SMARTAGENT_SKILL_PATH'] = binding['skill_path']
    return binding

def active():
    raw = os.environ.get('SMARTAGENT_REMOTE_BINDING_SNAPSHOT', '')
    return validate(json.loads(raw)) if raw else None
