# Regression tests for M365 hidden Lexical decoration at trusted-input capture.
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from .. import dom_config
from ..providers.m365.composer import READ_COMPOSER_TEXT_JS, read_composer_snapshot, validate_composer_prompt
from ..providers.m365.bridge import BRIDGE_VERSION
from WebAgent.browser_bridge import BrowserInputBridge
from WebAgent.protocol_loop import extract_authorized_paths
from WebAgent.runtime_log import WebAgentRuntimeLog
from ...security_context import SecurityContext
from ...tools import tool_list_directory
from ...tool_result_contract import decode_result

HTML = '''<!doctype html><meta charset="utf-8">
<div id="editor" contenteditable="true" role="textbox"></div><button id="send">Send</button>
<script>
window.nativeSubmits=0;
document.querySelector('#editor').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey)window.nativeSubmits++;
});
document.querySelector('#send').addEventListener('click',()=>window.nativeSubmits++);
</script>'''

class BrowserFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.context=self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page=self.context.new_page()

    def editor(self, text, *, decoration=True):
        self.page.locator('#editor').evaluate('''(el,data)=>{
            el.replaceChildren();
            const p=document.createElement('p');
            const span=document.createElement('span');span.dataset.lexicalText='true';
            span.textContent=data.text;p.append(span);
            if(data.decoration){const hidden=document.createElement('span');
              hidden.setAttribute('aria-hidden','true');hidden.dataset.lexicalText='true';
              hidden.textContent='\u200B\u200C';p.append(hidden);}
            el.append(p);
        }''', {'text':text,'decoration':decoration})
        return self.page.locator('#editor').element_handle()

class M365ComposerTextUT(BrowserFixture):
    def setUp(self):
        super().setUp()
        self.page.set_content(HTML)

    def test_hidden_decoration_removed_without_mutating_editor(self):
        expected=r'D:\workspace\sample'
        el=self.editor(expected)
        before=el.evaluate('el=>el.outerHTML')
        self.assertTrue(el.inner_text().endswith('\u200B\u200C'))
        self.assertEqual(el.evaluate(READ_COMPOSER_TEXT_JS),expected)
        self.assertEqual(el.evaluate('el=>el.outerHTML'),before)

    def test_visible_zero_width_characters_are_preserved(self):
        expected='user\u200Btext\u200C\n'+r'D:\workspace\sam'+'\u200B'+'ple\u200C'
        el=self.editor(expected)
        self.assertEqual(el.evaluate(READ_COMPOSER_TEXT_JS),expected)

    def test_native_value_preserved_exactly(self):
        self.page.set_content('<textarea id="editor"></textarea>')
        expected='first\n'+r'D:\workspace\sample'+'\u200B\u200C'
        self.page.locator('#editor').fill(expected)
        self.assertEqual(self.page.locator('#editor').evaluate(READ_COMPOSER_TEXT_JS),expected)

    def test_blocks_br_and_unicode_keep_line_boundaries(self):
        self.page.set_content('<div id="editor" contenteditable="true"><p>第一行<br>第二行</p>'
                              '<p>第三行<span aria-hidden="true">\u200B\u200C</span></p></div>')
        self.assertEqual(self.page.locator('#editor').evaluate(READ_COMPOSER_TEXT_JS),'第一行\n第二行\n第三行')

    def test_snapshot_and_bridge_share_semantic_text(self):
        expected=r'D:\workspace\sample'
        el=self.editor(expected)
        snapshot=read_composer_snapshot(el)
        self.assertEqual(snapshot.primary_text,expected+'\u200B\u200C')
        self.assertEqual(snapshot.semantic_text,expected)
        self.assertEqual(validate_composer_prompt(expected,snapshot).matched_view,'semantic')
        self.assertEqual(el.evaluate(READ_COMPOSER_TEXT_JS),snapshot.semantic_text)

class M365InputCaptureIT(BrowserFixture):
    def setUp(self):
        super().setUp()
        original=dom_config.config_path
        patcher=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:
            original(provider,root=root or self.root/'config'))
        patcher.start();self.addCleanup(patcher.stop)
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html; charset=utf-8',
            headers={'Content-Security-Policy':"require-trusted-types-for 'script'; trusted-types 'none'"},body=HTML))

    def bridge(self,host):
        self.page.goto('https://'+host+'/chat')
        dom_config.save_composer('m365',self.page.url,'#editor',{})
        dom_config.save_element('m365',self.page.url,'send','#send',{}, {'click_verified':True})
        return BrowserInputBridge(self.page)

    def test_trusted_enter_to_capture_log_authorized_path_and_real_directory(self):
        for host in ('m365.cloud.microsoft','copilot.cloud.microsoft'):
            with self.subTest(host=host):
                bridge=self.bridge(host);bridge.install();bridge.install()
                workspace=self.root/'sample';workspace.mkdir(exist_ok=True)
                for name in ('alpha.txt','beta.csv'):(workspace/name).write_text('fixture',encoding='utf-8')
                request='列出這個路徑有哪些檔案\n'+str(workspace)
                self.editor(request)
                self.page.locator('#editor').click();self.page.keyboard.press('Enter')
                item=bridge.pop()
                self.assertIsNotNone(item)
                captured=str(item['text']).strip()
                self.assertEqual(captured,request)
                self.assertIsNone(bridge.pop())
                self.assertEqual(self.page.evaluate('window.nativeSubmits'),0)
                log_path=self.root/(host+'.jsonl');log=WebAgentRuntimeLog(log_path)
                log.emit('request_captured',request=captured,captured_at=item['captured_at'])
                paths=extract_authorized_paths(captured)
                self.assertEqual(paths,[str(workspace)])
                result=decode_result(tool_list_directory(paths[0],security_context=SecurityContext.for_workspace(workspace),structured=True))
                self.assertEqual(result['status'],'SUCCEEDED')
                self.assertIn('alpha.txt',json.dumps(result))
                self.assertIn('beta.csv',json.dumps(result))
                log.emit('request_started',request=captured,authorized_paths=paths)
                log.emit('tool_completed',tool='list_directory',result=result)
                events=[json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(events[0]['request'],request)
                self.assertEqual(events[1]['authorized_paths'],[str(workspace)])
                self.assertNotIn('\u200B',events[0]['request'])
                self.assertNotIn('\u200C',events[0]['request'])

    def test_click_capture_preserves_legitimate_zero_width_text(self):
        bridge=self.bridge('copilot.cloud.microsoft');bridge.install()
        expected='保留\u200B文字\u200C'
        self.editor(expected)
        self.page.locator('#send').click()
        self.assertEqual(bridge.pop()['text'],expected)
        self.assertEqual(self.page.evaluate('window.nativeSubmits'),0)

    def test_untrusted_shift_enter_and_automation_do_not_capture(self):
        bridge=self.bridge('m365.cloud.microsoft');bridge.install()
        self.editor('draft')
        self.page.locator('#editor').dispatch_event('keydown',{'key':'Enter'})
        self.assertIsNone(bridge.pop())
        self.page.locator('#editor').click();self.page.keyboard.press('Shift+Enter')
        self.assertIsNone(bridge.pop())
        self.page.evaluate('window.__webAgentAutomationSubmit=true')
        self.page.keyboard.press('Enter')
        self.assertIsNone(bridge.pop())

    def test_legacy_bridge_requires_reload_and_reset_installs_new_version_once(self):
        bridge=self.bridge('m365.cloud.microsoft')
        self.page.evaluate('''()=>{window.__m365BridgeInstalled=true;
            window.__webAgentDirectBridgeInstalled=true;window.__webAgentDirectBridgeVersion=1;}''')
        self.assertFalse(bridge.installed())
        with self.assertRaisesRegex(Exception,'M365_INPUT_BRIDGE_UPGRADE_REQUIRES_RELOAD'):
            bridge.install()
        bridge.install(reset_legacy=True);bridge.install()
        self.assertTrue(bridge.installed())
        self.assertEqual(self.page.evaluate('window.__webAgentDirectBridgeVersion'),BRIDGE_VERSION)
        self.editor('after upgrade')
        self.page.locator('#editor').click();self.page.keyboard.press('Enter')
        self.assertEqual(bridge.pop()['text'],'after upgrade')
        self.assertIsNone(bridge.pop())

    def test_legacy_upgrade_preserves_unsent_draft(self):
        bridge=self.bridge('m365.cloud.microsoft')
        expected='unsent draft'
        self.editor(expected)
        self.page.evaluate("""()=>{window.__m365BridgeInstalled=true;
            window.__webAgentDirectBridgeInstalled=true;window.__webAgentDirectBridgeVersion=1;}""")
        with self.assertRaisesRegex(RuntimeError,'M365_INPUT_BRIDGE_UPGRADE_PENDING_INPUT'):
            bridge.install(reset_legacy=True)
        self.assertEqual(self.page.locator('#editor').evaluate(READ_COMPOSER_TEXT_JS),expected)
        self.assertEqual(self.page.evaluate('window.__webAgentDirectBridgeVersion'),1)

    def test_legacy_upgrade_preserves_pending_queue(self):
        bridge=self.bridge('m365.cloud.microsoft')
        self.page.evaluate("""()=>{window.__m365BridgeInstalled=true;
            window.__webAgentDirectBridgeInstalled=true;window.__webAgentDirectBridgeVersion=1;
            window.__webAgentDirectQueue=[{text:'pending request',captured_at:1}];}""")
        with self.assertRaisesRegex(RuntimeError,'M365_INPUT_BRIDGE_UPGRADE_PENDING_INPUT'):
            bridge.install(reset_legacy=True)
        self.assertEqual(bridge.pop()['text'],'pending request')

    def test_hidden_only_editor_does_not_queue_an_empty_request(self):
        bridge=self.bridge('m365.cloud.microsoft');bridge.install()
        self.editor('')
        self.page.locator('#editor').click();self.page.keyboard.press('Enter')
        self.assertIsNone(bridge.pop())

if __name__=='__main__':unittest.main()
