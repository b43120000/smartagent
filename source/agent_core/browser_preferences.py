"""One persistent browser selection for Agent, WebCopilot and DOM calibration."""
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


def load_browser_preferences() -> dict:
    path = settings_path()
    raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(raw, dict):
        raise ValueError('browser_settings_invalid_root')
    return validate_selection(raw.get('browser', 'chrome'), raw.get('instance', 1))


def save_browser_preferences(browser: str, instance: int) -> dict:
    selection = validate_selection(browser, instance)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_process_lock(runtime_root() / 'browser_settings.lock', timeout_sec=10, label='browser settings'):
        raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(raw, dict):
            raise ValueError('browser_settings_invalid_root')
        # The menu selects managed persistent profiles, not personal Chrome CDP.
        raw.update(selection, mode='legacy')
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    return selection


def browser_launch_selection(selection: dict | None = None) -> dict:
    selection = selection if selection is not None else load_browser_preferences()
    browser, instance = selection['browser'], selection['instance']
    validate_selection(browser, instance)
    # Brand AND instance define identity. Switching never reuses another login.
    port = (12800 if browser == 'chrome' else 12810) + instance
    profile = f'{browser}_{instance}'
    return dict(selection, channel='chrome' if browser == 'chrome' else 'msedge',
                profile=profile, profile_dir=str(browser_profile_root() / profile),
                port=port, endpoint=f'http://127.0.0.1:{port}')
