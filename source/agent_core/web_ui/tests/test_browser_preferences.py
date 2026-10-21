"""Global browser selection, menu persistence and all-provider routing."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_core import browser_preferences as prefs
from agent_core import workspace_manager
from . import test_shared_browser as fixtures
URLS = fixtures.URLS
from agent_core.web_runtime import WebLLMScraper, CHATGPT_CDP_ENDPOINT


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'browser_session.json'
        patcher = patch.object(prefs, 'settings_path', return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_and_all_ten_independent_identities(self):
        self.assertEqual(prefs.load_browser_preferences(), {'browser': 'chrome', 'instance': 1})
        identities = [prefs.browser_launch_selection({'browser': b, 'instance': n})
                      for b in ('chrome', 'edge') for n in range(1, 6)]
        self.assertEqual(len({x['profile_dir'] for x in identities}), 10)
        self.assertEqual(len({x['endpoint'] for x in identities}), 10)
        for selection in identities:
            prefs.save_browser_preferences(selection['browser'], selection['instance'])
            self.assertEqual(prefs.browser_launch_selection(), selection)

    def test_preserves_unrelated_settings_and_disables_personal_chrome_mode(self):
        self.path.write_text(json.dumps({'mode':'existing_chrome', 'user_data_dir':'keep'}))
        prefs.save_browser_preferences('edge', 5)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw['user_data_dir'], 'keep')
        self.assertEqual(raw['mode'], 'legacy')

    def test_invalid_values_cannot_overwrite_valid_selection(self):
        prefs.save_browser_preferences('chrome', 2)
        for browser, instance in [('firefox', 1), ('edge', 0), ('edge', 6), ('chrome', True), ('chrome', '1')]:
            with self.subTest(browser=browser, instance=instance), self.assertRaises(ValueError):
                prefs.save_browser_preferences(browser, instance)
        self.assertEqual(prefs.load_browser_preferences()['instance'], 2)
        self.path.write_text('{invalid')
        with self.assertRaises(ValueError): prefs.load_browser_preferences()

    def test_menu_default_selection_edge_selection_cancel_and_invalid_input(self):
        for answers, expected in [(['', ''], ('chrome', 1)), (['2', '9', '5'], ('edge', 5))]:
            with patch('builtins.input', side_effect=answers), redirect_stdout(io.StringIO()):
                self.assertEqual(workspace_manager._browser_menu(), 0)
            self.assertEqual(prefs.load_browser_preferences(), dict(browser=expected[0], instance=expected[1]))
        before = self.path.read_bytes()
        with patch('builtins.input', side_effect=['1', '0']), redirect_stdout(io.StringIO()):
            workspace_manager._browser_menu()
        self.assertEqual(self.path.read_bytes(), before)

    def test_main_menu_routes_browser_settings(self):
        snapshot = dict(security_status='CONFIGURED', writable_workspaces=[], read_only_roots=[])
        with patch.object(workspace_manager, '_binding_snapshot', return_value=snapshot), \
             patch('builtins.input', return_value='5'), \
             patch.object(workspace_manager, '_browser_menu', return_value=0) as menu, \
             redirect_stdout(io.StringIO()):
            workspace_manager._interactive()
        menu.assert_called_once()


class ManagedRoutingTests(unittest.TestCase):
    setUp = fixtures.SharedBrowserTests.setUp
    def test_release_cannot_close_other_provider_with_same_conversation_id(self):
        with patch('agent_core.web_runtime.PROFILE_DIR', Path(self.temp.name)), \
             patch.object(WebLLMScraper, 'release_conversation_owner', return_value=0):
            scraper = WebLLMScraper('claude')
            scraper._browser = self.context
            scraper._page = self.original[3]
            self.assertTrue(scraper.release_conversation(URLS['claude']))
        self.assertTrue(self.original[3].closed)
        self.assertFalse(self.original[1].closed)

    def test_remote_manager_ignores_previous_browser_endpoint_after_selection_change(self):
        from types import SimpleNamespace
        from agent_core.remote_browser_session_manager import RemoteBrowserSessionManager
        from unittest.mock import Mock
        import os
        for previous_endpoint in ['http://127.0.0.1:1273', CHATGPT_CDP_ENDPOINT]:
            observed = []
            scraper = Mock(service='chatgpt', cfg={})
            scraper.start.side_effect = lambda: observed.append((os.environ.get('SMARTAGENT_ATTACH_CDP'), os.environ.get('SMARTAGENT_CHATGPT_CDP')))
            scraper.get_cdp_endpoint.return_value = CHATGPT_CDP_ENDPOINT
            host = SimpleNamespace(_remote_browser_scraper=None, _remote_browser_state={}, token='fixture',
                                   _remote_binding=lambda: dict(gpt_url=URLS['chatgpt'], workspace=self.temp.name),
                                   live_local_agent_state=lambda: dict(cdp_endpoint=previous_endpoint))
            with patch('agent_core.web_runtime.WebLLMScraper', return_value=scraper), \
                 patch('agent_core.shared_browser.shared_browser_enabled', return_value=False), \
                 patch.dict(os.environ, {'SMARTAGENT_CHATGPT_CDP':'stale-inherited-endpoint'}):
                RemoteBrowserSessionManager(host).ensure()
            expected = ('1', CHATGPT_CDP_ENDPOINT) if previous_endpoint == CHATGPT_CDP_ENDPOINT else (None, None)
            self.assertEqual(observed, [expected])

    def test_managed_webdirect_adoption_does_not_close_shared_context(self):
        from types import SimpleNamespace
        from WebAgent import browser_bridge
        manager = SimpleNamespace(_scrapers={})
        self.context.close = unittest.mock.Mock()
        with patch('agent_core.web_runtime.PROFILE_DIR', Path(self.temp.name)), \
             patch('agent_core.web_runtime.get_manager', return_value=manager), \
             patch.object(browser_bridge, 'shared_browser_enabled', return_value=False), \
             patch.object(WebLLMScraper, '_claim_conversation_owner'), \
             patch.object(WebLLMScraper, 'release_conversation_owner', return_value=0):
            scraper = browser_bridge.adopt_page(self.original[0], self.context, self.pw)
            self.assertEqual(scraper.get_cdp_endpoint(), CHATGPT_CDP_ENDPOINT)
            scraper.close()
        self.context.close.assert_not_called()
        self.browser.close.assert_not_called()

    def test_managed_runtime_all_providers_bootstrap_and_attach_same_browser(self):
        def bind(scraper, playwright=None):
            scraper._pw = self.pw
            scraper._owns_playwright = False
        with patch('agent_core.web_runtime.PROFILE_DIR', Path(self.temp.name)), \
             patch('agent_core.web_runtime.shared_browser_enabled', return_value=False), \
             patch('agent_core.web_ui.manual_browser.ensure_agent_browser') as ensure, \
             patch.object(WebLLMScraper, '_bind_playwright', bind), \
             patch.object(WebLLMScraper, '_is_logged_in', return_value=True), \
             patch.object(WebLLMScraper, '_dismiss_known_blocking_dialogs'), \
             patch.object(WebLLMScraper, '_start_attached_chatgpt_conversation'), \
             patch.object(WebLLMScraper, '_claim_conversation_owner'), \
             patch.object(WebLLMScraper, 'release_conversation_owner', return_value=0), \
             patch('agent_core.web_runtime.time.sleep'), \
             patch.dict('os.environ', {'SMARTAGENT_ATTACH_CDP':'', 'SMARTAGENT_ISOLATED_BROWSER':'',
                                       'SMARTAGENT_CHATGPT_CDP':'stale-inherited-endpoint'}):
            profiles = []
            for provider, url in URLS.items():
                scraper = WebLLMScraper(provider)
                scraper.cfg = dict(scraper.cfg, url=url)
                profiles.append(scraper._profile_dir)
                scraper.start(playwright=self.pw)
                self.pw.chromium.connect_over_cdp.assert_called_with(CHATGPT_CDP_ENDPOINT)
                self.assertIs(scraper._browser, self.context)
                self.assertEqual(scraper._page.url, url)
                scraper.close()
            self.assertEqual(ensure.call_count, 4)
            self.assertEqual(len(set(profiles)), 1)
        self.pw.chromium.launch_persistent_context.assert_not_called()
        self.browser.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
