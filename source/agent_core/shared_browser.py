"""Attach to the user's approved Chrome session; never launch or close Chrome."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .paths import install_root, runtime_root
from .process_file_lock import exclusive_process_lock


def browser_settings() -> dict:
    path = install_root() / 'config' / 'browser_session.json'
    if not path.exists():
        return {'mode': 'legacy'}
    settings = json.loads(path.read_text(encoding='utf-8'))
    if settings.get('mode') not in {'existing_chrome', 'legacy'}:
        raise ValueError('shared_browser_invalid_mode')
    return settings


def shared_browser_enabled() -> bool:
    mode = os.environ.get('SMARTAGENT_BROWSER_MODE')
    if mode is not None and mode not in {'existing_chrome', 'legacy'}:
        raise ValueError('shared_browser_invalid_mode')
    return (mode if mode is not None else browser_settings()['mode']) == 'existing_chrome'


def validate_local_endpoint(value: str) -> str:
    endpoint = str(value).strip()
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {'ws', 'http'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('shared_browser_endpoint_must_be_loopback')
    if parsed.scheme == 'ws' and not re.fullmatch(r'/devtools/browser/[A-Za-z0-9_-]+', parsed.path):
        raise ValueError('shared_browser_invalid_websocket_path')
    return endpoint.rstrip('/') if parsed.scheme == 'http' else endpoint


def endpoint_from_port_file(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535:
        raise ValueError('shared_browser_invalid_active_port')
    path = lines[1]
    if not re.fullmatch(r'/devtools/browser/[A-Za-z0-9_-]+', path):
        raise ValueError('shared_browser_invalid_active_port')
    return validate_local_endpoint(f'ws://127.0.0.1:{int(lines[0])}{path}')


def shared_browser_endpoint() -> str:
    explicit = os.environ.get('SMARTAGENT_SHARED_BROWSER_CDP', '')
    if explicit:
        return validate_local_endpoint(explicit)
    settings = browser_settings()
    profile = os.environ.get('SMARTAGENT_CHROME_USER_DATA_DIR') or settings.get('user_data_dir', '')
    profile = Path(os.path.expandvars(profile)).expanduser()
    try:
        return endpoint_from_port_file((profile / 'DevToolsActivePort').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Chrome 共用連線尚未就緒：請保持已登入的 Chrome 開啟，至 '
            'chrome://inspect/#remote-debugging 啟用遠端偵錯，並在連線要求出現時按「允許」。'
            '程式不會另開需要登入的瀏覽器。') from exc


def connect_shared_browser(playwright, endpoint: str = ''):
    endpoint = validate_local_endpoint(endpoint) if endpoint else shared_browser_endpoint()
    print('[SharedBrowser] 連線到已登入的 Chrome；若出現連線要求，請按「允許」。', flush=True)
    with exclusive_process_lock(runtime_root() / 'shared_browser_attach.lock', timeout_sec=90,
                                label='shared Chrome attachment'):
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=60000)
        if not browser.contexts:
            raise RuntimeError('shared_chrome_has_no_default_context')
        return browser, endpoint


def mark_shared_page(page) -> None:
    page.add_init_script('window.__smartAgentSharedOwned = true;')
    page.evaluate('() => { window.__smartAgentSharedOwned = true; }')


def is_shared_page(page) -> bool:
    try:
        return bool(page.evaluate("() => window.__smartAgentSharedOwned === true || (window.name || '').startsWith('SMARTAGENT_')"))
    except Exception:
        return False


def same_provider_url(first: str, second: str) -> bool:
    from .web_ui.provider_registry import provider_from_url
    try:
        return provider_from_url(first) == provider_from_url(second)
    except ValueError:
        return False
