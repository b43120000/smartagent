"""Shared Chrome routing and personal-tab ownership regressions."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core import shared_browser as shared
from agent_core.web_runtime import WebLLMScraper, CHATGPT_CDP_ENDPOINT, CHATGPT_CDP_PORT
from WebAgent import browser_bridge
from agent_core.web_ui.manual_browser import open_manual_page, active_agent_endpoint, agent_browser_launch_options, ensure_agent_browser

WS = 'ws://127.0.0.1:9222/devtools/browser/shared-test'
URLS = {'chatgpt':'https://chatgpt.com/c/shared-test', 'gemini':'https://gemini.google.com/app/test',
        'm365':'https://m365.cloud.microsoft/chat', 'claude':'https://claude.ai/chat/test'}


class Page:
    def __init__(self, url='about:blank'):
        self.url=url; self.owned=False; self.closed=False; self.name=''
    def evaluate(self, script, arg=None):
        if 'window.__smartAgentSharedOwned = true' in script: self.owned=True
        if '__smartAgentSharedOwned === true' in script: return self.owned
        if 'window.name = value' in script: self.name=arg
        if script == '() => window.name': return self.name
    def add_init_script(self, script): pass
    def goto(self, url, **kwargs): self.url=url
    def bring_to_front(self): pass
    def is_closed(self): return self.closed
    def close(self, **kwargs): self.closed=True


class SharedBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original=[Page(url) for url in URLS.values()]
        self.context=SimpleNamespace(pages=list(self.original))
        def new_page():
            page=Page(); self.context.pages.append(page); return page
        self.context.new_page=Mock(side_effect=new_page)
        self.browser=Mock(contexts=[self.context]);self.browser.is_connected.return_value=True
        self.context.browser=self.browser
        self.pw=Mock()
        self.pw.chromium.connect_over_cdp.return_value=self.browser

    def test_port_file_and_loopback_validation(self):
        self.assertEqual(shared.endpoint_from_port_file('9222\n/devtools/browser/shared-test\n'), WS)
        for text in ['0\n/devtools/browser/a', '65536\n/devtools/browser/a', '9222\nhttps://evil.test',
                     '9222\n/devtools/browser/', '9222\n/devtools/browser/a?x=1', '9222\n/devtools/browser/a\nextra']:
            with self.subTest(text=text), self.assertRaises(ValueError): shared.endpoint_from_port_file(text)
        for endpoint in ['ws://evil.test:9222/devtools/browser/a', 'file:///tmp/x',
                         'ws://user:pass@127.0.0.1:9222/devtools/browser/a', 'http://127.0.0.1:0']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError): shared.validate_local_endpoint(endpoint)

    def test_discovery_tracks_chrome_restart_and_missing_file_fails_closed(self):
        root=Path(self.temp.name)
        with patch.dict(os.environ, {'SMARTAGENT_CHROME_USER_DATA_DIR':str(root), 'SMARTAGENT_SHARED_BROWSER_CDP':''}):
            with self.assertRaisesRegex(RuntimeError,'chrome://inspect'): shared.shared_browser_endpoint()
            path=root/'DevToolsActivePort';path.write_text('9222\n/devtools/browser/shared-test')
            self.assertEqual(shared.shared_browser_endpoint(),WS)
            path.write_text('9333\n/devtools/browser/new-browser')
            self.assertEqual(shared.shared_browser_endpoint(),'ws://127.0.0.1:9333/devtools/browser/new-browser')

    def test_all_manual_providers_attach_same_browser_and_only_close_their_page(self):
        @contextmanager
        def driver(): yield self.pw
        with patch('playwright.sync_api.sync_playwright', driver), \
             patch('agent_core.web_ui.manual_browser._available', return_value=False), \
             patch.object(shared,'shared_browser_enabled',return_value=True), \
             patch.object(shared,'connect_shared_browser',return_value=(self.browser,WS)) as connect:
            for provider,url in URLS.items():
                with self.subTest(provider=provider), open_manual_page(url) as page:
                    self.assertTrue(page.owned)
                    self.assertIn(page,self.context.pages)
                    self.assertNotIn(page,self.original)
                self.assertTrue(page.closed)
            self.assertEqual(connect.call_count,4)
        self.assertFalse(any(page.closed for page in self.original))
        self.browser.close.assert_not_called()
        self.pw.chromium.launch_persistent_context.assert_not_called()

    def test_all_sites_discover_existing_agent_browser_before_standalone_launch(self):
        @contextmanager
        def driver(): yield self.pw
        with patch('playwright.sync_api.sync_playwright',driver), \
             patch('agent_core.web_ui.manual_browser._available',side_effect=lambda ep: ep==CHATGPT_CDP_ENDPOINT), \
             patch.object(shared,'shared_browser_enabled',return_value=False):
            for provider,url in URLS.items():
                with self.subTest(provider=provider):
                    self.assertEqual(active_agent_endpoint(provider),CHATGPT_CDP_ENDPOINT)
                    with open_manual_page(url) as page:
                        self.assertIn(page,self.context.pages)
                    self.pw.chromium.connect_over_cdp.assert_called_with(CHATGPT_CDP_ENDPOINT)
        self.pw.chromium.launch_persistent_context.assert_not_called()
        self.browser.close.assert_not_called()
        self.assertFalse(any(page.closed for page in self.original))

    def test_missing_agent_connection_keeps_existing_fallback(self):
        with patch('agent_core.web_ui.manual_browser._available',return_value=False):
            self.assertEqual(active_agent_endpoint('claude'),'')
        with patch('agent_core.web_ui.manual_browser._available',side_effect=lambda ep: ep.endswith(':1275')):
            self.assertEqual(active_agent_endpoint('m365'),'')

    def test_standalone_calibration_uses_webcopilot_binary_profile_and_endpoint(self):
        self.pw.chromium.executable_path='C:/runtime/chromium/chrome.exe'
        with patch('agent_core.web_runtime.CHATGPT_BROWSER_CHANNEL',None), \
             patch('agent_core.web_runtime.CHATGPT_BROWSER_PROFILE','existing-webcopilot-login'), \
             patch('agent_core.paths.browser_profile_root',return_value=Path(self.temp.name)):
            args=agent_browser_launch_options(self.pw)
        self.assertEqual(args[0],'C:\\runtime\\chromium\\chrome.exe' if os.name=='nt' else 'C:/runtime/chromium/chrome.exe')
        self.assertIn('--user-data-dir='+str(Path(self.temp.name)/'existing-webcopilot-login'),args)
        self.assertIn(f'--remote-debugging-port={CHATGPT_CDP_PORT}',args)
        self.assertNotIn('--headless=new',args)

    def test_standalone_browser_process_survives_calibration_page_close(self):
        @contextmanager
        def driver(): yield self.pw
        with patch('playwright.sync_api.sync_playwright',driver), \
             patch('agent_core.web_ui.manual_browser.active_agent_endpoint',return_value=''), \
             patch('agent_core.web_ui.manual_browser.ensure_agent_browser',return_value=(CHATGPT_CDP_ENDPOINT,True)) as ensure, \
             patch.object(shared,'shared_browser_enabled',return_value=False):
            with open_manual_page(URLS['claude']) as page:
                agent_page=self.context.new_page()
            self.assertTrue(page.closed)
            self.assertFalse(agent_page.closed)
            ensure.assert_called_once_with(self.pw,headless=False)
        self.browser.close.assert_not_called()
        self.pw.chromium.launch_persistent_context.assert_not_called()

    def test_concurrent_start_never_takes_existing_blank_page(self):
        @contextmanager
        def driver(): yield self.pw
        initializing=Page();self.context.pages.insert(0,initializing)
        with patch('playwright.sync_api.sync_playwright',driver), \
             patch('agent_core.web_ui.manual_browser.active_agent_endpoint',return_value=''), \
             patch('agent_core.web_ui.manual_browser.ensure_agent_browser',return_value=(CHATGPT_CDP_ENDPOINT,True)), \
             patch.object(shared,'shared_browser_enabled',return_value=False):
            with open_manual_page(URLS['claude']) as page:
                self.assertIsNot(page,initializing)
        self.assertFalse(initializing.closed)
        self.assertEqual(initializing.url,'about:blank')

    def test_browser_bootstrap_rechecks_endpoint_under_lock(self):
        with patch('agent_core.web_ui.manual_browser._available',return_value=True), \
             patch('agent_core.paths.runtime_root',return_value=Path(self.temp.name)), \
             patch('agent_core.web_ui.manual_browser.subprocess.Popen') as launch:
            self.assertEqual(ensure_agent_browser(self.pw),(CHATGPT_CDP_ENDPOINT,False))
        launch.assert_not_called()

    def test_launcher_exit_does_not_end_endpoint_wait_early(self):
        child=Mock();child.poll.return_value=0
        with patch('agent_core.web_ui.manual_browser._available',side_effect=[False,False,True]), \
             patch('agent_core.paths.runtime_root',return_value=Path(self.temp.name)), \
             patch('agent_core.web_ui.manual_browser.agent_browser_launch_options',return_value=['chrome.exe']), \
             patch('agent_core.web_ui.manual_browser.time.monotonic',side_effect=[0,0,1]), \
             patch('agent_core.web_ui.manual_browser.time.sleep'), \
             patch('agent_core.web_ui.manual_browser.subprocess.Popen',return_value=child):
            self.assertEqual(ensure_agent_browser(self.pw),(CHATGPT_CDP_ENDPOINT,True))
        child.terminate.assert_not_called()

    def test_start_timeout_cleans_up_only_spawned_process(self):
        child=Mock();child.poll.return_value=None
        with patch('agent_core.web_ui.manual_browser._available',return_value=False), \
             patch('agent_core.paths.runtime_root',return_value=Path(self.temp.name)), \
             patch('agent_core.web_ui.manual_browser.agent_browser_launch_options',return_value=['chrome.exe']), \
             patch('agent_core.web_ui.manual_browser.time.monotonic',side_effect=[0,0,31]), \
             patch('agent_core.web_ui.manual_browser.time.sleep'), \
             patch('agent_core.web_ui.manual_browser.subprocess.Popen',return_value=child):
            with self.assertRaisesRegex(RuntimeError,'launcher_exit'):
                ensure_agent_browser(self.pw)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)

    def test_real_browser_bootstrap_creates_no_blank_and_supports_cdp_page(self):
        import socket
        import time
        from agent_core.web_ui.manual_browser import _launched_browsers
        from playwright.sync_api import sync_playwright
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        endpoint=f'http://127.0.0.1:{port}'
        with patch('agent_core.web_runtime.CHATGPT_BROWSER_CHANNEL',None), \
             patch('agent_core.web_runtime.CHATGPT_BROWSER_PROFILE','isolated-startup-fixture'), \
             patch('agent_core.web_runtime.CHATGPT_CDP_PORT',port), \
             patch('agent_core.web_runtime.CHATGPT_CDP_ENDPOINT',endpoint), \
             patch('agent_core.paths.browser_profile_root',return_value=Path(self.temp.name)), \
             patch('agent_core.paths.runtime_root',return_value=Path(self.temp.name)), \
             sync_playwright() as playwright:
            chosen,started=ensure_agent_browser(playwright)
            self.assertEqual(chosen,endpoint);self.assertTrue(started)
            browser=playwright.chromium.connect_over_cdp(endpoint)
            try:
                context=browser.contexts[0]
                self.assertEqual(context.pages,[])
                page=context.new_page();page.set_content('<textarea id="composer"></textarea>')
                page.locator('#composer').fill('shared browser startup verified')
                self.assertEqual(page.locator('#composer').input_value(),'shared browser startup verified')
            finally:
                # This is the isolated fixture browser created above, never
                # the real Agent endpoint or a user profile.
                browser.new_browser_cdp_session().send('Browser.close')
                _launched_browsers[-1].wait(timeout=10)
        # Windows cache handles can outlive the main browser for a short time.
        deadline=time.monotonic()+5
        while True:
            try:
                self.temp.cleanup();break
            except PermissionError:
                if time.monotonic()>=deadline: raise
                time.sleep(.1)

    def test_runtime_all_providers_use_same_cdp_without_launching_or_taking_personal_tabs(self):
        def bind(scraper, playwright=None): scraper._pw=self.pw;scraper._owns_playwright=False
        with patch('agent_core.web_runtime.PROFILE_DIR',Path(self.temp.name)), \
             patch('agent_core.web_runtime.shared_browser_enabled',return_value=True), \
             patch('agent_core.web_runtime.shared_browser_endpoint',return_value=WS), \
             patch('agent_core.web_runtime.connect_shared_browser',return_value=(self.browser,WS)), \
             patch.object(WebLLMScraper,'_bind_playwright',bind), \
             patch.object(WebLLMScraper,'_is_logged_in',return_value=True), \
             patch.object(WebLLMScraper,'_dismiss_known_blocking_dialogs'), \
             patch.object(WebLLMScraper,'_claim_conversation_owner'), \
             patch.object(WebLLMScraper,'release_conversation_owner',return_value=0), \
             patch('agent_core.web_runtime.time.sleep'):
            for provider,url in URLS.items():
                with self.subTest(provider=provider):
                    scraper=WebLLMScraper(provider);scraper.cfg=dict(scraper.cfg,url=url)
                    scraper.start(playwright=self.pw)
                    self.assertIs(scraper._browser,self.context)
                    self.assertNotIn(scraper._page,self.original)
                    self.assertEqual(scraper._page.url,url)
                    self.assertEqual(scraper.get_cdp_endpoint(),WS)
                    owned=scraper._page;scraper.close()
                    self.assertTrue(owned.closed)
        self.assertFalse(any(page.closed for page in self.original))
        self.browser.close.assert_not_called()
        self.pw.chromium.launch_persistent_context.assert_not_called()

    def test_webdirect_preserves_personal_tab_with_same_conversation(self):
        with patch.object(browser_bridge,'shared_browser_enabled',return_value=True), \
             patch.object(browser_bridge,'_conversation_ready') as ready:
            page=browser_bridge._open_target_conversation_unlocked(self.context, URLS['chatgpt'])
            self.assertTrue(page.owned)
            self.assertNotIn(page,self.original)
            ready.assert_called_once_with(page,URLS['chatgpt'])
        self.assertFalse(any(page.closed for page in self.original))

    def test_normal_webdirect_start_does_not_require_personal_chrome_debugging(self):
        page=Page(URLS['chatgpt'])
        scraper=SimpleNamespace(_page=page,_browser=self.context)
        manager=Mock();manager.get_or_create.return_value=scraper
        with patch.object(browser_bridge,'shared_browser_enabled',return_value=False), \
             patch.object(browser_bridge,'shared_browser_endpoint',side_effect=AssertionError('must not require Chrome opt-in')), \
             patch.object(browser_bridge,'_cdp_available',return_value=False), \
             patch.object(browser_bridge,'open_target_conversation',return_value=page), \
             patch('agent_core.web_runtime.get_manager',return_value=manager):
            result=browser_bridge.open_or_attach_browser(URLS['chatgpt'])
        self.assertEqual(result[3],'OWN_PERSISTENT_BROWSER')
        self.assertIs(result[0],page)
        manager.get_or_create.assert_called_once_with('chatgpt')

    def test_webdirect_adoption_cannot_close_the_shared_browser(self):
        manager=SimpleNamespace(_scrapers={})
        with patch('agent_core.web_runtime.PROFILE_DIR',Path(self.temp.name)), \
             patch('agent_core.web_runtime.get_manager',return_value=manager), \
             patch.object(browser_bridge,'shared_browser_enabled',return_value=True), \
             patch.object(browser_bridge,'shared_browser_endpoint',return_value=WS), \
             patch.object(WebLLMScraper,'_claim_conversation_owner'), \
             patch.object(WebLLMScraper,'release_conversation_owner',return_value=0):
            scraper=browser_bridge.adopt_page(self.original[0],self.context,self.pw)
            self.assertTrue(scraper._attached_over_cdp)
            self.assertEqual(scraper.get_cdp_endpoint(),WS)
            scraper.close()
        self.browser.close.assert_not_called()
        self.assertFalse(self.original[0].closed)


if __name__=='__main__': unittest.main()
