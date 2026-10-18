"""Controlled-browser integration for stages6–10; never sends to model websites."""
import copy
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from .. import dom_config
from .. import dialogue_calibration as flow
from ..dialogue_state import DEFAULT_BLOCKING_RULES,detect_blocking,validate_blocking_rules,wait_owned_response
from ..factory import create_web_ui_for_page
from ...web_runtime import WebLLMScraper,WebScraperStageError
from ..provider_registry import provider_from_url

HTML='''<!doctype html><meta charset="utf-8"><textarea id="composer"></textarea>
<button id="send" aria-label="Send">Send</button><button id="stop" aria-label="Stop" data-testid="stop-button" style="display:none">Stop</button><main></main>
<script>
window.submits=0;window.stopClicks=0;let timer;
const finish=()=>{document.querySelector('#stop').style.display='none';document.querySelector('#send').disabled=false;document.querySelectorAll('[aria-busy]').forEach(e=>e.setAttribute('aria-busy','false'));};
document.querySelector('#stop').onclick=()=>{window.stopClicks++;clearTimeout(timer);finish();};
document.querySelector('#send').onclick=()=>{
 const n=++window.submits,box=document.querySelector('textarea'),prompt=box.value, marker=prompt.match(/DOMPROBE_[A-F0-9]+/)[0];box.value='';
 const user=document.createElement('article');user.dataset.messageAuthorRole='user';user.dataset.messageId='user-'+n;
 const body=document.createElement('div');body.className='user-text';body.textContent=prompt;user.append(body);document.querySelector('main').append(user);
 const answer=document.createElement('article');answer.dataset.messageAuthorRole='assistant';answer.dataset.messageId='assistant-'+n;answer.setAttribute('aria-busy','true');
 const content=document.createElement('div');content.className='markdown';
 for(const text of [marker,'第一段：文字校準。','第二段：保留換行。']){const p=document.createElement('p');p.textContent=text;content.append(p);}
 const wrapper=document.createElement('div'),head=document.createElement('div'),copy=document.createElement('button');copy.textContent='Copy code';head.append(copy);
 const pre=document.createElement('pre'),code=document.createElement('code');code.textContent='print("'+marker+'")';pre.append(code);wrapper.append(head,pre);content.append(wrapper);
 const end=document.createElement('p');end.textContent=marker+'_END';content.append(end);answer.append(content);document.querySelector('main').append(answer);
 document.querySelector('#stop').style.display='inline-block';document.querySelector('#send').disabled=true;
 timer=setTimeout(finish,prompt.includes('600 numbered')?10000:350);
};
</script>'''


class DialogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        original=dom_config.config_path
        config=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:original(provider,root=root or self.root))
        config.start();self.addCleanup(config.stop)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.context.route('**/*',lambda r:r.fulfill(content_type='text/html',body=HTML))
        self.page=self.context.new_page();self.page.goto('https://chatgpt.com/c/fixture')
        dom_config.save_composer('chatgpt',self.page.url,'#composer',{})
        for name,selector in [('send','#send'),('user_turn','article[data-message-author-role="user"]'),('user_content','.user-text'),('assistant_turn','article[data-message-author-role="assistant"]')]:
            dom_config.save_element('chatgpt',self.page.url,name,selector,{}, {dom_config.VALIDATION_KEYS[name]:True})
        waiter=patch.object(flow,'wait_owned_response',side_effect=lambda *args,**kw:wait_owned_response(*args,**{**kw,'quiet_seconds':.25,'timeout':5}))
        waiter.start();self.addCleanup(waiter.stop)

    def save_content_stop(self):
        for name,selector in [('assistant_content','.markdown'),('stop','#stop')]:
            dom_config.save_element('chatgpt',self.page.url,name,selector,{}, {dom_config.VALIDATION_KEYS[name]:True})

    def test_stages6_through10_complete_loop_and_runtime_overlay(self):
        with patch.object(flow,'select_component',return_value=dict(selector='.markdown',tag='div',kind='assistant_content')):
            result=flow.calibrate_content(self.page,self.root)
        self.assertTrue(result['code_and_paragraphs_verified'])
        with patch.object(flow,'select_component',return_value=dict(selector='#stop',tag='button',kind='stop')):
            result=flow.calibrate_stop(self.page,self.root)
        self.assertEqual(self.page.evaluate('window.stopClicks'),1)
        self.assertTrue(result['send_returned'])
        self.assertEqual(create_web_ui_for_page(self.page).profile.stop_selectors,('#stop',))
        self.assertTrue(flow.verify_lifecycle(self.page,self.root)['active_to_idle_verified'])
        self.assertFalse(flow.verify_blocking(self.page,self.root,'start')['real_blocker_observed'])
        before=self.page.evaluate('window.submits')
        result=flow.verify_conversation(self.page,self.root)
        self.assertEqual(self.page.evaluate('window.submits')-before,2)
        self.assertEqual(len({r['response_id'] for r in result['rounds']}),2)
        self.assertTrue(result['full_conversation'])
        self.assertFalse(result['automatic_resubmit'])

    def test_rule_positive_negative_and_quoted_error_exclusion(self):
        flow.verify_blocking(self.page,self.root,'start')
        self.assertEqual(detect_blocking(self.page,DEFAULT_BLOCKING_RULES),{})
        self.page.locator('main').evaluate('''el=>{const article=document.createElement('div');article.dataset.messageAuthorRole='assistant';const quote=document.createElement('div');quote.setAttribute('role','alert');quote.textContent='too many requests';article.append(quote);el.append(article);}''')
        self.assertEqual(detect_blocking(self.page,DEFAULT_BLOCKING_RULES),{})
        for rule in DEFAULT_BLOCKING_RULES:
            with self.subTest(kind=rule['kind']):
                self.page.evaluate('''text=>{document.querySelector('#error')?.remove();const el=document.createElement('div');el.id='error';el.setAttribute('role','dialog');el.textContent=text;document.body.append(el);}''',rule['tokens'][0])
                detected=detect_blocking(self.page,DEFAULT_BLOCKING_RULES)
                self.assertEqual(detected['kind'],rule['kind'])
                self.assertFalse(detected['auto_resubmit'])
                runtime=create_web_ui_for_page(self.page).blocking_transport_error()
                self.assertEqual(runtime['kind'],rule['kind'])
                self.assertFalse(runtime['auto_resubmit'])
                scraper=WebLLMScraper.__new__(WebLLMScraper)
                scraper._web_ui_adapter=lambda:create_web_ui_for_page(self.page)
                scraper._set_request_state=lambda *args:None
                scraper._log_stage=lambda *args:None
                with self.assertRaises(WebScraperStageError) as stopped:
                    scraper._check_provider_transport_error()
                self.assertFalse(stopped.exception.safe_to_retry)
                self.assertEqual(self.page.evaluate('window.submits'),0)
        self.page.locator('#error').evaluate('el=>el.style.display="none"')
        self.assertEqual(detect_blocking(self.page,DEFAULT_BLOCKING_RULES),{})

    def test_invalid_rules_and_capability_dependency_rejected(self):
        for rules in [[],[{'kind':'unknown','selectors':['body'],'tokens':['x']}],
                      [{'kind':'provider_error','selectors':['javascript:alert(1)'],'tokens':['x']}]]:
            with self.subTest(rules=rules),self.assertRaises(ValueError):validate_blocking_rules(rules)
        with self.assertRaises(ValueError):
            dom_config.save_capability('chatgpt','generation_state',dict(strategy='stop_busy_and_stable_text',quiet_seconds=5,validation={'lifecycle_verified':True}))

    def test_first_body_preview_does_not_depend_on_existing_body_selector(self):
        probe=flow.content_probe(self.page);scope=flow.begin_request(self.page,probe)
        adapter=create_web_ui_for_page(self.page)
        with patch.object(adapter,'extract_final_text',side_effect=AssertionError('body_not_calibrated')):
            turn,text,_=wait_owned_response(adapter,scope,probe['end_marker'],allow_raw_preview=True,
                                          quiet_seconds=.25,timeout=5)
        self.assertIn(probe['end_marker'],text)
        self.assertIsNotNone(turn)
        self.assertNotIn('assistant_content',dom_config.load_dom_config('chatgpt')['elements'])

    def test_stages6_to9_immediate_guidance_and_three_statuses(self):
        for stage in (6,7,8,9):
            with self.subTest(stage=stage):
                self.page.reload();flow.panel(self.page,stage,wait=False)
                panel=self.page.locator('#smartagent-dialogue-panel')
                self.assertTrue(panel.is_visible())
                for label in ('選取不符','初步檢查通過，待功能驗證','功能驗證通過'):
                    self.assertIn(label,panel.locator('#state-legend').inner_text())
                flow.update_panel(self.page,'invalid','test selection error')
                self.assertIn('選取不符',panel.locator('#status').inner_text())
                flow.update_panel(self.page,'preflight','test awaiting functional verification')
                self.assertIn('初步檢查通過，待功能驗證',panel.locator('#status').inner_text())
                self.page.evaluate('window.__smartAgentDialoguePanel.finish("test done",true)')
                self.assertIn('功能驗證通過',panel.locator('#status').inner_text())
                self.assertEqual(self.page.evaluate('window.submits'),0)

    def test_stage6_guidance_precedes_composer_wait_and_keeps_failure_visible(self):
        @contextmanager
        def opened(*args,**kwargs):yield self.page
        self.page.locator('#composer').evaluate('el=>el.style.display="none"')
        self.page.evaluate('''()=>{
            const timer=setInterval(()=>{
                const panel=document.querySelector('#smartagent-dialogue-panel');
                if(panel&&!window.sawGuidanceBeforeSubmit){
                    window.sawGuidanceBeforeSubmit=window.submits===0;
                    document.querySelector('#composer').style.display='block';
                }
                if(window.__smartAgentDialoguePanel?.phase==='invalid'){
                    window.failureVisible=panel.getClientRects().length>0;
                    window.failureMessage=panel.shadowRoot.querySelector('#status').textContent;
                    panel.shadowRoot.querySelector('#cancel').click();clearInterval(timer);
                }
            },50);
        }''')
        def fail_after_visible(*args,**kwargs):
            self.assertTrue(self.page.locator('#smartagent-dialogue-panel').is_visible())
            self.assertTrue(self.page.evaluate('window.sawGuidanceBeforeSubmit'))
            self.assertEqual(self.page.evaluate('window.submits'),1)
            raise TimeoutError('fixture_response_not_ready')
        with patch.object(flow,'open_manual_page',side_effect=opened),patch('agent_core.paths.log_root',return_value=self.root/'logs'),patch.object(flow,'wait_owned_response',side_effect=fail_after_visible):
            with self.assertRaisesRegex(TimeoutError,'fixture_response_not_ready'):
                flow.run(self.page.url,6,no_wait=False)
        self.assertTrue(self.page.evaluate('window.failureVisible'))
        self.assertIn('選取不符',self.page.evaluate('window.failureMessage'))
        self.assertIn('fixture_response_not_ready',self.page.evaluate('window.failureMessage'))

    def test_stage6_picker_stays_visible_during_functional_verification(self):
        flow.panel(self.page,6,wait=False)
        with patch.object(flow,'select_component',side_effect=self.auto_select):
            self.assertTrue(flow.calibrate_content(self.page,self.root)['passed'])
        panel=self.page.locator('#smartagent-component-picker')
        self.assertTrue(panel.is_visible())
        self.assertFalse(self.page.locator('#smartagent-dialogue-panel').is_visible())
        self.assertIn('初步檢查通過，待功能驗證',panel.locator('#status').inner_text())
        self.page.evaluate('window.__smartAgentComponentPicker.finish("verified")')
        self.assertIn('功能驗證通過',panel.locator('#status').inner_text())
        self.page.evaluate('window.__smartAgentComponentPicker.fail("verification failure")')
        self.assertIn('選取不符',panel.locator('#status').inner_text())

    def test_auxiliary_load_timeout_keeps_usable_guidance_and_allows_stage9(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        @contextmanager
        def opened(*args,**kwargs):yield self.page
        self.page.evaluate('''()=>{
          const timer=setInterval(()=>{
            const ui=document.querySelector('#smartagent-dialogue-panel')?.shadowRoot;
            if(ui){window.initialPanelSeen=true;ui.querySelector('#start').click();clearInterval(timer);}
          },50);
        }''')
        with patch.object(flow,'open_manual_page',side_effect=opened),patch('agent_core.paths.log_root',return_value=self.root/'logs'),patch.object(self.page,'wait_for_load_state',side_effect=BrowserTimeout('fixture auxiliary frame still loading')):
            result=flow.run(self.page.url,9,no_wait=True)
        self.assertTrue(result['passed'])
        self.assertTrue(self.page.evaluate('window.initialPanelSeen'))
        self.assertIn('功能驗證通過',self.page.locator('#smartagent-dialogue-panel').locator('#status').inner_text())
        self.assertEqual(self.page.evaluate('window.submits'),0)

    def test_guidance_reinstalls_after_navigation_without_sending(self):
        flow.panel(self.page,6,wait=False)
        self.page.reload(wait_until='domcontentloaded')
        panel=self.page.locator('#smartagent-dialogue-panel')
        self.assertTrue(panel.is_visible())
        self.assertEqual(self.page.evaluate('window.__smartAgentDialoguePanel.stage'),6)
        self.assertIn('初步檢查通過，待功能驗證',panel.locator('#state-legend').inner_text())
        self.assertEqual(self.page.evaluate('window.submits'),0)

    def test_selector_change_invalidates_lifecycle_and_loop_proofs(self):
        self.save_content_stop()
        flow.verify_lifecycle(self.page,self.root);flow.verify_blocking(self.page,self.root,'start')
        flow.verify_conversation(self.page,self.root)
        dom_config.save_element('chatgpt',self.page.url,'assistant_content',':scope',{}, {'content_verified':True})
        caps=dom_config.load_dom_config('chatgpt')['capabilities']
        self.assertNotIn('generation_state',caps);self.assertNotIn('conversation_loop',caps)
        self.assertIn('blocking_state',caps)

    def auto_select(self,page,stage,probe):
        # Drive the actual picker and Python preflight bridge, not a mocked
        # selector result. Selection itself must never activate the website.
        page.evaluate('''stage=>{
          const timer=setInterval(()=>{
            const state=window.__smartAgentComponentPicker;
            const ui=document.querySelector('#smartagent-component-picker')?.shadowRoot;
            if(!state||!ui)return;
            if(!state.selected){
              const target=stage===6?[...document.querySelectorAll('.markdown')].at(-1):stage===7?document.querySelector('#stop'):document.querySelector('#error');
              if(!target)return;
              ui.querySelector('#pick').click();target.click();
            }
            if(state.phase==='preflight'){ui.querySelector('#save').click();clearInterval(timer);}
          },50);
          window.__fixturePickerTimer=timer;
        }''',stage)
        from ..component_calibration import select_component
        return select_component(page,stage,probe,timeout=8)

    def test_all_providers_real_picker_and_stages6_through10(self):
        urls=['https://chatgpt.com/c/fixture','https://gemini.google.com/app/fixture',
              'https://claude.ai/chat/fixture','https://m365.cloud.microsoft/chat/fixture',
              'https://manual-dialogue.example/chat/fixture']
        for url in urls:
            with self.subTest(url=url):
                provider=provider_from_url(url)
                html=HTML.replace('aria-label="Stop" data-testid="stop-button"','aria-label="End output"')
                html=html.replace("content.className='markdown'","content.className='markdown font-claude-message'")
                user_selector='article[data-message-author-role="user"]'
                assistant_selector='article[data-message-author-role="assistant"]'
                if provider.startswith('other_'):
                    html=html.replace("user.dataset.messageAuthorRole='user'","user.className='custom-user'")
                    html=html.replace("answer.dataset.messageAuthorRole='assistant'","answer.className='custom-answer'")
                    user_selector='.custom-user';assistant_selector='.custom-answer'
                self.context.unroute('**/*')
                self.context.route('**/*',lambda r:r.fulfill(content_type='text/html',body=html))
                self.page.goto(url)
                dom_config.save_composer(provider,url,'#composer',{})
                for name,selector in [('send','#send'),('user_turn',user_selector),('user_content','.user-text'),('assistant_turn',assistant_selector)]:
                    dom_config.save_element(provider,url,name,selector,{}, {dom_config.VALIDATION_KEYS[name]:True})
                with patch.object(flow,'select_component',side_effect=self.auto_select):
                    result=flow.calibrate_content(self.page,self.root)
                    self.assertTrue(result['code_and_paragraphs_verified'])
                    self.assertEqual(self.page.evaluate('window.submits'),2)
                    self.page.reload()
                    result=flow.calibrate_stop(self.page,self.root)
                self.assertTrue(result['send_returned'])
                self.assertEqual(self.page.evaluate('window.stopClicks'),1)
                self.assertEqual(create_web_ui_for_page(self.page).profile.stop_selectors,('#stop',))
                self.assertTrue(flow.verify_lifecycle(self.page,self.root)['active_to_idle_verified'])
                self.assertTrue(flow.verify_blocking(self.page,self.root,'start')['passed'])
                before=self.page.evaluate('window.submits')
                self.assertTrue(flow.verify_conversation(self.page,self.root)['passed'])
                self.assertEqual(self.page.evaluate('window.submits')-before,2)
                # Normal div banners and arbitrary custom message containers:
                # ignore a quoted error, but accept the actual outside banner.
                self.page.evaluate('''selector=>{
                  document.querySelector('#smartagent-component-picker')?.remove();delete window.__smartAgentComponentPicker;
                  const host=document.createElement('div');host.id='smartagent-dialogue-panel';document.body.append(host);
                  const quote=document.createElement('div');quote.className='fixture-error';quote.textContent='network error';document.querySelector(selector).append(quote);
                  const error=document.createElement('div');error.id='error';error.className='fixture-error';error.textContent='network error';document.body.append(error);
                }''',assistant_selector)
                with patch.object(flow,'select_component',side_effect=self.auto_select):
                    self.assertTrue(flow.verify_blocking(self.page,self.root,'mark')['real_blocker_observed'])
                self.assertEqual(create_web_ui_for_page(self.page).blocking_transport_error()['kind'],'disconnected')
                self.page.locator('#error').evaluate('el=>el.remove()')
                self.assertEqual(create_web_ui_for_page(self.page).blocking_transport_error(),{})
                quoted_rule=[{'kind':'disconnected','selectors':['.fixture-error'],'tokens':['network error']}]
                self.assertEqual(detect_blocking(self.page,quoted_rule),{})


if __name__=='__main__':unittest.main()
