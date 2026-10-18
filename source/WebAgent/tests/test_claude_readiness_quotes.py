"""Claude typography compatibility must preserve the readiness identity checks."""
import json
import unittest
import tempfile
from pathlib import Path
from agent_core.session_protocol import SessionProtocol


class ReadinessQuotesTests(unittest.TestCase):
    def setUp(self):
        self.protocol = SessionProtocol('web_agent_direct', 9, 'test body')
        self.payload = dict(protocol_name='web_agent_direct', protocol_version=9,
                            protocol_hash=self.protocol.identity.protocol_hash, session_id='SES-TEST')

    def reply(self, payload=None, marker='AGENT_PROTOCOL_READY'):
        body=json.dumps(self.payload if payload is None else payload,separators=(',',':'))
        # Observed reply mixes curly delimiters with an ASCII final quote.
        body=body.replace('"','”').replace('{”','{“').replace(',”',',“')
        body=body[:-2]+'"}'
        return '['+marker+']\n'+body+'\n[/'+marker+']'

    def test_opt_in_and_standard_json(self):
        self.assertFalse(self.protocol.parse_protocol_ready(self.reply(),session_id='SES-TEST'))
        self.assertTrue(self.protocol.parse_protocol_ready(self.reply(),session_id='SES-TEST',allow_smart_quotes=True))
        standard='[AGENT_PROTOCOL_READY]'+json.dumps(self.payload)+'[/AGENT_PROTOCOL_READY]'
        self.assertTrue(self.protocol.parse_protocol_ready(standard,session_id='SES-TEST'))

    def test_wrong_identity_and_arbitrary_prose_rejected(self):
        for key,value in [('protocol_name','other'),('protocol_version',8),('protocol_hash','wrong'),('session_id','SES-OLD'),('extra','prose')]:
            with self.subTest(key=key):
                self.assertFalse(self.protocol.parse_protocol_ready(self.reply({**self.payload,key:value}),session_id='SES-TEST',allow_smart_quotes=True))
        self.assertFalse(self.protocol.parse_protocol_ready('[AGENT_PROTOCOL_READY]',session_id='SES-TEST',allow_smart_quotes=True))

    def test_attach_nonce_still_required(self):
        reply=self.reply({**self.payload,'handshake_id':'HND-NEW'},'AGENT_SESSION_READY')
        self.assertTrue(self.protocol.parse_session_ready(reply,session_id='SES-TEST',handshake_id='HND-NEW',allow_smart_quotes=True))
        self.assertFalse(self.protocol.parse_session_ready(reply,session_id='SES-TEST',handshake_id='HND-OLD',allow_smart_quotes=True))

    def test_duplicate_keys_fail_closed(self):
        reply=self.reply().replace('{','{“session_id”:”SES-OLD”,',1)
        self.assertFalse(self.protocol.parse_protocol_ready(reply,session_id='SES-TEST',allow_smart_quotes=True))

    def test_webagent_enables_quote_compatibility_only_for_claude(self):
        from WebAgent.session import ensure_webagent_session
        def send(prompt, **kwargs):
            identity=json.loads(prompt.splitlines()[1])
            marker='AGENT_SESSION_READY' if prompt.startswith('[AGENT_SESSION_ATTACH]') else 'AGENT_PROTOCOL_READY'
            return self.reply(identity,marker)
        for url,accepted in [('https://claude.ai/chat/fixture',True),('https://chatgpt.com/c/fixture',False),('https://claude.ai.evil.test/chat/fixture',False)]:
            with self.subTest(url=url),tempfile.TemporaryDirectory() as folder:
                options=dict(workspace=folder,gpt_url=url,state_dir=Path(folder)/'state',send_prompt=send)
                if accepted:
                    self.assertEqual(ensure_webagent_session(**options)['state']['readiness_mode'],'FULL_IDENTITY')
                    self.assertEqual(ensure_webagent_session(**options)['mode'],'SESSION_ATTACH')
                else:
                    with self.assertRaisesRegex(RuntimeError,'matching WEBAGENT protocol readiness'):
                        ensure_webagent_session(**options)


if __name__=='__main__':unittest.main()
