"""Timestamp-bearing Other user containers, using a local browser fixture."""
from pathlib import Path
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from . import test_components as fixtures
from .. import dom_config
from .. import component_calibration as flow
from ..selection_preflight import check_component_selection
from ..factory import create_web_ui_for_page
from ..provider_registry import provider_from_url

USER = 'article[data-testid="user-message"]'
BODY = 'span[data-testid="user-message-content"]'
FIXTURE = fixtures.HTML.replace("user.setAttribute('data-content-search-unit-key',`fallback-turn-${n}:0:user`);", "user.setAttribute('data-testid','user-message'); const stamp=document.createElement('time');stamp.textContent='2026-10-19 14:51:25';user.append(stamp);").replace("const content=document.createElement('div');content.className", "const content=document.createElement('span');content.dataset.testid='user-message-content';content.className")

class OtherTimestampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(channel='chrome',headless=True)
    tearDownClass=classmethod(fixtures.ComponentTests.tearDownClass.__func__)
    setUp=fixtures.ComponentTests.setUp
    tearDown=fixtures.ComponentTests.tearDown

    def prepare_other(self):
        self.context.unroute('**/*')
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body=FIXTURE))
        self.page.goto('https://internal.example/chat')
        self.provider=provider_from_url(self.page.url)
        dom_config.save_composer(self.provider,self.page.url,'#composer',{})
        dom_config.save_element(self.provider,self.page.url,'send','button[aria-label="Send"]',{}, {'click_verified':True})

    def select(self,page,stage,probe):
        page.evaluate("() => {document.querySelector('#smartagent-component-picker')?.remove();delete window.__smartAgentComponentPicker;}")
        options={'field':flow.STAGES[stage],'marker':probe['marker'],'title':'Test','instructions':'Select'}
        if stage==4:options['rootSelector']=USER
        page.evaluate(flow.PICKER,options)
        panel=page.locator('#smartagent-component-picker');panel.locator('#pick').click()
        target=page.locator(USER).filter(has_text=probe['marker'])
        if stage==4:target=target.locator(BODY)
        target.evaluate("el=>el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}))")
        selection=page.evaluate('() => window.__smartAgentComponentPicker.selected')
        result=check_component_selection(page,stage,probe,selection)
        self.assertTrue(result['passed'],result)
        page.evaluate('() => window.__smartAgentComponentPicker.hide()')
        return selection

    def test_two_turns_with_timestamps_then_exact_body_stage4(self):
        self.prepare_other()
        with patch.object(flow,'select_component',side_effect=self.select):
            for stage in (3,4):
                evidence=self.root/str(stage);evidence.mkdir()
                result=flow.calibrate_stage(self.page,stage,evidence,no_wait=True)
                self.assertTrue(result['passed'])
                if stage==3:self.assertEqual(len(set(result['distinct_user_turn_ids'])),2)
        adapter=create_web_ui_for_page(self.page)
        self.assertEqual(len(adapter.turns('user')),4)
        self.assertTrue(all('2026-10-19 14:51:25' not in t.raw_text for t in adapter.turns('user')))
        node=self.page.locator(USER).last.locator(BODY)
        node.evaluate("el=>el.textContent='約定日期 2026-10-19 14:51:25'")
        self.assertEqual(adapter.turns('user')[-1].raw_text,'約定日期 2026-10-19 14:51:25')
        self.assertEqual(self.page.evaluate('window.submits'),4)

    def test_duplicate_and_multi_message_containers_rejected(self):
        self.prepare_other()
        probe=flow.make_probe(self.page);flow.prepare_probe(self.page,probe);flow.send_probe(self.page,probe,'button',timeout=5)
        self.page.locator(USER).evaluate('el=>el.after(el.cloneNode(true))')
        adapter=flow.candidate_adapter(self.page,'user_turn',USER)
        with self.assertRaisesRegex(ValueError,'new_user_turn_not_unique'):
            flow.stage3_user_turns(adapter,probe)
        adapter=flow.candidate_adapter(self.page,'user_turn','#messages')
        with self.assertRaisesRegex(ValueError,'user_container_includes_other_turns'):
            flow.stage3_user_turns(adapter,probe)

    def test_builtin_profile_keeps_exact_text_comparison(self):
        from types import SimpleNamespace
        adapter=SimpleNamespace(provider_name='m365',turns=lambda role:[SimpleNamespace(raw_text='2026-10-19 prompt')])
        self.assertEqual(flow.stage3_user_turns(adapter,{'prompt':'prompt'}),[])
