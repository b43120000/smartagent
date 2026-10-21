"""Calibration browser lifecycle, independent of adapter DOM compatibility."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from .factory import normalize_web_conversation_url
from .provider_registry import provider_from_url

_launched_browsers: list[subprocess.Popen] = []

def _available(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=1) as response:
            return bool(json.load(response).get("webSocketDebuggerUrl"))
    except (OSError, ValueError):
        return False


def active_agent_endpoint(provider: str = '') -> str:
    """All sites can reuse the authenticated WebCopilot browser context."""
    from ..web_runtime import CHATGPT_CDP_ENDPOINT
    endpoints = [CHATGPT_CDP_ENDPOINT]
    return next((endpoint for endpoint in endpoints if _available(endpoint)), '')


def agent_browser_launch_options(playwright, *, headless: bool = False) -> list[str]:
    """Use exactly WebCopilot's browser binary/channel and persistent profile."""
    from ..paths import browser_profile_root
    from ..web_runtime import CHATGPT_BROWSER_CHANNEL, CHATGPT_BROWSER_PROFILE, CHATGPT_CDP_PORT
    if not CHATGPT_BROWSER_CHANNEL:
        executable = Path(playwright.chromium.executable_path)
    else:
        suffix = {'chrome': 'Google/Chrome/Application/chrome.exe',
                  'msedge': 'Microsoft/Edge/Application/msedge.exe'}.get(CHATGPT_BROWSER_CHANNEL)
        if suffix is None:
            raise RuntimeError('unsupported_agent_browser_channel:' + CHATGPT_BROWSER_CHANNEL)
        candidates = [Path(os.environ.get(key, '')) / suffix for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is None:
            raise RuntimeError('agent_browser_executable_not_found:' + CHATGPT_BROWSER_CHANNEL)
    return [str(executable), f'--user-data-dir={browser_profile_root() / CHATGPT_BROWSER_PROFILE}',
            f'--remote-debugging-port={CHATGPT_CDP_PORT}', '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check', '--disable-blink-features=AutomationControlled',
            '--hide-crash-restore-bubble', '--disable-session-crashed-bubble', '--no-startup-window',
            '--headless=new' if headless else '--start-maximized']


def ensure_agent_browser(playwright, *, headless: bool = False) -> tuple[str, bool]:
    """Start a reusable app-profile browser independently of calibration lifetime."""
    from ..paths import runtime_root
    from ..process_file_lock import exclusive_process_lock
    from ..web_runtime import CHATGPT_CDP_ENDPOINT
    with exclusive_process_lock(runtime_root() / 'calibration_browser_start.lock', timeout_sec=40,
                                label='calibration Agent browser startup'):
        if _available(CHATGPT_CDP_ENDPOINT):
            return CHATGPT_CDP_ENDPOINT, False
        args = agent_browser_launch_options(playwright, headless=headless)
        _launched_browsers[:] = [child for child in _launched_browsers if child.poll() is None]
        print('[manualBrowser] 開啟與 WebCopilot 相同的瀏覽器及登入資料夾。', flush=True)
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _available(CHATGPT_CDP_ENDPOINT):
                _launched_browsers.append(process)
                return CHATGPT_CDP_ENDPOINT, True
            time.sleep(0.2)
        # A launcher may exit after forwarding to another startup. Give the
        # endpoint its full deadline; only clean up our exact live child.
        exit_code = process.poll()
        if exit_code is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise RuntimeError('無法開啟 Agent 的共用登入瀏覽器；原登入資料夾可能仍被另一個視窗使用。'
                           f'不會改用另一份未登入的 profile。launcher_exit={exit_code}')


def create_manual_page(context, *, shared_browser: bool):
    """Reuse only the startup blank in a browser we launched ourselves."""
    if not shared_browser:
        startup_blank = next((page for page in context.pages if page.url == "about:blank"), None)
        if startup_blank is not None:
            return startup_blank
    return context.new_page()


def close_owned_context(context) -> None:
    from playwright.sync_api import Error
    # Chromium may already exit when the final owned tab is closed.
    if context.browser is not None and not context.browser.is_connected():
        return
    try:
        context.close()
    except Error as exc:
        if "Target page, context or browser has been closed" not in str(exc):
            raise


@contextmanager
def open_manual_page(url: str, *, endpoint: str = "", headless: bool = False, navigation_wait: str = "domcontentloaded"):
    from playwright.sync_api import sync_playwright
    from ..shared_browser import shared_browser_enabled, connect_shared_browser, mark_shared_page

    url = normalize_web_conversation_url(url)
    provider = provider_from_url(url)
    chosen = endpoint or active_agent_endpoint(provider)
    with sync_playwright() as playwright:
        browser = context = page = None
        started = False
        try:
            if chosen or shared_browser_enabled():
                if shared_browser_enabled():
                    browser, chosen = connect_shared_browser(playwright, endpoint or '')
                else:
                    print('[manualBrowser] 使用已啟動的 Agent 瀏覽器：' + chosen, flush=True)
                    browser = playwright.chromium.connect_over_cdp(chosen)
                if not browser.contexts:
                    raise RuntimeError("manual_browser_no_context")
                context = browser.contexts[0]
            else:
                chosen, started = ensure_agent_browser(playwright, headless=headless)
                browser = playwright.chromium.connect_over_cdp(chosen)
                if not browser.contexts:
                    raise RuntimeError('manual_browser_no_context')
                context = browser.contexts[0]
            # The CDP endpoint may have been published by a concurrent Agent.
            # Never infer ownership of any existing page from a launch attempt.
            page = create_manual_page(context, shared_browser=True)
            if shared_browser_enabled():
                mark_shared_page(page)
            page.goto(url, wait_until=navigation_wait, timeout=60000)
            if not headless:
                page.bring_to_front()
            yield page
        finally:
            if page is not None and not page.is_closed():
                page.close()
            if context is not None and browser is None:
                close_owned_context(context)
            # Stopping Playwright disconnects CDP; do not close the shared browser.
