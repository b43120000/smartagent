"""A fresh M365 submit nonce binds transformed text, never an old/ambiguous turn."""
import json
from .test_m365_assistant import M365AssistantTests
from ..component_calibration import candidate_adapter
from ..contracts import ui_text_matches
from ..user_binding_diagnostic import diagnose_user_binding


class M365SubmitIdentityTests(M365AssistantTests):
    def make_adapter(self):
        self.page.locator('main').evaluate('el=>el.replaceChildren()')
        self.page.locator('main').evaluate('''el=>{
            const user=document.createElement('div');user.className='fai-UserMessage__message';
            user.id='recycled-user';user.textContent='previous request';el.append(user);
        }''')
        self.article(['native-old'])
        return candidate_adapter(self.page,'assistant_turn','div.fai-CopilotMessage')

    def test_fixed_count_transformed_body_binds_but_old_answer_stays_rejected(self):
        for host in ['m365.cloud.microsoft','copilot.cloud.microsoft']:
            with self.subTest(host=host):
                # The same provider adapter is used on both public aliases.
                self.page.goto('https://'+host+'/chat')
                adapter=self.make_adapter()
                raw='[WEBAGENT_ACTIVE_REQUEST]\n{"request_id":"WA-TEST-123"}\noriginal body '+('long '*2000)
                prompt=adapter.prepare_transport_prompt(raw)
                scope=adapter.capture_request(prompt)
                self.page.locator('.fai-UserMessage__message').evaluate(
                    '(el,text)=>el.textContent=text',prompt.replace('original body','rendered body'))
                self.assertFalse(ui_text_matches(adapter.turns('user')[-1].raw_text,scope.prompt_text))
                self.assertEqual(len(adapter.turns('user')),scope.baseline_user_count)
                self.assertIsNotNone(adapter.confirm_user_turn(scope))
                self.assertIsNone(adapter.latest_owned_assistant(scope))
                self.page.locator('[data-testid="markdown-reply"]').evaluate('''el=>{
                    el.dataset.messageId='native-new-'+Math.random();el.textContent='fresh owned reply';
                }''')
                self.assertIsNotNone(adapter.latest_owned_assistant(scope))

    def test_missing_or_duplicate_current_nonce_never_binds(self):
        adapter=self.make_adapter();prompt=adapter.prepare_transport_prompt('new request')
        scope=adapter.capture_request(prompt)
        self.assertIsNone(adapter.confirm_user_turn(scope))
        self.page.locator('.fai-UserMessage__message').evaluate('(el,text)=>el.textContent=text',prompt)
        self.page.locator('main').evaluate('''el=>{
            const duplicate=el.firstElementChild.cloneNode(true);duplicate.id='duplicate-user';el.append(duplicate);
        }''')
        self.assertIsNone(adapter.confirm_user_turn(scope))
        self.assertIsNone(adapter.rebind_user_turn(scope))
        bound,state=adapter.reconcile_user_turn(scope)
        self.assertIsNone(bound);self.assertEqual(state['reason'],'submit_nonce_not_uniquely_visible')
        self.assertIsNone(adapter.latest_owned_assistant(scope))

    def test_old_quoted_nonce_cannot_bind_when_new_envelope_is_missing(self):
        adapter=self.make_adapter();old=adapter.prepare_transport_prompt('historical request')
        self.page.locator('.fai-UserMessage__message').evaluate('(el,text)=>el.textContent=text',old)
        prompt=adapter.prepare_transport_prompt('Quoted data:\n'+old)
        scope=adapter.capture_request(prompt)
        self.assertEqual(len(scope.request_markers),1)
        self.assertNotEqual(scope.request_markers[0],adapter.capture_request(old).request_markers[0])
        self.assertIsNone(adapter.confirm_user_turn(scope))

    def test_rebinding_requires_current_nonce_after_dom_replacement(self):
        adapter=self.make_adapter();prompt=adapter.prepare_transport_prompt('new request')
        scope=adapter.capture_request(prompt)
        self.page.locator('.fai-UserMessage__message').evaluate('(el,text)=>el.textContent=text',prompt)
        self.assertIsNotNone(adapter.confirm_user_turn(scope))
        self.page.locator('.fai-UserMessage__message').evaluate('el=>el.replaceWith(el.cloneNode(true))')
        self.assertIsNotNone(adapter.rebind_user_turn(scope))
        self.page.locator('.fai-UserMessage__message').evaluate('el=>el.textContent="old request"')
        self.assertIsNone(adapter.rebind_user_turn(scope))
        self.assertIsNone(adapter.latest_owned_assistant(scope))

    def test_diagnostic_retains_request_identity_but_not_nonce_or_prompt_text(self):
        adapter=self.make_adapter()
        raw='[WEBAGENT_ACTIVE_REQUEST]\n{"request_id":"WA-TEST-456"}\nprivate content'
        prompt=adapter.prepare_transport_prompt(raw);scope=adapter.capture_request(prompt)
        self.page.locator('.fai-UserMessage__message').evaluate('(el,text)=>el.textContent=text',prompt)
        report=diagnose_user_binding(adapter,scope)
        self.assertEqual(report['request_id'],'WA-TEST-456')
        self.assertEqual(report['prompt_kind'],'WEBAGENT_ACTIVE_REQUEST')
        self.assertTrue(report['submit_marker_present'])
        self.assertNotIn('private content',json.dumps(report))
        self.assertNotIn(scope.request_markers[0],json.dumps(report))
