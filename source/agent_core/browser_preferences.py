"""Persistent browser selections for WebGPT1 and optional dedicated GAR WebGPT2."""
from __future__ import annotations

import json
from pathlib import Path

from .paths import install_root, browser_profile_root, runtime_root
from .process_file_lock import exclusive_process_lock


def settings_path() -> Path:
    return install_root() / 'config' / 'browser_session.json'


def validate_selection(browser: str, instance: int) -> dict:
    if browser not in {'chrome', 'edge'}:
        raise ValueError('browser_must_be_chrome_or_edge')
    if type(instance) is not int or not 1 <= instance <= 5:
        raise ValueError('browser_instance_must_be_1_to_5')
    return {'browser': browser, 'instance': instance}


def _load_raw() -> dict:
    path = settings_path()
    raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(raw, dict):
        raise ValueError('browser_settings_invalid_root')
    return raw


def load_browser_preferences() -> dict:
    raw = _load_raw()
    primary = validate_selection(raw.get('browser', 'chrome'), raw.get('instance', 1))
    second = raw.get('webgpt2')
    if second is None:
        second = primary
    if not isinstance(second, dict):
        raise ValueError('browser_webgpt2_settings_invalid')
    fresh = validate_selection(second.get('browser'), second.get('instance'))
    return dict(primary, webgpt2=fresh)


def save_browser_preferences(browser: str, instance: int, *, role: str = 'webgpt1') -> dict:
    selection = validate_selection(browser, instance)
    if role not in {'webgpt1', 'webgpt2'}:
        raise ValueError('browser_role_invalid')
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_process_lock(runtime_root() / 'browser_settings.lock', timeout_sec=10,
                                label='browser settings'):
        raw = _load_raw()
        if role == 'webgpt1':
            raw.update(selection)
        else:
            raw['webgpt2'] = dict(selection)
        raw['mode'] = 'legacy'
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        temporary.replace(path)
    return selection


def browser_launch_selection(selection: dict | None = None, *, role: str = 'webgpt1') -> dict:
    selection = selection if selection is not None else load_browser_preferences()
    if role == 'webgpt1':
        selected = validate_selection(selection['browser'], selection['instance'])
    elif role == 'webgpt2':
        other = selection.get('webgpt2') or selection
        selected = validate_selection(other['browser'], other['instance'])
    else:
        raise ValueError('browser_role_invalid')
    browser, instance = selected['browser'], selected['instance']
    port = (12800 if browser == 'chrome' else 12810) + instance
    profile = f'{browser}_{instance}'
    return dict(selected, channel='chrome' if browser == 'chrome' else 'msedge',
                profile=profile, profile_dir=str(browser_profile_root() / profile),
                port=port, endpoint=f'http://127.0.0.1:{port}')
