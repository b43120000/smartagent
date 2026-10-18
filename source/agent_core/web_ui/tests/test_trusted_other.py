"""Trusted Types enforced pages and origin-isolated Other URL calibration."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from .. import dom_config
from ..factory import create_web_ui_for_page, normalize_web_conversation_url
from ..manual_adapter import install_picker, save_selection
from ..component_calibration import PICKER
from ..provider_registry import provider_from_url
from .write_send_box import write_sample


class TrustedOtherTests(unittest.TestCase):
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
        original = dom_config.config_path
        self.patcher = patch.object(dom_config, 'config_path', side_effect=lambda provider, root=None:
            original(provider, root=root or Path(self.temp.name)))
        self.patcher.start()
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.fulfill(
            headers={'Content-Security-Policy': "require-trusted-types-for 'script'; trusted-types 'none'"},
            content_type='text/html; charset=utf-8',
            body='<textarea id="composer"></textarea><button aria-label="Send">Send</button>'))
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()
        self.patcher.stop()
        self.temp.cleanup()

    def test_gemini_composer_picker_under_enforced_trusted_types(self):
        self.page.goto('https://gemini.google.com/app')
        with self.assertRaisesRegex(Exception, 'TrustedHTML'):
            self.page.evaluate("() => document.body.innerHTML = '<p>blocked</p>'")
        install_picker(self.page)
        panel = self.page.locator('#smartagent-composer-picker')
        panel.get_by_role('button', name='開始選取', exact=True).click()
        self.page.locator('#composer').click()
        panel.get_by_role('button', name='儲存輸入框').click()
        state = self.page.evaluate('() => window.__smartAgentComposerPicker')
        self.assertEqual(state['action'], 'save')
        save_selection(self.page, 'gemini', state['selected'])
        self.assertTrue(write_sample(self.page)['roundtrip_match'])

    def test_component_picker_under_enforced_trusted_types(self):
        self.page.goto('https://gemini.google.com/app')
        self.page.evaluate(PICKER, {'field':'send', 'marker':'test', 'title':'Send', 'instructions':'Select'})
        panel = self.page.locator('#smartagent-component-picker')
        panel.get_by_role('button', name='開始選取', exact=True).click()
        self.page.locator('button[aria-label="Send"]').click()
        panel.get_by_role('button', name='儲存並測試').click()
        self.assertEqual(self.page.evaluate('() => window.__smartAgentComponentPicker.action'), 'save')

    def test_other_picker_save_and_runtime_input_with_origin_isolation(self):
        first = 'https://custom.example/chat/one'
        self.assertEqual(normalize_web_conversation_url(first), first)
        provider = provider_from_url(first)
        self.assertEqual(provider, provider_from_url('https://custom.example/chat/two'))
        self.assertNotEqual(provider, provider_from_url('https://custom.example:8443/chat'))
        self.assertNotEqual(provider, provider_from_url('https://another.example/chat'))
        self.assertEqual(provider, provider_from_url('https://custom.example:443/chat'))
        ipv6 = provider_from_url('http://[::1]:8080/chat')
        dom_config.save_composer(ipv6, 'http://[::1]:8080/chat', '#composer', {})
        self.assertEqual(dom_config.load_dom_config(ipv6)['source_origin'], 'http://[::1]:8080')
        self.page.goto(first)
        install_picker(self.page)
        panel = self.page.locator('#smartagent-composer-picker')
        panel.get_by_role('button', name='開始選取', exact=True).click()
        self.page.locator('#composer').click()
        selection = self.page.evaluate('() => window.__smartAgentComposerPicker.selected')
        path = save_selection(self.page, provider, selection)
        self.assertEqual(path.parent.parent.name, 'other')
        result = write_sample(self.page)
        self.assertTrue(result['roundtrip_match'])
        self.assertTrue(result['runtime_config_loaded'])
        adapter = create_web_ui_for_page(self.page)
        self.page.goto('https://another.example/chat')
        with self.assertRaisesRegex(ValueError, 'origin_changed'):
            adapter.visible_composer()
        with self.assertRaisesRegex(ValueError, 'config_missing'):
            create_web_ui_for_page(self.page)

    def test_other_invalid_urls_and_config_origin_mismatch_rejected(self):
        for value in ['file:///tmp/x', 'javascript:alert(1)', 'https://', 'https://a:b@custom.example/']:
            with self.assertRaises(ValueError):
                normalize_web_conversation_url(value)
        provider = provider_from_url('https://custom.example/')
        with self.assertRaisesRegex(ValueError, 'url_provider_mismatch'):
            dom_config.save_composer(provider, 'https://another.example/', '#composer', {})

    def test_other_stages_two_to_five_through_real_picker_clicks(self):
        from .test_components import HTML, USER, ASSISTANT
        from ..component_calibration import calibrate_stage, STAGES
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(
            headers={'Content-Security-Policy': "require-trusted-types-for 'script'; trusted-types 'none'"},
            content_type='text/html; charset=utf-8', body=HTML))
        self.page.goto('https://custom.example/chat')
        provider = provider_from_url(self.page.url)
        dom_config.save_composer(provider, self.page.url, '#composer', {})

        def select(page, stage, probe):
            # Reinstall panel between stages just as separate batch tabs do.
            page.evaluate("() => { document.querySelector('#smartagent-component-picker')?.remove(); delete window.__smartAgentComponentPicker; }")
            options = {'field': STAGES[stage], 'marker': probe['marker'], 'title': 'Test', 'instructions': 'Select'}
            if stage == 4:
                options['rootSelector'] = dom_config.load_dom_config(provider)['elements']['user_turn']['selector']
            page.evaluate(PICKER, options)
            panel = page.locator('#smartagent-component-picker')
            panel.get_by_role('button', name='開始選取', exact=True).click()
            if stage == 2:
                target = page.locator('button[aria-label="Send"]')
            elif stage == 5:
                target = page.locator(ASSISTANT).filter(has_text=probe['marker']).locator('p')
            else:
                target = page.locator(USER).filter(has_text=probe['marker']).locator('.user-text')
            target.click()
            panel.get_by_role('button', name='儲存並測試').click()
            selection = page.evaluate('() => window.__smartAgentComponentPicker.selected')
            page.evaluate('() => window.__smartAgentComponentPicker.hide()')
            return selection

        with patch('agent_core.web_ui.component_calibration.select_component', side_effect=select):
            for stage in range(2, 6):
                evidence = Path(self.temp.name) / str(stage)
                evidence.mkdir()
                result = calibrate_stage(self.page, stage, evidence, no_wait=True)
                self.assertTrue(result['passed'])
                self.assertTrue(result['runtime_config_loaded'])
        self.assertEqual(self.page.evaluate('window.submits'), 7)


    def test_gemini_content_can_select_same_paragraph_as_user_turn(self):
        from .test_components import HTML
        from ..component_calibration import calibrate_stage
        fixture = HTML.replace("const content=document.createElement('div')", "const content=document.createElement('p')").replace('user-text whitespace-pre-wrap', 'query-text-line')
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=fixture))
        self.page.goto('https://gemini.google.com/app/test')
        dom_config.save_composer('gemini', self.page.url, '#composer', {})
        for name, selector in [('send', 'button[aria-label="Send"]'), ('user_turn', 'p.query-text-line')]:
            dom_config.save_element('gemini', self.page.url, name, selector, {}, {dom_config.VALIDATION_KEYS[name]: True})

        def select(page, stage, probe):
            page.evaluate(PICKER, {'field':'user_content', 'marker':probe['marker'],
                'title':'Test', 'instructions':'Select', 'rootSelector':'p.query-text-line'})
            panel = page.locator('#smartagent-component-picker')
            panel.get_by_role('button', name='開始選取', exact=True).click()
            page.locator('.answer').click()
            self.assertTrue(panel.get_by_role('button', name='儲存並測試').is_disabled())
            page.locator('p.query-text-line').filter(has_text=probe['marker']).click()
            panel.get_by_role('button', name='儲存並測試').click()
            state = page.evaluate('() => window.__smartAgentComponentPicker')
            self.assertEqual(state['selected']['selector'], ':scope')
            self.assertEqual(state['action'], 'save')
            page.evaluate('() => window.__smartAgentComponentPicker.hide()')
            return state['selected']

        with patch('agent_core.web_ui.component_calibration.select_component', side_effect=select):
            result = calibrate_stage(self.page, 4, Path(self.temp.name), no_wait=True)
        self.assertTrue(result['passed'])
        self.assertTrue(result['runtime_config_loaded'])
        document = dom_config.load_dom_config('gemini')
        self.assertEqual(document['elements']['user_turn']['selector'], 'p.query-text-line')
        self.assertEqual(document['elements']['user_content']['selector'], ':scope')


    def test_gemini_assistant_container_uses_unique_child_response_id(self):
        from .test_components import HTML, USER
        from ..component_calibration import calibrate_stage
        fixture = HTML.replace("assistant.append(p);", "const message=document.createElement('message-content');message.id='message-content-id-r_'+n;message.append(p);assistant.className='response-container-content';assistant.append(message);")
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=fixture))
        self.page.goto('https://gemini.google.com/app/test')
        dom_config.save_composer('gemini', self.page.url, '#composer', {})
        for name, selector in [('send', 'button[aria-label="Send"]'), ('user_turn', USER), ('user_content', '.user-text')]:
            dom_config.save_element('gemini', self.page.url, name, selector, {}, {dom_config.VALIDATION_KEYS[name]: True})

        def select(page, stage, probe):
            page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            return {'selector': '.response-container-content', 'tag': 'article', 'kind': 'assistant_turn'}

        with patch('agent_core.web_ui.component_calibration.select_component', side_effect=select):
            result = calibrate_stage(self.page, 5, Path(self.temp.name), no_wait=True)
        self.assertTrue(result['newest_assistant_owned'])
        self.assertTrue(result['old_response_rejected'])
        adapter = create_web_ui_for_page(self.page)
        turns = adapter.turns('assistant')
        self.assertEqual(len(turns), 2)
        self.assertNotEqual(turns[0].structural_id, turns[1].structural_id)
        self.assertIn(('data-response-id', 'message-content-id-r_2'), turns[1].stable_attributes)
        self.page.locator('.response-container-content').last.evaluate('el=>el.setAttribute("aria-busy","false")')
        self.assertEqual(turns[1].structural_id, adapter.turns('assistant')[1].structural_id)

    def test_gemini_assistant_identity_must_be_inside_selected_frame_and_unique(self):
        self.page.goto('https://gemini.google.com/app/test')
        dom_config.save_composer('gemini', self.page.url, '#composer', {})
        self.page.locator('textarea').evaluate('''el=>{
            const frame=document.createElement('section');frame.className='frame';
            for (const id of ['one','two']) {
                const m=document.createElement('message-content');m.id='message-content-id-r_'+id;
                const p=document.createElement('p');p.textContent=id;m.append(p);frame.append(m);
            }
            document.body.append(frame);
        }''')
        adapter = create_web_ui_for_page(self.page)
        frame = self.page.locator('.frame').element_handle()
        self.assertFalse(adapter._turn_ref(frame, 'assistant', 1).stable_attributes)
        # A paragraph cannot borrow identity from an ancestor or sibling.
        paragraph = self.page.locator('.frame p').first.element_handle()
        self.assertFalse(adapter._turn_ref(paragraph, 'assistant', 1).stable_attributes)


if __name__ == '__main__':
    unittest.main()
