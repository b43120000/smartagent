"""M365 presentation instructions preserve strict protocol and request binding."""
import html
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from ..providers.m365.adapter import M365UIAdapter
from ..base_adapter import BaseWebUIAdapter
from ...protocol_v9 import parse_v9_tool_transport_detailed
from .test_m365_code_ui_filter import M365CodeUIFilterTests, BODY

class PromptTests(unittest.TestCase):
    def test_only_m365_appends_format_instruction_without_altering_request(self):
        raw='[AGENT_SESSION_ATTACH]\n{"session_id":"test","handshake_id":"h1"}\n[/AGENT_SESSION_ATTACH]'
        adapter=object.__new__(M365UIAdapter)
        result=adapter.prepare_transport_prompt(raw)
        self.assertTrue(result.startswith(raw+'\n\n'))
        self.assertIn('smartagent_tool alone on its own line',result)
        self.assertIn('turn_commit',result)
        self.assertIn('exact action_count',result)
        self.assertIn('actual line breaks',result)
        self.assertIn('\n[M365_PLAIN_TEXT_TRANSPORT]\n',result)
        self.assertEqual(BaseWebUIAdapter.prepare_transport_prompt(None,raw),raw)

    def test_recovery_submission_uses_same_formatted_prompt_for_capture_and_send(self):
        from agent_core.web_runtime import WebLLMScraper
        for service in ['m365','chatgpt']:
            with self.subTest(service=service):
                adapter=object.__new__(M365UIAdapter)
                fake=SimpleNamespace(service=service,_web_ui_adapter=lambda:adapter,
                    _log_stage=Mock(),_protocol_recovery_admission_state=lambda:(True,{}),
                    _write_prompt_to_composer=Mock(),_wait_for_send_ready=Mock(return_value='send'),
                    _capture_request_turn_state=Mock(return_value={'scope':'new'}),
                    _submit_verified_prompt=Mock(),_wait_for_user_sent=Mock())
                WebLLMScraper._send_protocol_recovery_probe(fake,{},prompt_override='repair request')
                sent=fake._write_prompt_to_composer.call_args.args[0]
                self.assertEqual('[M365_PLAIN_TEXT_TRANSPORT]' in sent,service=='m365')
                fake._capture_request_turn_state.assert_called_once_with(sent)
                fake._submit_verified_prompt.assert_called_once_with('send',prompt=sent,submit_snapshot={'scope':'new'})

class PlainTextDOMTests(M365CodeUIFilterTests):
    # Inherited code-widget tests cover the fallback when the model ignores the request.
    def test_plain_text_paragraphs_and_br_keep_protocol_lines(self):
        for markup in ['<p>smartagent_tool</p>'+''.join('<p>'+html.escape(x)+'</p>' for x in BODY.splitlines()),
                       html.escape('smartagent_tool\n'+BODY).replace('\n','<br>')]:
            self.load(markup)
            result=parse_v9_tool_transport_detailed(self.read())
            self.assertFalse(result.diagnostics)
            self.assertEqual(len(result.calls),3)

    def test_collapsed_label_and_json_still_rejected(self):
        self.load('<p>'+html.escape('smartagent_tool '+BODY.replace('\n',' '))+'</p>')
        self.assertTrue(parse_v9_tool_transport_detailed(self.read()).diagnostics)

if __name__=='__main__':unittest.main()
