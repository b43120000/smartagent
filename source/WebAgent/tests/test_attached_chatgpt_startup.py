"""Regression coverage for CDP ChatGPT startup without load-event waits."""
import os
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_core.web_runtime import WebLLMScraper

TARGET = 'https://chatgpt.com/g/g-project/c/12345678-abcd-abcd-abcd-123456789abc'


class Page:
    def __init__(self, url='about:blank'):
        self.url = url
        self.closed = False
        self.navigate = True
        self.clock = 0.0
        self.evaluations = []
        self.close = Mock()
        self.goto = Mock(side_effect=AssertionError('must not wait for goto'))

    def evaluate(self, script, target):
        self.evaluations.append((script, target))
        if self.navigate:
            self.url = target
        return True

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000


def scraper(page):
    s = WebLLMScraper('chatgpt')
    s.cfg = dict(s.cfg, url=TARGET)
    s._page = page
    s._log_stage = Mock()
    s._emit_status = Mock()
    s._web_ui_adapter = Mock(return_value=SimpleNamespace(probe_compatibility=Mock(), visible_composer=Mock(return_value=Mock(is_visible=Mock(return_value=True)))))
    return s


class StartupTests(unittest.TestCase):
    def test_scheduled_navigation_and_ready(self):
        p = Page()
        s = scraper(p)
        s._start_attached_chatgpt_conversation()
        self.assertIn('setTimeout', p.evaluations[0][0])
        self.assertEqual(p.evaluations[0][1], TARGET)
        p.goto.assert_not_called()

    def test_existing_conversation_is_not_reloaded(self):
        p = Page(TARGET.replace('/g/g-project', ''))
        scraper(p)._start_attached_chatgpt_conversation()
        self.assertFalse(p.evaluations)

    def test_wrong_provider_same_id_is_rejected(self):
        p = Page(TARGET.replace('chatgpt.com', 'example.com'))
        self.assertFalse(scraper(p)._attached_chatgpt_target_matches())

    def test_wrong_conversation_times_out(self):
        p = Page(TARGET.replace('123456789abc', '000000000000'))
        p.navigate = False
        s = scraper(p)
        with patch('agent_core.web_runtime.time.monotonic', side_effect=lambda: p.clock):
            with self.assertRaisesRegex(RuntimeError, 'target_match=False'):
                s._start_attached_chatgpt_conversation(timeout_ms=400)
        s._web_ui_adapter.assert_not_called()

    def test_composer_required_even_for_matching_conversation(self):
        p = Page(TARGET)
        s = scraper(p)
        s._web_ui_adapter().visible_composer.return_value = None
        with patch('agent_core.web_runtime.time.monotonic', side_effect=lambda: p.clock):
            with self.assertRaisesRegex(RuntimeError, 'target_match=True'):
                s._start_attached_chatgpt_conversation(timeout_ms=400)
        p.close.assert_not_called()

    def test_delayed_composer_is_polled(self):
        p = Page(TARGET)
        s = scraper(p)
        s._web_ui_adapter().visible_composer.side_effect = [None, Mock(is_visible=Mock(return_value=True))]
        with patch('agent_core.web_runtime.time.monotonic', side_effect=lambda: p.clock):
            s._start_attached_chatgpt_conversation(timeout_ms=400)
        self.assertEqual(p.clock, .2)

    def test_initial_dom_profile_missing_is_polled(self):
        p = Page(TARGET)
        s = scraper(p)
        s._web_ui_adapter().visible_composer.side_effect = [
            RuntimeError('WEB_UI_PROFILE_UNSUPPORTED: composer_missing'),
            Mock(is_visible=Mock(return_value=True)),
        ]
        with patch('agent_core.web_runtime.time.monotonic', side_effect=lambda: p.clock):
            s._start_attached_chatgpt_conversation(timeout_ms=400)
        self.assertEqual(p.clock, .2)
        s._web_ui_adapter().probe_compatibility.assert_called_with(force=True)

    def test_hidden_composer_is_not_ready(self):
        p = Page(TARGET)
        s = scraper(p)
        s._web_ui_adapter().visible_composer.return_value.is_visible.return_value = False
        with patch('agent_core.web_runtime.time.monotonic', side_effect=lambda: p.clock):
            with self.assertRaisesRegex(RuntimeError, 'target_match=True'):
                s._start_attached_chatgpt_conversation(timeout_ms=400)

    def test_driver_disconnect_is_not_swallowed(self):
        p = Page()
        p.evaluate = Mock(side_effect=RuntimeError('Connection closed while reading from the driver'))
        with self.assertRaisesRegex(RuntimeError, 'Connection closed'):
            scraper(p)._start_attached_chatgpt_conversation()

    def test_context_replacement_is_rechecked(self):
        p = Page()
        def navigate(*args):
            p.url = TARGET
            raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
        p.evaluate = navigate
        scraper(p)._start_attached_chatgpt_conversation()

    def test_closed_page_fails(self):
        p = Page(TARGET)
        p.closed = True
        with self.assertRaisesRegex(RuntimeError, 'page_closed'):
            scraper(p)._start_attached_chatgpt_conversation()

    def test_start_reuses_target_after_readiness_timeout(self):
        p = Page()
        s = scraper(p)
        context = SimpleNamespace(pages=[], new_page=Mock(return_value=p))
        pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=Mock(
            return_value=SimpleNamespace(contexts=[context]))))
        def bind(_):
            s._pw = pw
            s._owns_playwright = False
        s._bind_playwright = bind
        s._mark_page_canonical = Mock()
        s._page_has_canonical_marker = Mock(return_value=True)
        s._claim_conversation_owner = Mock()
        s._dismiss_known_blocking_dialogs = Mock()
        s._is_logged_in = Mock(return_value=True)
        def navigate():
            p.url = TARGET
            context.pages = [p]
            if helper.call_count == 1:
                raise RuntimeError('chatgpt_startup_not_ready: target_match=True')
        helper = Mock(side_effect=navigate)
        s._start_attached_chatgpt_conversation = helper
        with patch.dict(os.environ, {'SMARTAGENT_ATTACH_CDP': '1', 'SMARTAGENT_ISOLATED_BROWSER': '0'}), \
             patch('agent_core.web_runtime.shared_browser_enabled', return_value=False), \
             patch('agent_core.web_runtime.exclusive_process_lock', return_value=nullcontext()), \
             patch('agent_core.web_runtime.time.sleep'):
            s.start(playwright=pw)
        self.assertEqual(helper.call_count, 2)
        context.new_page.assert_called_once()
        p.close.assert_not_called()
        p.goto.assert_not_called()


if __name__ == '__main__':
    unittest.main()
