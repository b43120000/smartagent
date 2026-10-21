"""All providers share submit identity while retaining native reply ownership."""
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from .. import dom_config
from ..factory import create_web_ui_for_page
from ..provider_registry import provider_from_url
from ...web_runtime import WebLLMScraper


URLS = (
    'https://chatgpt.com/c/fixture', 'https://claude.ai/chat/fixture',
    'https://gemini.google.com/app/fixture', 'https://m365.cloud.microsoft/chat',
    'https://custom.example/chat',
)
HTML = '''<!doctype html><meta charset="utf-8">
<textarea id="composer"></textarea><button id="send">Send</button><main>
<article class="user" id="user-old"><div class="user-text">previous request</div></article>
<article class="assistant" id="answer-old"><message-content id="message-content-id-r_old">
<div data-testid="markdown-reply" data-message-id="reply-old">old reply</div>
</message-content></article></main>'''


class SharedSubmitIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original = dom_config.config_path
        config = patch.object(dom_config, 'config_path', side_effect=lambda provider, root=None:
            original(provider, root=root or Path(self.temp.name)))
        config.start()
        self.addCleanup(config.stop)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=HTML))
        self.page = self.context.new_page()

    def adapter(self, url):
        self.page.goto(url)
        provider = provider_from_url(url)
        dom_config.save_composer(provider, url, '#composer', {})
        for name, selector in [('send', '#send'), ('user_turn', 'article.user'),
                               ('user_content', '.user-text'), ('assistant_turn', 'article.assistant')]:
            dom_config.save_element(provider, url, name, selector, {},
                                    {dom_config.VALIDATION_KEYS[name]: True})
        return create_web_ui_for_page(self.page)

    def test_transformed_fixed_count_user_binds_and_only_new_reply_is_owned(self):
        for url in URLS:
            with self.subTest(url=url):
                adapter = self.adapter(url)
                prompt = adapter.prepare_transport_prompt('original body')
                scope = adapter.capture_request(prompt)
                self.page.locator('.user-text').evaluate('(el,t)=>el.textContent=t',
                                                        prompt.replace('original', 'rendered'))
                self.assertIsNotNone(adapter.confirm_user_turn(scope))
                self.assertIsNone(adapter.latest_owned_assistant(scope))
                self.page.locator('article.assistant').evaluate('''el=>{
                    el.id='answer-new';
                    el.querySelector('message-content').id='message-content-id-r_new';
                    el.querySelector('[data-message-id]').dataset.messageId='reply-new';
                }''')
                self.assertIsNotNone(adapter.latest_owned_assistant(scope))
                self.page.locator('article.user').evaluate('el=>el.replaceWith(el.cloneNode(true))')
                self.assertIsNotNone(adapter.rebind_user_turn(scope))

    def test_quoted_old_nonce_and_missing_current_nonce_cannot_bind(self):
        for url in URLS:
            with self.subTest(url=url):
                adapter = self.adapter(url)
                old = adapter.prepare_transport_prompt('historical request')
                self.page.locator('.user-text').evaluate('(el,t)=>el.textContent=t', old)
                prompt = adapter.prepare_transport_prompt('quoted data:\n' + old)
                scope = adapter.capture_request(prompt)
                self.assertEqual(len(scope.request_markers), 1)
                self.assertNotEqual(scope.request_markers, adapter.capture_request(old).request_markers)
                self.assertIsNone(adapter.confirm_user_turn(scope))
                self.assertIsNone(adapter.rebind_user_turn(scope))
                self.assertIsNone(adapter.latest_owned_assistant(scope))

    def test_duplicate_current_nonce_is_rejected(self):
        for url in URLS:
            with self.subTest(url=url):
                adapter = self.adapter(url)
                prompt = adapter.prepare_transport_prompt('new request')
                scope = adapter.capture_request(prompt)
                self.page.locator('.user-text').evaluate('(el,t)=>el.textContent=t', prompt)
                self.page.locator('article.user').evaluate('''el=>{
                    const copy=el.cloneNode(true);copy.id='user-duplicate';el.after(copy);
                }''')
                self.assertIsNone(adapter.confirm_user_turn(scope))
                self.assertIsNone(adapter.rebind_user_turn(scope))
                bound, state = adapter.reconcile_user_turn(scope)
                self.assertIsNone(bound)
                self.assertEqual(state['reason'], 'submit_nonce_not_uniquely_visible')

    def test_each_recovery_gets_fresh_identity_used_for_capture_and_send(self):
        for url in URLS:
            with self.subTest(url=url):
                adapter = self.adapter(url)
                raw = 'repair data:\n' + adapter.prepare_transport_prompt('old prompt')
                fake = SimpleNamespace(service=provider_from_url(url), _web_ui_adapter=lambda: adapter,
                    _web_file_planner_prompt=lambda prompt: prompt,
                    _log_stage=Mock(), _protocol_recovery_admission_state=lambda: (True, {}),
                    _write_prompt_to_composer=Mock(), _wait_for_send_ready=Mock(return_value='send'),
                    _capture_request_turn_state=Mock(return_value={'scope': 'new'}),
                    _submit_verified_prompt=Mock(), _wait_for_user_sent=Mock())
                prompts = []
                for _ in range(2):
                    WebLLMScraper._send_protocol_recovery_probe(fake, {}, prompt_override=raw)
                    sent = fake._write_prompt_to_composer.call_args.args[0]
                    self.assertTrue(sent.endswith('\n' + raw))
                    fake._capture_request_turn_state.assert_called_with(sent)
                    fake._submit_verified_prompt.assert_called_with(
                        'send', prompt=sent, submit_snapshot={'scope': 'new'})
                    prompts.append(adapter.capture_request(sent).request_markers)
                self.assertNotEqual(*prompts)
                self.assertTrue(all(len(markers) == 1 for markers in prompts))

    def test_meta_repair_initial_and_source_followup_share_identity_contract(self):
        from ... import meta_recovery_dispatcher as meta
        root = Path(self.temp.name)
        (root / 'fixture.txt').write_text('fixture source', encoding='utf-8')
        for url in URLS:
            with self.subTest(url=url), ExitStack() as stack:
                adapter = self.adapter(url)
                pw = stack.enter_context(patch.object(meta, 'sync_playwright'))
                pw.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value = (
                    SimpleNamespace(contexts=[self.context]))
                for name, value in [('_build_request', ('issue-test', 'repair request')),
                                    ('_get_repair_page', (self.page, False)),
                                    ('_navigate', None), ('_dismiss_rate_limit_dialog', False),
                                    ('_wait_response', 'fixture response'), ('_atomic', None)]:
                    stack.enter_context(patch.object(meta, name, return_value=value))
                stack.enter_context(patch.object(meta, 'execution_page_lease', return_value=nullcontext()))
                stack.enter_context(patch.object(meta, 'create_web_ui_for_page', return_value=adapter))
                stack.enter_context(patch.object(meta, 'parse', side_effect=[
                    ({'type': 'SELF_REPAIR_PLAN', 'issue_id': 'issue-test',
                      'inspect_paths': ['fixture.txt']}, ''),
                    ({'type': 'SELF_REPAIR_PLAN', 'issue_id': 'issue-test'}, '')]))
                capture = stack.enter_context(patch.object(adapter, 'capture_request', wraps=adapter.capture_request))
                budget = stack.enter_context(patch.object(meta, 'ensure_webgpt_prompt_budget'))
                sent = []
                stack.enter_context(patch.object(meta, '_submit_with_rate_gate',
                    side_effect=lambda *args: sent.append(self.page.locator('#composer').input_value())))
                result = meta._dispatch_once_locked({}, 'fixture-cdp', root, Mock())
                self.assertEqual(result['status'], 'PLAN_READY')
                self.assertEqual(len(sent), 2)
                self.assertEqual([call.args[0] for call in capture.call_args_list], sent)
                self.assertEqual([call.args[0] for call in budget.call_args_list], sent)
                self.assertTrue(sent[0].endswith('\nrepair request'))
                self.assertIn('[SELF_REPAIR_SOURCE_CONTEXT]', sent[1])
                first, second = [adapter.capture_request(text).request_markers for text in sent]
                self.assertEqual(len(first), 1)
                self.assertEqual(len(second), 1)
                self.assertNotEqual(first, second)
