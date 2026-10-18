"""Observed Gemini completed-content styling must not hold the idle gate open."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright

from .. import dom_config
from .. import dialogue_calibration as flow
from ..dialogue_state import wait_owned_response
from ..factory import create_web_ui_for_page
from .test_dialogue_calibration import HTML
from ..providers.gemini.bridge import BRIDGE_SCRIPT, BRIDGE_VERSION


class GeminiBusyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        original=dom_config.config_path
        config=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:original(provider,root=root or Path(self.temp.name)))
        config.start();self.addCleanup(config.stop)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        html=HTML.replace("document.createElement('div');content.className='markdown'",
                          "document.createElement('structured-content-container');content.className='markdown model-response-text has-thoughts processing-state-visible'")
        html=html.replace('answer.append(content);',
                          "const message=document.createElement('message-content');message.id='message-content-id-r_fixture'+n;message.append(content);answer.append(message);")
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
        self.page=self.context.new_page();self.page.goto('https://gemini.google.com/app/fixture')
        dom_config.save_composer('gemini',self.page.url,'#composer',{})
        for name,selector in [('send','#send'),('user_turn','article[data-message-author-role="user"]'),
                              ('user_content','.user-text'),('assistant_turn','article[data-message-author-role="assistant"]')]:
            dom_config.save_element('gemini',self.page.url,name,selector,{}, {dom_config.VALIDATION_KEYS[name]:True})

    def completed_reply(self):
        probe=flow.content_probe(self.page);scope=flow.begin_request(self.page,probe)
        adapter=create_web_ui_for_page(self.page)
        turn,text,_=wait_owned_response(adapter,scope,probe['end_marker'],allow_raw_preview=True,quiet_seconds=.25,timeout=5)
        return adapter,turn,text

    def test_completed_processing_state_visible_releases_owned_response_wait(self):
        adapter,turn,text=self.completed_reply()
        self.assertIn('_END',text)
        self.assertFalse(adapter.activity(turn).generating)
        self.assertFalse(adapter.media_state(turn)['media_pending'])
        self.assertEqual(self.page.evaluate('window.submits'),1)

    def test_explicit_busy_flags_progress_and_other_processing_classes_still_block(self):
        adapter,turn,_=self.completed_reply()
        body=self.page.locator('structured-content-container')
        for attribute,value in [('aria-busy','true'),('data-state','processing')]:
            with self.subTest(attribute=attribute):
                body.evaluate('(el,pair)=>el.setAttribute(...pair)',[attribute,value])
                self.assertTrue(adapter.media_state(turn)['media_pending'])
                body.evaluate('(el,name)=>el.removeAttribute(name)',attribute)
        body.evaluate('''el=>{const spinner=document.createElement('div');spinner.setAttribute('role','progressbar');spinner.textContent='Working';el.append(spinner);}''')
        self.assertTrue(adapter.media_state(turn)['media_pending'])
        body.locator('[role="progressbar"]').evaluate('el=>el.remove()')
        body.evaluate("el=>el.className='processing-active'")
        self.assertTrue(adapter.media_state(turn)['media_pending'])

    def test_stop_control_still_prevents_completion(self):
        adapter,turn,_=self.completed_reply()
        self.page.locator('#stop').evaluate('el=>el.style.display="inline-block"')
        self.assertTrue(adapter.activity(turn).generating)

    def test_click_without_accepted_user_turn_stops_before_response_wait(self):
        self.page.locator('#send').evaluate('el=>el.onclick=()=>window.submits++')
        probe=flow.content_probe(self.page)
        with self.assertRaisesRegex(TimeoutError,'test_message_not_accepted'):
            flow.begin_request(self.page,probe)
        self.assertEqual(self.page.evaluate('window.submits'),1)
        self.assertTrue(create_web_ui_for_page(self.page).validate_composer(probe['prompt']).matched)

    def test_enter_bridge_under_trusted_types_queues_once_without_site_submit(self):
        self.context.route('**/bridge-fixture',lambda route:route.fulfill(
            content_type='text/html',headers={'Content-Security-Policy':"require-trusted-types-for 'script'"},
            body='<rich-textarea><div class="ql-editor" contenteditable="true"></div></rich-textarea>'))
        self.page.goto('https://gemini.google.com/bridge-fixture')
        self.page.evaluate('''()=>{window.legacyCaptures=0;document.addEventListener('keydown',e=>{
            if(e.key==='Enter'&&!e.shiftKey&&!window.__webAgentAutomationSubmit)window.legacyCaptures++;
        },true)}''')
        self.assertTrue(self.page.evaluate(BRIDGE_SCRIPT))
        self.page.evaluate(BRIDGE_SCRIPT) # Installing twice must not capture twice.
        errors=[];self.page.on('pageerror',lambda error:errors.append(str(error)))
        editor=self.page.locator('.ql-editor');editor.click()
        self.page.keyboard.insert_text('列出工作目錄中的檔案')
        self.page.keyboard.press('Enter')
        self.assertEqual(self.page.evaluate('window.__webAgentDirectQueue.map(x=>x.text)'),['列出工作目錄中的檔案'])
        self.assertEqual(editor.inner_text(),'')
        self.assertEqual(self.page.evaluate('window.__webAgentDirectBridgeVersion'),BRIDGE_VERSION)
        self.assertEqual(errors,[])
        self.assertEqual(self.page.evaluate('window.legacyCaptures'),0)
        self.page.keyboard.insert_text('換行草稿')
        self.page.keyboard.press('Shift+Enter')
        self.assertEqual(self.page.evaluate('window.__webAgentDirectQueue.length'),1)
        self.page.evaluate('window.__webAgentAutomationSubmit=true')
        self.page.keyboard.press('Enter')
        self.assertEqual(self.page.evaluate('window.__webAgentDirectQueue.length'),1)


if __name__=='__main__':unittest.main()
