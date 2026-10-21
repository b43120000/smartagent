"""Generation delivery is provisional; fresh response ownership stays mandatory."""
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from agent_core.web_runtime import WebLLMScraper, WebScraperStageError
from agent_core.web_ui.contracts import ComposerSnapshot

URL='https://fixture.example/chat/one'

class GenerationDeliveryTests(unittest.TestCase):
    def make_runtime(self, service='claude'):
        rt=WebLLMScraper.__new__(WebLLMScraper);rt.service=service
        rt._page=SimpleNamespace(url=URL);rt.events=[];rt.states=[];rt.deliveries=[]
        rt._log_stage=lambda stage,detail='':rt.events.append((stage,detail))
        rt._set_request_state=lambda state,detail='':rt.states.append((state,detail))
        rt._record_composer_delivery=lambda:rt.deliveries.append(True)
        rt._run_control_hook=lambda:None;rt._check_provider_transport_error=lambda:None
        rt._log_composer_state=lambda stage:{}
        rt._turn_elements=lambda role:[]
        rt._active_generation_emergency_sec=.2;rt._ui_idle_grace_sec=.01
        rt.editor=ComposerSnapshot('','')
        rt.adapter=SimpleNamespace(composer_snapshot=lambda:rt.editor,
            reconcile_user_turn=lambda scope:(None,{'reason':'fixture_no_user'}),recover_turn_mounts=lambda:False,
            set_automation_submit=lambda active:None)
        rt._web_ui_adapter=lambda:rt.adapter
        snapshot={'_web_ui_scope':SimpleNamespace(user_turn=None), '_submit_click_completed':True,
            '_generation_active_before_submit':False,'_submit_page_url':URL,'user_count':0}
        return rt,snapshot

    def test_all_providers_latch_early_activity_without_fabricating_user(self):
        for service in ('chatgpt','claude','gemini','m365','other_fixture'):
            with self.subTest(service=service):
                rt,snapshot=self.make_runtime(service)
                self.assertFalse(rt._confirm_generation_delivery(snapshot,{'generation_active':True},deadline_reached=False))
                self.assertTrue(rt._confirm_generation_delivery(snapshot,{'generation_active':False},deadline_reached=True))
                self.assertIsNone(snapshot['_web_ui_scope'].user_turn)
                self.assertTrue(snapshot['_submit_delivery_confirmed'])
                self.assertTrue(snapshot['_generation_delivery_pending_scope'])
                self.assertEqual(len(rt.deliveries),1)
                self.assertEqual(rt.states[-1][0],'SUBMIT_CONFIRMED_PENDING_SCOPE')

    def test_hidden_decoration_requires_empty_semantic_view(self):
        for editor, expected in ((ComposerSnapshot('\u200b\u200c',''),True),
                (ComposerSnapshot('\u200b','\u200b'),False),
                (ComposerSnapshot('draft\u200b',''),False)):
            rt,snapshot=self.make_runtime('m365');rt.editor=editor
            self.assertEqual(rt._confirm_generation_delivery(snapshot,
                {'generation_active':True},deadline_reached=True),expected)

    def test_missing_or_conflicting_proof_never_provisionally_confirms(self):
        for name,value in [('_submit_click_completed',False),('_submit_click_completed',None),
                ('_generation_active_before_submit',True),('_generation_active_before_submit',None),
                ('_submit_page_url','https://fixture.example/chat/another'),('_web_ui_scope',None)]:
            with self.subTest(name=name,value=value):
                rt,snapshot=self.make_runtime();snapshot[name]=value
                self.assertFalse(rt._confirm_generation_delivery(snapshot,{'generation_active':True},deadline_reached=True))
                self.assertNotIn('_submit_delivery_confirmed',snapshot)
        rt,snapshot=self.make_runtime()
        self.assertFalse(rt._confirm_generation_delivery(snapshot,{'generation_active':False},deadline_reached=True))
        for editor in (ComposerSnapshot('still a draft',''),ComposerSnapshot('','draft'),SimpleNamespace(candidates=()),SimpleNamespace()):
            rt,snapshot=self.make_runtime();rt.editor=editor
            self.assertFalse(rt._confirm_generation_delivery(snapshot,{'generation_active':True},deadline_reached=True))
            self.assertNotIn('_submit_delivery_confirmed',snapshot)
        rt,snapshot=self.make_runtime();snapshot['_web_ui_scope'].user_turn=object()
        self.assertFalse(rt._confirm_generation_delivery(snapshot,{'generation_active':True},deadline_reached=True))

    def test_wait_loop_handles_transient_activity_before_normal_deadline(self):
        rt,snapshot=self.make_runtime();clock=[0.0];calls=[0]
        def state():
            calls[0]+=1;return {'generation_active':calls[0]==1}
        rt._composer_debug_state=state;rt._is_generation_active=lambda:False
        with patch('agent_core.web_runtime.time.time',side_effect=lambda:clock[0]), patch('agent_core.web_runtime.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            rt._wait_for_user_sent(snapshot,timeout_sec=.1)
        self.assertGreaterEqual(clock[0],.1)
        self.assertTrue(snapshot['_submit_delivery_confirmed'])
        self.assertIsNone(snapshot['_web_ui_scope'].user_turn)

    def test_transient_without_completed_click_times_out_and_never_resends(self):
        rt,snapshot=self.make_runtime();snapshot['_submit_click_completed']=False;clock=[0.0];calls=[0]
        def state():calls[0]+=1;return {'generation_active':calls[0]==1}
        rt._composer_debug_state=state;rt._is_generation_active=lambda:False
        with patch('agent_core.web_runtime.time.time',side_effect=lambda:clock[0]), patch('agent_core.web_runtime.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            with self.assertRaisesRegex(WebScraperStageError,'WEB_SEND_TIMEOUT'):
                rt._wait_for_user_sent(snapshot,timeout_sec=.1)
        self.assertFalse(rt.deliveries);self.assertNotIn('_submit_delivery_confirmed',snapshot)

    def test_persistent_activity_without_receipt_keeps_waiting_not_confirming(self):
        rt,snapshot=self.make_runtime();snapshot['_submit_click_completed']=False;clock=[0.0]
        rt._composer_debug_state=lambda:{'generation_active':True};rt._is_generation_active=lambda:True
        with patch('agent_core.web_runtime.time.time',side_effect=lambda:clock[0]), patch('agent_core.web_runtime.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            with self.assertRaisesRegex(WebScraperStageError,'WEB_USER_SCOPE_ACTIVE_STALLED'):
                rt._wait_for_user_sent(snapshot,timeout_sec=0)
        self.assertFalse(rt.deliveries);self.assertNotIn('_submit_delivery_confirmed',snapshot)

    def test_click_receipt_is_false_until_actual_click_returns(self):
        for failed in (False,True):
            rt,snapshot=self.make_runtime();snapshot['_generation_after_submit_seen']=True; snapshot['_submit_delivery_confirmed']=True
            calls=[]
            def click(**kwargs):
                calls.append(True);self.assertFalse(snapshot['_submit_click_completed'])
                if failed:raise RuntimeError('fixture click failed')
            send=SimpleNamespace(click=click)
            if failed:
                with self.assertRaises(WebScraperStageError):rt._submit_verified_prompt(send,submit_snapshot=snapshot)
            else:rt._submit_verified_prompt(send,submit_snapshot=snapshot)
            self.assertEqual(snapshot['_submit_click_completed'],not failed)
            self.assertNotIn('_generation_after_submit_seen',snapshot)
            self.assertNotIn('_submit_delivery_confirmed',snapshot)
            self.assertEqual(len(calls),1)


class GenerationDeliveryBrowserTests(unittest.TestCase):
    def test_recycled_user_dom_after_generation_ack_never_returns_old_answer(self):
        import tempfile
        from pathlib import Path
        from playwright.sync_api import sync_playwright
        from agent_core.web_ui import dom_config
        from agent_core.web_ui.factory import create_web_ui_for_page
        from agent_core.web_ui.provider_registry import provider_from_url
        html='''<textarea id="composer"></textarea><button id="send">Send</button><button id="stop" hidden>Stop</button>
        <article class="user" data-message-id="u1"><span class="user-text">old request</span></article>
        <article class="assistant" data-message-id="a1"><span class="answer">OLD ANSWER MUST NOT BE RETURNED</span></article>
        <script>window.submits=0;document.querySelector('#send').onclick=()=>{window.submits++;document.querySelector('#composer').value='';document.querySelector('#stop').hidden=false;};</script>'''
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as pw:
            original=dom_config.config_path
            with patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:original(provider,root=Path(folder))):
                browser=pw.chromium.launch(channel='chrome',headless=True)
                try:
                    context=browser.new_context();context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
                    page=context.new_page();page.goto(URL);provider=provider_from_url(URL)
                    dom_config.save_composer(provider,URL,'#composer',{})
                    for field,selector in [('send','#send'),('user_turn','.user'),('user_content','.user-text'),('assistant_turn','.assistant'),('assistant_content','.answer'),('stop','#stop')]:
                        dom_config.save_element(provider,URL,field,selector,{}, {dom_config.VALIDATION_KEYS[field]:True})
                    from agent_core.web_ui.providers.m365.composer import read_composer_snapshot
                    page.evaluate("() => { const el=document.createElement('div'); el.id='lexical-fixture'; el.innerHTML='<p><span aria-hidden=\"true\">&#8203;&#8204;</span></p>'; document.body.append(el); }")
                    lexical=read_composer_snapshot(page.locator('#lexical-fixture'))
                    self.assertIn('\u200b',lexical.primary_text)
                    self.assertFalse(lexical.semantic_text.strip())
                    decoration_rt,decoration_snapshot=GenerationDeliveryTests.make_runtime(self,'m365')
                    decoration_rt.editor=lexical
                    self.assertTrue(decoration_rt._confirm_generation_delivery(decoration_snapshot,
                        {'generation_active':True},deadline_reached=True))
                    page.locator('#lexical-fixture').evaluate('(el) => el.remove()')
                    adapter=create_web_ui_for_page(page)
                    rt,_=GenerationDeliveryTests.make_runtime(self,provider)
                    rt._page=page;rt._web_ui_adapter=lambda:adapter
                    rt._turn_elements=lambda role:[turn.element for turn in adapter.turns(role)]
                    rt._response_elements=lambda:page.query_selector_all('.answer')
                    rt._element_fingerprint=lambda element:element.inner_text()
                    rt._is_generation_active=adapter.generation_active
                    rt._composer_debug_state=lambda:{'generation_active':adapter.generation_active()}
                    rt._check_cancel_requested=lambda stage:None
                    rt._maybe_recover_disconnected_generation=lambda:False
                    rt._generation_stall_sec=2;rt._active_generation_warn_sec=2;rt._active_generation_emergency_sec=3
                    page.locator('#composer').fill('new request')
                    snapshot=rt._capture_request_turn_state('new request')
                    self.assertFalse(snapshot['_generation_active_before_submit'])
                    rt._submit_verified_prompt(page.locator('#send'),submit_snapshot=snapshot)
                    rt._wait_for_user_sent(snapshot,timeout_sec=.03)
                    scope=snapshot['_web_ui_scope']
                    self.assertTrue(snapshot['_submit_delivery_confirmed']);self.assertIsNone(scope.user_turn)
                    self.assertIsNone(adapter.latest_owned_assistant(scope))
                    page.evaluate('''() => {const answer=document.createElement('article');answer.className='assistant';answer.dataset.messageId='a2';answer.innerHTML='<span class="answer">NEW OWNED ANSWER</span>';document.body.append(answer);} ''')
                    self.assertIsNone(adapter.latest_owned_assistant(scope))
                    page.evaluate("() => setTimeout(()=>document.querySelector('.user-text').textContent='new request',100)")
                    owned=rt._wait_for_new_assistant_turn(snapshot,allow_fresh_ready_image=False)
                    self.assertEqual(owned.raw_text,'NEW OWNED ANSWER')
                    self.assertIsNotNone(scope.user_turn)
                    self.assertEqual(page.locator('.user').count(),1)
                    self.assertEqual(page.evaluate('window.submits'),1)
                    self.assertTrue(any(stage=='user_scope_restored_after_generation_fallback' for stage,detail in rt.events))
                finally:browser.close()
