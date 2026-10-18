"""Real installed Chrome/Edge: isolated profiles, persistence, Agent/calibration reuse.

Run: python -B -m agent_core.web_ui.tests.live_browser_preferences_it
Only fixture profiles and intercepted HTML are used; no website prompts are sent.
"""
import json
import socket
import tempfile
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from agent_core import web_runtime as runtime
from agent_core.browser_preferences import browser_launch_selection
from agent_core.paths import install_root
from agent_core.web_ui.manual_browser import ensure_agent_browser, open_manual_page, _launched_browsers

URL = 'https://claude.ai/chat/browser-preferences-it'
HTML = '<!doctype html><title>SmartAgent Browser Settings IT</title><textarea id="composer"></textarea>'


def main():
    evidence = install_root() / 'localdata/logs/manual_dom_it' / ('browser_settings_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    evidence.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='smartagent-browser-settings-it-')
    root = Path(temporary.name)
    results, owned, sessions = [], [], []
    with sync_playwright() as pw:
        @contextmanager
        def existing_driver():
            # The synchronous test already owns an event loop; production BATs
            # normally create their driver in a separate process.
            yield pw
        try:
            for browser, instance in [('chrome', 1), ('chrome', 2), ('edge', 1)]:
                selection = browser_launch_selection(dict(browser=browser, instance=instance))
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                endpoint = f'http://127.0.0.1:{port}'
                with ExitStack() as stack:
                    for name, value in dict(CHATGPT_BROWSER_CHANNEL=selection['channel'],
                                            CHATGPT_BROWSER_PROFILE=selection['profile'],
                                            CHATGPT_CDP_PORT=port, CHATGPT_CDP_ENDPOINT=endpoint,
                                            M365_CDP_PORT=port, M365_CDP_ENDPOINT=endpoint,
                                            PROFILE_DIR=root).items():
                        stack.enter_context(patch.object(runtime, name, value))
                    stack.enter_context(patch('agent_core.paths.browser_profile_root', return_value=root))
                    stack.enter_context(patch('agent_core.paths.runtime_root', return_value=root))
                    chosen, started = ensure_agent_browser(pw)
                    assert started and chosen == endpoint
                    process = _launched_browsers[-1]
                    actual = pw.chromium.connect_over_cdp(endpoint)
                    owned.append((actual, process))
                    context = actual.contexts[0]
                    assert context.pages == [], 'unexpected_startup_blank'
                    context.route('https://claude.ai/**', lambda route: route.fulfill(content_type='text/html', body=HTML))
                    keepalive = context.new_page()
                    keepalive.goto(URL)
                    marker = f'{browser}_{instance}'
                    context.add_cookies([dict(name='browser_it_selection', value=marker, url='https://claude.ai', expires=time.time()+3600)])
                    # Exercise the actual Agent startup path against the HTML fixture.
                    with patch.object(runtime, 'shared_browser_enabled', return_value=False), \
                         patch.object(runtime.WebLLMScraper, '_is_logged_in', return_value=True), \
                         patch.object(runtime.WebLLMScraper, '_dismiss_known_blocking_dialogs'), \
                         patch.object(runtime.WebLLMScraper, '_claim_conversation_owner'), \
                         patch.object(runtime.WebLLMScraper, 'release_conversation_owner', return_value=0), \
                         patch.dict(runtime.os.environ, {'SMARTAGENT_ATTACH_CDP':'', 'SMARTAGENT_ISOLATED_BROWSER':''}):
                        scraper = runtime.WebLLMScraper('claude')
                        scraper.cfg = dict(scraper.cfg, url=URL)
                        scraper.start(playwright=pw)
                        assert scraper.get_cdp_endpoint() == endpoint
                        scraper._page.locator('#composer').fill('Agent fixture write')
                        assert scraper._page.locator('#composer').input_value() == 'Agent fixture write'
                        scraper.close()
                    with patch('playwright.sync_api.sync_playwright', existing_driver), open_manual_page(URL) as page:
                        assert next(c['value'] for c in page.context.cookies(URL) if c['name']=='browser_it_selection') == marker
                        page.locator('#composer').fill('Calibration fixture write: ' + marker)
                        assert page.locator('#composer').input_value().endswith(marker)
                        page.screenshot(path=str(evidence / f'{marker}.png'))
                    assert not keepalive.is_closed()
                    assert ensure_agent_browser(pw) == (endpoint, False)
                    sessions.append((context, marker))
                    for previous, expected in sessions:
                        assert next(c['value'] for c in previous.cookies(URL) if c['name']=='browser_it_selection') == expected
                    # Restart this exact fixture profile to verify persisted login cookies.
                    actual.new_browser_cdp_session().send('Browser.close')
                    process.wait(timeout=15)
                    owned.pop()
                    ensure_agent_browser(pw)
                    process = _launched_browsers[-1]
                    actual = pw.chromium.connect_over_cdp(endpoint)
                    owned.append((actual, process))
                    restored = actual.contexts[0]
                    assert next(c['value'] for c in restored.cookies(URL) if c['name']=='browser_it_selection') == marker
                    sessions[-1] = (restored, marker)
                    results.append(dict(browser=browser, instance=instance, passed=True,
                                        channel=selection['channel'], agent_calibration_shared=True,
                                        isolated_cookies=True, cookies_survive_restart=True,
                                        blank_pages_at_start=0, submitted=False))
            (evidence / 'result.json').write_text(json.dumps(dict(passed=True, cases=results), indent=2), encoding='utf-8')
            print('BROWSER_SETTINGS_IT_PASS ' + str(evidence))
        finally:
            for browser, process in reversed(owned):
                if browser.is_connected():
                    browser.new_browser_cdp_session().send('Browser.close')
                process.wait(timeout=15)
    for attempt in range(50):
        try:
            temporary.cleanup()
            break
        except PermissionError:
            if attempt == 49: raise
            time.sleep(.1)


if __name__ == '__main__':
    main()
