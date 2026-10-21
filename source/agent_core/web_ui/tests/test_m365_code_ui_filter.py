"""M365 code widget UI is not tool payload; strict parsing stays enabled."""
import html
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ..message_text import read_answer_text
from ..factory import create_web_ui_for_page
from ..providers.m365.adapter import M365UIAdapter
from ...protocol_v9 import parse_v9_tool_transport_detailed

NOTICE='smartagent_tool 並沒有完全支援。語法標示是基於 Shell。'
BODY=Path(__file__).with_name('fixtures').joinpath('m365_code_ui_reply.txt').read_text(encoding='utf-8')

class M365CodeUIFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.browser.new_page()
    def tearDown(self):self.page.close()
    def widget(self,body=BODY,notice=NOTICE,language='Shell'):
        return ('<section class="code-widget"><div class="toolbar"><span>'+html.escape(language)+
                '</span><button>Copy</button></div><div class="notice" role="alert"><span>'+html.escape(notice)+
                '</span><button aria-label="Close">×</button></div><div class="code-scroll"><pre><code>'+
                html.escape(body)+'</code></pre></div></section>')
    def load(self,value):self.page.set_content('<article><div class="markdown">'+value+'</div></article>')
    def read(self):return read_answer_text(self.page.locator('.markdown'),copilot_code_ui=True)
    def test_photo_payload_passes_and_real_dom_is_unchanged(self):
        self.load(self.widget());before=self.page.content()
        result=parse_v9_tool_transport_detailed(self.read())
        self.assertEqual(result.diagnostics,[])
        self.assertEqual([x['tool'] for x in result.calls],['report_progress','final_response','turn_commit'])
        self.assertEqual(self.page.content(),before)
        self.assertNotIn(NOTICE,self.read())
    def test_adapter_uses_filter_on_both_m365_hosts(self):
        self.page.route('**/*',lambda route:route.fulfill(body='<html></html>'))
        for host in ['m365.cloud.microsoft','copilot.cloud.microsoft']:
            self.page.goto('https://'+host+'/chat/conversation/fixture')
            self.load(self.widget())
            adapter=create_web_ui_for_page(self.page)
            self.assertIsInstance(adapter,M365UIAdapter)
            with patch.object(adapter,'_active_profile',return_value=SimpleNamespace(final_content_selector='.markdown')):
                result=parse_v9_tool_transport_detailed(adapter.extract_final_text(self.page.locator('article').element_handle()))
                self.assertEqual(result.diagnostics,[])
    def test_prose_outside_code_is_preserved_and_rejected(self):
        self.load('<p>Do not execute: quoted example follows.</p>'+self.widget())
        text=self.read();self.assertIn('Do not execute:',text)
        self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)
    def test_notice_and_language_inside_payload_are_not_removed(self):
        body=json.dumps({'tool':'final_response','action_id':'a1','content':NOTICE+' Shell'},ensure_ascii=False)+'\n'+json.dumps({'tool':'turn_commit','action_count':1})
        # Use a real newline between complete objects, not a JSON escape.
        body=body.replace('}\\n{','}\n{')
        self.load(self.widget(body))
        text=self.read();self.assertIn(NOTICE+' Shell',text)
    def test_missing_commit_and_wrong_count_still_rejected(self):
        for body in [BODY.rsplit('\n',1)[0],BODY.replace('"action_count":2','"action_count":8')]:
            self.load(self.widget(body));self.assertTrue(parse_v9_tool_transport_detailed(self.read()).diagnostics)
    def test_unknown_notice_does_not_become_protocol(self):
        self.load(self.widget(notice='Unknown website warning.'))
        text=self.read()
        self.assertNotIn('```smartagent_tool',text)
        self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)
    def test_ordinary_markdown_prose_is_unchanged(self):
        self.load('<p>Shell</p><p>'+html.escape(NOTICE)+'</p><p>Normal reply.</p>')
        self.assertEqual(self.read(),read_answer_text(self.page.locator('.markdown')))
    def test_two_code_widgets_keep_order_and_commit(self):
        a,b,c=BODY.split('\n')
        self.load(self.widget(a)+self.widget(b+'\n'+c))
        result=parse_v9_tool_transport_detailed(self.read())
        self.assertFalse(result.diagnostics)
        self.assertEqual([x['tool'] for x in result.calls],['report_progress','final_response','turn_commit'])

if __name__=='__main__':unittest.main()
