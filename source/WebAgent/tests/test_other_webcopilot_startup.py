"""Other URL startup/config/input bridge integration without external network."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from WebAgent.browser_bridge import normalize_conversation_url, BrowserInputBridge
from agent_core.conversation_identity import conversation_id, same_conversation
from agent_core.web_ui import dom_config
from agent_core.web_ui.factory import create_web_ui_for_page
from agent_core.web_ui.provider_registry import provider_from_url

URL='https://internal.example/qa/#/room/one'
HTML='<textarea id="composer"></textarea><button id="send">Send</button><script>window.nativeSends=0;document.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey)window.nativeSends++;});document.querySelector("#send").onclick=()=>window.nativeSends++;</script>'

class OtherStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(channel='chrome',headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();original=dom_config.config_path
        self.patcher=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:original(provider,root=Path(self.temp.name)))
        self.patcher.start();self.context=self.browser.new_context()
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=HTML))
        self.page=self.context.new_page();self.page.goto(URL)
        self.provider=provider_from_url(URL)
        dom_config.save_composer(self.provider,URL,'#composer',{})
        dom_config.save_element(self.provider,URL,'send','#send',{}, {'click_verified':True})
    def tearDown(self):
        self.context.close();self.patcher.stop();self.temp.cleanup()
    def test_arbitrary_paths_and_hash_routes_have_stable_separate_identity(self):
        for url in (URL,'https://internal.example','https://internal.example/chat?room=one'):
            self.assertTrue(normalize_conversation_url(url));self.assertTrue(conversation_id(url))
        self.assertTrue(same_conversation(URL,URL))
        self.assertFalse(same_conversation(URL,URL.replace('/one','/two')))
        self.assertFalse(same_conversation(URL,URL.replace('internal.example','another.example')))
    def test_runtime_loads_only_origin_owned_config(self):
        adapter=create_web_ui_for_page(self.page)
        self.assertEqual(adapter.provider_name,self.provider)
        self.assertEqual(adapter.profile.composer_selector,'#composer')
        self.assertEqual(adapter.profile.send_selector,'#send')
        self.assertEqual(dom_config.config_path(self.provider).parent.parent.name,'other')
        self.page.goto('https://another.example/qa')
        with self.assertRaisesRegex(ValueError,'origin_changed'):adapter._active_profile()
        with self.assertRaisesRegex(ValueError,'config_missing'):create_web_ui_for_page(self.page)
    def test_trusted_enter_and_click_capture_once_using_calibrated_dom(self):
        bridge=BrowserInputBridge(self.page);bridge.install();bridge.install()
        self.assertTrue(bridge.installed())
        box=self.page.locator('#composer');box.fill('request one');box.press('Enter')
        self.assertEqual(bridge.pop()['text'],'request one');self.assertIsNone(bridge.pop())
        self.assertEqual(self.page.evaluate('window.nativeSends'),0)
        self.assertEqual(box.input_value(),'')
        box.fill('request two');self.page.locator('#send').click()
        self.assertEqual(bridge.pop()['text'],'request two');self.assertIsNone(bridge.pop())
        self.assertEqual(self.page.evaluate('window.nativeSends'),0)
    def test_automation_shift_enter_and_untrusted_events_are_not_captured(self):
        bridge=BrowserInputBridge(self.page);bridge.install()
        box=self.page.locator('#composer');box.fill('draft');box.press('Shift+Enter')
        self.assertIsNone(bridge.pop())
        box.evaluate('el=>el.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}))')
        self.assertIsNone(bridge.pop())
        self.page.evaluate('window.__webAgentAutomationSubmit=true');box.fill('software');box.press('Enter')
        self.assertIsNone(bridge.pop());self.assertIn('software',box.input_value())
    def test_bridge_is_page_scoped_and_reloads_only_same_origin(self):
        bridge=BrowserInputBridge(self.page);bridge.install()
        sibling=self.context.new_page();sibling.goto(URL)
        self.assertFalse(sibling.evaluate('!!window.__webAgentDirectBridgeInstalled'))
        self.page.reload();self.assertTrue(bridge.installed())
        self.page.locator('#composer').fill('after reload');self.page.locator('#composer').press('Enter')
        self.assertEqual(bridge.pop()['text'],'after reload')
        self.page.goto('https://another.example/qa')
        self.assertFalse(self.page.evaluate('!!window.__webAgentDirectBridgeInstalled'))
    def test_reinstall_refreshes_calibrated_selectors_without_duplicate_handlers(self):
        bridge=BrowserInputBridge(self.page);bridge.install()
        self.page.evaluate("() => {document.querySelector('#composer').id='new-composer';document.querySelector('#send').id='new-send';}")
        dom_config.save_composer(self.provider,URL,'#new-composer',{})
        dom_config.save_element(self.provider,URL,'send','#new-send',{}, {'click_verified':True})
        fresh=bridge;fresh.install()
        self.page.locator('#new-composer').fill('new mapping');self.page.locator('#new-send').click()
        self.assertEqual(fresh.pop()['text'],'new mapping');self.assertIsNone(fresh.pop())
        self.assertEqual(self.page.evaluate('window.nativeSends'),0)

    def test_controller_reports_incomplete_dom_before_session_or_ownership(self):
        from types import SimpleNamespace
        from WebAgent.controller import prepare_browser_input_bridge
        p=dom_config.config_path(self.provider);p.unlink()
        dom_config.save_composer(self.provider,URL,'#composer',{})
        stopped=[]
        pw=SimpleNamespace(stop=lambda:stopped.append(True))
        self.assertIsNone(prepare_browser_input_bridge(self.page,pw))
        self.assertEqual(stopped,[True])
        self.assertFalse(self.page.is_closed())

    def test_calibration_click_bypasses_human_bridge_and_restores_outer_guard(self):
        from agent_core.web_ui.component_calibration import click_automation_send
        bridge=BrowserInputBridge(self.page);bridge.install()
        adapter=create_web_ui_for_page(self.page)
        for previous in (False,True):
            adapter.set_automation_submit(previous)
            self.page.locator('#composer').fill('software calibration')
            click_automation_send(adapter,self.page.locator('#send'))
            self.assertIsNone(bridge.pop())
            self.assertEqual(self.page.evaluate('!!window.__webAgentAutomationSubmit'),previous)
        self.assertEqual(self.page.evaluate('window.nativeSends'),2)
        adapter.set_automation_submit(False)
        with patch.object(self.page.locator('#send').__class__,'click',side_effect=RuntimeError('click failed')):
            with self.assertRaises(RuntimeError):click_automation_send(adapter,self.page.locator('#send'))
        self.assertFalse(self.page.evaluate('!!window.__webAgentAutomationSubmit'))

    def test_missing_send_mapping_fails_with_actionable_error(self):
        p=dom_config.config_path(self.provider);p.unlink()
        dom_config.save_composer(self.provider,URL,'#composer',{})
        with self.assertRaisesRegex(ValueError,'requires_composer_and_send'):BrowserInputBridge(self.page).install()

class EntryURLTests(unittest.TestCase):
    def test_entry_url_normalizes_without_fabricating_conversation_id(self):
        url='https://copilot.cloud.microsoft/chat/conversation'
        self.assertEqual(normalize_conversation_url(url),url)
        self.assertEqual(conversation_id(url),'')
        for invalid in ('','file:///secret','javascript:alert(1)','https://user:password@example.com'):
            with self.assertRaises(ValueError):normalize_conversation_url(invalid)
    def test_entry_target_does_not_reuse_or_close_unidentified_tabs(self):
        from types import SimpleNamespace
        from WebAgent import browser_bridge as flow
        old=SimpleNamespace(url='https://copilot.cloud.microsoft/chat/conversation',is_closed=lambda:False)
        fresh=SimpleNamespace(url='about:blank',bring_to_front=lambda:None)
        context=SimpleNamespace(pages=[old],new_page=lambda:fresh)
        with patch.object(flow,'shared_browser_enabled',return_value=False),patch.object(flow,'_conversation_ready') as ready:
            self.assertIs(flow._open_target_conversation_unlocked(context,old.url),fresh)
            ready.assert_called_once_with(fresh,old.url)
    def test_entry_waits_for_canonical_identity_on_same_provider(self):
        from types import SimpleNamespace
        from WebAgent import browser_bridge as flow
        target='https://copilot.cloud.microsoft/chat/conversation'
        canonical=target+'/test-conversation'
        page=SimpleNamespace(url='about:blank',is_closed=lambda:False)
        page.evaluate=lambda script,url:setattr(page,'url',url)
        page.wait_for_timeout=lambda ms:setattr(page,'url',canonical)
        with patch.object(flow,'_visible_composer',return_value=object()):
            flow._conversation_ready(page,target,timeout_ms=1000)
        self.assertEqual(page.url,canonical)
    def test_entry_rejects_cross_provider_landing_without_initializing_session(self):
        from types import SimpleNamespace
        from WebAgent import browser_bridge as flow
        import itertools
        page=SimpleNamespace(url='about:blank',is_closed=lambda:False)
        page.evaluate=lambda script,url:setattr(page,'url','https://claude.ai/chat/other')
        page.wait_for_timeout=lambda ms:None
        ticks=itertools.count(0,.25)
        with patch.object(flow.time,'monotonic',side_effect=lambda:next(ticks)),patch.object(flow,'_visible_composer') as visible:
            with self.assertRaisesRegex(RuntimeError,'WEB_CONVERSATION_ID_PENDING'):
                flow._conversation_ready(page,'https://copilot.cloud.microsoft/chat/conversation',timeout_ms=1000)
            visible.assert_not_called()

if __name__=='__main__':unittest.main()
