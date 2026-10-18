"""Browser component regression tests; fixtures never write real provider configs.

Run: python -B -m unittest agent_core.web_ui.tests.test_manual_composer -v
"""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from .. import dom_config
from ..factory import create_web_ui_for_page
from ..manual_adapter import install_picker, save_selection, unique_composer
from ..manual_browser import create_manual_page
from ..providers.m365.composer import read_composer_snapshot, validate_composer_prompt
from .write_send_box import SAMPLE_TEXT, read_text, write_sample


class ManualComposerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        original = dom_config.config_path
        self.config_patch = patch.object(dom_config, "config_path", side_effect=lambda provider, root=None: original(provider, root=root or self.root))
        self.config_patch.start()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()
        self.config_patch.stop()
        self.temporary.cleanup()

    def fixture(self, html: str, url="https://chatgpt.com/manual-dom-fixture"):
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=html))
        self.page.goto(url)
        install_picker(self.page)

    def select(self, selector: str):
        self.page.locator("#smartagent-composer-picker").get_by_role("button", name="開始選取", exact=True).click()
        self.page.locator(selector).click()
        return self.page.evaluate("() => window.__smartAgentComposerPicker.selected")

    def test_nested_contenteditable_save_reopen_write_runtime(self):
        self.fixture('<div id="custom-composer" role="textbox" contenteditable="true"><p><span>點這裡</span></p></div>')
        selection = self.select("#custom-composer span")
        self.assertEqual(selection["selector"], "#custom-composer")
        self.page.locator("#smartagent-composer-picker").get_by_role("button", name="儲存輸入框").click()
        self.assertEqual(self.page.evaluate("() => window.__smartAgentComposerPicker.action"), "save")
        path = save_selection(self.page, "chatgpt", selection)
        self.assertEqual(path.parent.name, "chatgpt")
        # New document proves no retained node/temporary marker is needed.
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<div id="custom-composer" role="textbox" contenteditable="true"></div>'))
        self.page.reload()
        result = write_sample(self.page)
        self.assertTrue(result["runtime_config_loaded"])
        self.assertFalse(result["submitted"])
        self.assertEqual(read_text(self.page.locator("#custom-composer")), SAMPLE_TEXT)

    def test_textarea_and_input(self):
        for markup in ('<textarea aria-label="訊息"></textarea>', '<input type="text" name="chat">'):
            with self.subTest(markup=markup):
                self.fixture(markup)
                selection = self.select("textarea,input")
                self.assertIsNotNone(selection)
                save_selection(self.page, "chatgpt", selection)
                self.assertTrue(write_sample(self.page)["roundtrip_match"])

    def test_invalid_click_does_not_activate_page_button(self):
        self.fixture('<button id="send" onclick="window.sent=true">Send</button><textarea></textarea>')
        self.assertIsNone(self.select("#send"))
        self.assertFalse(self.page.evaluate("() => !!window.sent"))
        self.assertTrue(self.page.locator("#smartagent-composer-picker").get_by_role("button", name="儲存輸入框").is_disabled())

    def test_disabled_readonly_password_and_nested_false_are_rejected(self):
        for markup, selector in (
            ('<textarea readonly>readonly</textarea>', 'textarea'),
            ('<input type="password">', 'input'),
            ('<div contenteditable="true"><span contenteditable="false">不可輸入</span></div>', 'span'),
        ):
            with self.subTest(markup=markup):
                self.fixture(markup)
                self.assertIsNone(self.select(selector))

    def test_ambiguous_composer_is_rejected(self):
        self.fixture('<textarea></textarea><textarea></textarea>')
        self.page.locator("#smartagent-composer-picker").get_by_role("button", name="開始選取", exact=True).click()
        self.page.locator("textarea").first.click()
        self.assertIsNone(self.page.evaluate("() => window.__smartAgentComposerPicker.selected"))
        with self.assertRaisesRegex(ValueError, "not_unique"):
            unique_composer(self.page, "textarea")

    def test_stale_selection_cannot_save(self):
        self.fixture('<textarea id="chat"></textarea>')
        selection = self.select("#chat")
        self.page.locator("#chat").evaluate("el => el.remove()")
        self.page.locator("#smartagent-composer-picker").get_by_role("button", name="儲存輸入框").click()
        self.assertEqual(self.page.evaluate("() => window.__smartAgentComposerPicker.action"), "")
        with self.assertRaises(ValueError):
            save_selection(self.page, "chatgpt", selection)

    def test_existing_draft_is_preserved(self):
        self.fixture('<textarea id="chat">使用者原有草稿</textarea>')
        save_selection(self.page, "chatgpt", self.select("#chat"))
        with self.assertRaisesRegex(ValueError, "contains_draft"):
            write_sample(self.page)
        self.assertEqual(self.page.locator("#chat").input_value(), "使用者原有草稿")

    def test_repeated_sample_is_allowed(self):
        self.fixture('<textarea id="chat"></textarea>')
        save_selection(self.page, "chatgpt", self.select("#chat"))
        self.assertFalse(write_sample(self.page)["reused_sample"])
        self.assertTrue(write_sample(self.page)["reused_sample"])

    def test_all_provider_profiles_load_their_config_only(self):
        urls = {"chatgpt": "https://chatgpt.com/", "gemini": "https://gemini.google.com/app",
                "claude": "https://claude.ai/new", "m365": "https://m365.cloud.microsoft/chat"}
        for provider, url in urls.items():
            with self.subTest(provider=provider):
                self.fixture('<textarea id="manual-only"></textarea>', url)
                dom_config.save_composer(provider, url, "#manual-only", {"tag": "textarea", "kind": "input"})
                adapter = create_web_ui_for_page(self.page)
                self.assertEqual(adapter.profile.composer_selector, "#manual-only")
                profiles = import_module(f"agent_core.web_ui.providers.{provider}.profiles")
                self.assertEqual(profiles.profile_by_name(adapter.profile.name).composer_selector, "#manual-only")
        doc = dom_config.load_dom_config("chatgpt")
        self.assertEqual(doc["scope"], "composer_only")
        self.assertFalse(doc["elements"]["composer"]["validation"]["full_conversation"])

    def test_config_history_and_corruption(self):
        url = "https://chatgpt.com/"
        path = dom_config.save_composer("chatgpt", url, "#a", {})
        dom_config.save_composer("chatgpt", url, "#b", {})
        self.assertEqual(len(list((path.parent / "dom_config.history").glob("*.json"))), 1)
        path.write_text('{"schema":"invalid"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema_mismatch"):
            dom_config.load_dom_config("chatgpt")

    def test_owned_browser_reuses_startup_blank(self):
        startup = self.page
        selected = create_manual_page(self.context, shared_browser=False)
        self.assertIs(selected, startup)
        self.assertEqual(len(self.context.pages), 1)

    def test_shared_browser_preserves_existing_blank(self):
        startup = self.page
        selected = create_manual_page(self.context, shared_browser=True)
        self.assertIsNot(selected, startup)
        self.assertEqual(startup.url, "about:blank")
        self.assertEqual(len(self.context.pages), 2)

    def test_owned_browser_preserves_existing_nonblank(self):
        self.fixture('<p>Original page</p>')
        original_url = self.page.url
        selected = create_manual_page(self.context, shared_browser=False)
        self.assertIsNot(selected, self.page)
        self.assertEqual(self.page.url, original_url)

    def test_m365_hidden_lexical_markers_are_not_message_text(self):
        self.fixture('<span id="chat" contenteditable="true"><p><span data-lexical-text="true">範本</span><span aria-hidden="true" data-lexical-text="true">\u200b\u200c</span></p></span>', 'https://m365.cloud.microsoft/chat')
        snapshot = read_composer_snapshot(self.page.locator('#chat'))
        self.assertIn('\u200b\u200c', snapshot.primary_text)
        self.assertTrue(validate_composer_prompt('範本', snapshot).matched)
        self.assertFalse(validate_composer_prompt('其他文字', snapshot).matched)

    def test_m365_preserves_user_zero_width_characters(self):
        self.fixture('<span id="chat" contenteditable="true"><p><span data-lexical-text="true">A\u200bB\u200cC</span><span aria-hidden="true">\u200b\u200c</span></p></span>', 'https://m365.cloud.microsoft/chat')
        snapshot = read_composer_snapshot(self.page.locator('#chat'))
        self.assertEqual(snapshot.semantic_text, 'A\u200bB\u200cC')
        self.assertTrue(validate_composer_prompt('A\u200bB\u200cC', snapshot).matched)
        self.assertFalse(validate_composer_prompt('ABC', snapshot).matched)

    def test_editor_requires_pointer_focus_before_input(self):
        self.fixture('''<span style="display:block;min-height:30px" id="chat" role="textbox" contenteditable="true"></span>
            <script>
            const editor=document.getElementById('chat');
            let activated=false;
            editor.onclick=()=>activated=true;
            editor.oninput=()=>{
                if (!activated) { editor.textContent=''; return; }
                for (const old of editor.querySelectorAll('[aria-hidden]')) old.remove();
                const marker=document.createElement('span');
                marker.setAttribute('aria-hidden','true'); marker.textContent='\\u200b\\u200c';
                editor.append(marker);
            };
            </script>''', 'https://m365.cloud.microsoft/chat')
        dom_config.save_composer('m365', self.page.url, '#chat', {'tag':'span','kind':'contenteditable'})
        result = write_sample(self.page)
        self.assertTrue(result['roundtrip_match'])
        self.assertEqual(result['matched_view'], 'semantic')

    def test_first_input_discarded_during_initialization_retries_only_empty(self):
        self.fixture('''<div id="chat" contenteditable="true" style="min-height:30px"></div>
            <script>
            window.inputs=0;
            document.getElementById('chat').addEventListener('beforeinput', event=>{
                window.inputs++;
                if(window.inputs===1) event.preventDefault();
            });
            </script>''')
        dom_config.save_composer('chatgpt', self.page.url, '#chat', {})
        result = write_sample(self.page)
        self.assertEqual(result['write_attempts'], 2)
        self.assertEqual(self.page.evaluate('window.inputs'), 2)

    def test_partial_or_different_text_is_never_retried(self):
        self.fixture('''<div id="chat" contenteditable="true" style="min-height:30px"></div>
            <script>
            window.clicks=0;
            document.getElementById('chat').onpointerdown=()=>window.clicks++;
            document.getElementById('chat').oninput=event=>{
                event.target.textContent='Different text';
            };
            </script>''')
        dom_config.save_composer('chatgpt', self.page.url, '#chat', {})
        with self.assertRaisesRegex(AssertionError, 'roundtrip_mismatch'):
            write_sample(self.page)
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        self.assertEqual(self.page.locator('#chat').inner_text(), 'Different text')

    def test_unified_flow_verifies_the_selected_page(self):
        from ..composer_calibration import calibrate_on_page
        self.fixture('<textarea id="chat"></textarea>')
        path = save_selection(self.page, 'chatgpt', self.select('#chat'))
        original_page = self.page
        with patch('agent_core.web_ui.composer_calibration.select_composer_on_page', return_value=path):
            result = calibrate_on_page(self.page, no_wait=True)
        self.assertTrue(result['verified_in_same_page'])
        self.assertTrue(result['roundtrip_match'])
        self.assertEqual(len(self.context.pages), 1)
        self.assertIs(self.context.pages[0], original_page)

    def test_unified_cancel_does_not_write_or_verify(self):
        from ..composer_calibration import calibrate_on_page
        self.fixture('<textarea id="chat"></textarea>')
        with patch('agent_core.web_ui.composer_calibration.select_composer_on_page', return_value=None), patch('agent_core.web_ui.composer_calibration.verify_composer_on_page') as verify:
            self.assertIsNone(calibrate_on_page(self.page, no_wait=True))
            verify.assert_not_called()
        self.assertFalse(dom_config.config_path('chatgpt').exists())
        self.assertEqual(self.page.locator('#chat').input_value(), '')


if __name__ == "__main__":
    unittest.main()
