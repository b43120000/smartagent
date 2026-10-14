"""Fixed-count/recycled DOM ownership and fresh reconnect handshakes."""
import json
from agent_core.web_ui.providers.chatgpt.adapter import ChatGPTUIAdapter
from agent_core.session_protocol import SessionProtocol
from WebAgent.tests.validate_web_ui_contract import FakePage


def run():
    from agent_core.web_runtime import WebLLMScraper, WebScraperStageError
    class QuotaAdapter:
        blocked = True
        def blocking_transport_error(self):
            return {'reason':'PROVIDER_RATE_LIMITED','message':'quota'} if self.blocked else {}
    quota_adapter=QuotaAdapter()
    scraper=WebLLMScraper.__new__(WebLLMScraper)
    scraper._web_ui_adapter=lambda:quota_adapter
    scraper._log_stage=lambda *_:None
    scraper._pending_submit_context=None
    scraper._set_request_state=lambda state,*_:setattr(scraper,'_request_state',state)
    for stage in ('READY_IDLE','GENERATING'):
        scraper._request_state=stage
        try: scraper._check_provider_transport_error()
        except WebScraperStageError as exc: assert exc.stage=='provider_rate_limit' and not exc.safe_to_retry
        else: raise AssertionError('quota not blocked')
        assert scraper._request_state=='RATE_LIMITED'
        quota_adapter.blocked=False
        scraper._check_provider_transport_error()
        scraper._reconcile_pending_submit_before_request()
        quota_adapter.blocked=True
    scraper._pending_submit_context={'real_pending_submit':True}
    try: scraper._check_provider_transport_error()
    except WebScraperStageError: pass
    assert scraper._request_state=='RECOVERY_REQUIRED' and scraper._pending_submit_context
    page = FakePage()
    page.users = [page.user('previous request', 'fallback-turn-5:0:user', 1)]
    page.assistants = [page.assistant('previous answer', 'fallback-turn-5:1:assistant', 2)]
    adapter = ChatGPTUIAdapter(page)
    prompt = adapter.prepare_transport_prompt('same logical request')
    assert prompt != adapter.prepare_transport_prompt('same logical request')
    scope = adapter.capture_request(prompt)
    # Same key/count, changed body, visible unique transport token.
    page.users = [page.user(prompt, 'fallback-turn-5:0:user', 1)]
    assert adapter.confirm_user_turn(scope)
    assert adapter.latest_owned_assistant(scope) is None
    page.users = [page.user(prompt, 'fallback-turn-6:0:user', 1)]
    page.assistants = [page.assistant('previous answer', 'fallback-turn-6:1:assistant', 2)]
    assert adapter.confirm_user_turn(scope)
    assert adapter.latest_owned_assistant(scope) is None
    page.users = [page.user(prompt, 'fallback-turn-5:0:user', 1)]
    page.assistants = [page.assistant('fresh answer', 'fallback-turn-5:1:assistant', 2)]
    assert adapter.latest_owned_assistant(scope).raw_text == 'fresh answer'
    # A reused key must never anchor a different/old submit.
    page.users = [page.user('previous request', 'fallback-turn-5:0:user', 1)]
    assert adapter.rebind_user_turn(scope) is None
    assert adapter.latest_owned_assistant(scope) is None
    bound, detail = adapter.reconcile_user_turn(scope)
    assert bound is None and detail['reason'] == 'submit_nonce_not_uniquely_visible'
    # Two matches are ambiguous; native historical IDs cannot pass content fallback.
    page.users = [page.user(prompt, 'fallback-turn-5:0:user', 1), page.user(prompt, 'fallback-turn-6:0:user', 3)]
    assert adapter.confirm_user_turn(scope) is None
    page.users = [page.users[0]]
    adapter.confirm_user_turn(scope)
    page.assistants[0].attrs['data-message-id'] = 'native-old'
    native_scope = adapter.capture_request(prompt)
    adapter.confirm_user_turn(native_scope)
    page.assistants[0]._text = 'changed historical native answer'
    assert adapter.latest_owned_assistant(native_scope) is None
    proto = SessionProtocol('test', 9, 'body')
    prompt = proto.session_attach_prompt(session_id='S', handshake_id='H-NEW')
    assert 'H-NEW' in prompt
    payload = {'protocol_name':'test','protocol_version':9,
        'protocol_hash':proto.identity.protocol_hash,'session_id':'S','handshake_id':'H-NEW'}
    response = '[AGENT_SESSION_READY]\n'+json.dumps(payload)+'\n[/AGENT_SESSION_READY]'
    assert proto.parse_session_ready(response, session_id='S', handshake_id='H-NEW')
    assert not proto.parse_session_ready(response, session_id='S', handshake_id='H-OLD')
    print('SUBMIT_IDENTITY_PASS')


if __name__ == '__main__':
    run()
