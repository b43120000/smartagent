"""Photo-derived Copilot virtualized DIV code widgets, without pre/code tags."""
import html
from types import SimpleNamespace
from unittest.mock import patch
from .test_m365_code_ui_filter import M365CodeUIFilterTests, BODY, NOTICE
from ..message_text import read_answer_text
from ..providers.m365.adapter import M365UIAdapter
from ...protocol_v9 import parse_v9_tool_transport_detailed


class M365VirtualCodeUITests(M365CodeUIFilterTests):
    def widget(self, body=BODY, notice=NOTICE, language='Shell'):
        # Layout classes are deliberately arbitrary; selectors cannot depend on them.
        return ('<div class="scriptor-component-code-block scriptor-codeblock-virtualized">'
                '<div aria-live="assertive"></div><div aria-live="polite"></div>'
                '<div class="random-header"><div><span>'+html.escape(language)+
                '</span><button>Copy</button></div><div class="fui-MessageBar">'
                '<div data-testid="message-bar-body-info">'+html.escape(notice)+
                '</div><button>Close</button></div></div>'
                '<div class="random-body">'+html.escape(body)+'</div></div>')

    def test_virtual_structure_reports_reconstruction(self):
        self.load(self.widget());diagnostic=[]
        text=read_answer_text(self.page.locator('.markdown'),copilot_code_ui=True,diagnostics=diagnostic)
        self.assertFalse(parse_v9_tool_transport_detailed(text).diagnostics)
        self.assertEqual(diagnostic,[{'structure':'virtualized_div','body_candidates':1,
                                     'reconstructed':True,'reason':'reconstructed'}])
        self.assertEqual(self.page.locator('pre,code').count(),0)

    def test_selected_widget_root_and_reordered_children(self):
        self.load(self.widget())
        widget=self.page.locator('.scriptor-codeblock-virtualized')
        widget.evaluate('el=>el.prepend(el.lastElementChild)')
        text=read_answer_text(widget,copilot_code_ui=True)
        self.assertFalse(parse_v9_tool_transport_detailed(text).diagnostics)
        self.assertIn(BODY,text)

    def test_rendered_div_lines_and_br_preserve_json_sequence(self):
        for markup in [''.join('<div>'+html.escape(line)+'</div>' for line in BODY.splitlines()),
                       html.escape(BODY).replace('\n','<br>')]:
            self.load(self.widget())
            self.page.locator('.random-body').evaluate('(el,html)=>el.innerHTML=html',markup)
            self.assertFalse(parse_v9_tool_transport_detailed(self.read()).diagnostics)

    def test_extra_header_prose_is_not_silently_discarded(self):
        self.load(self.widget())
        self.page.locator('.random-header').evaluate('el=>el.append("Do not execute this example.")')
        diagnostic=[]
        text=read_answer_text(self.page.locator('.markdown'),copilot_code_ui=True,diagnostics=diagnostic)
        self.assertIn('Do not execute this example.',text)
        self.assertEqual(diagnostic[0]['reason'],'unexpected_header_text')
        self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)

    def test_two_body_candidates_are_rejected(self):
        self.load(self.widget())
        self.page.locator('.scriptor-codeblock-virtualized').evaluate(
            'el=>el.append(el.querySelector(".random-body").cloneNode(true))')
        diagnostic=[]
        text=read_answer_text(self.page.locator('.markdown'),copilot_code_ui=True,diagnostics=diagnostic)
        self.assertEqual(diagnostic[0]['body_candidates'],2)
        self.assertEqual(diagnostic[0]['reason'],'ambiguous_body')
        self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)

    def test_non_m365_does_not_reconstruct_virtual_code(self):
        self.load(self.widget())
        text=read_answer_text(self.page.locator('.markdown'))
        self.assertNotIn('```smartagent_tool',text)
        self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)

    def test_unknown_notice_or_non_json_body_rejected_with_reason(self):
        for body,notice,reason in [(BODY,'Unknown warning.','recognized_notice_missing'),
                                   ('Explanation before JSON.\n'+BODY,NOTICE,'json_body_not_found')]:
            self.load(self.widget(body,notice));diagnostic=[]
            text=read_answer_text(self.page.locator('.markdown'),copilot_code_ui=True,diagnostics=diagnostic)
            self.assertEqual(diagnostic[0]['reason'],reason)
            self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)

    def test_nested_widgets_do_not_promote_outer_prose(self):
        self.load(self.widget())
        inner=self.widget()
        self.page.locator('.random-body').evaluate('(el,html)=>el.innerHTML=html',inner)
        diagnostic=[]
        text=read_answer_text(self.page.locator('.markdown'),copilot_code_ui=True,diagnostics=diagnostic)
        self.assertEqual(diagnostic[0]['reason'],'nested_widget')
        self.assertTrue(parse_v9_tool_transport_detailed(text).diagnostics)

    def test_diagnostic_flows_to_runtime_and_resets_for_next_read(self):
        from agent_core.web_runtime import WebLLMScraper
        self.load(self.widget());adapter=object.__new__(M365UIAdapter)
        with patch.object(adapter,'_active_profile',return_value=SimpleNamespace(final_content_selector='.markdown')):
            text=adapter.extract_final_text(self.page.locator('article').element_handle())
            runtime=SimpleNamespace(service='m365',_web_ui_adapter=lambda:adapter)
            source=WebLLMScraper._protocol_response_source(runtime,{},None,text)
            self.assertEqual(source['dom_filter'][0]['reason'],'reconstructed')
            self.assertNotIn(BODY,str(source))
            self.load('<p>Ordinary reply</p>')
            adapter.extract_final_text(self.page.locator('article').element_handle())
            self.assertEqual(adapter._answer_dom_filter_diagnostics,[])
