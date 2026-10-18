"""Focused browser regressions for stages 2–5; all data is fixture-owned."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from .. import dom_config
from ..component_calibration import PICKER, calibrate_stage, make_probe, prepare_probe, send_probe, user_root
from ..factory import create_web_ui_for_page
from .write_send_box import SAMPLE_TEXT, write_sample

USER = "article[data-content-search-unit-key$=':user']"
ASSISTANT = "article[data-content-search-unit-key$=':assistant']"
HTML = '''<!doctype html><meta charset="utf-8">
<textarea id="composer"></textarea><button aria-label="Send"><svg width="20" height="20"><rect width="20" height="20"/></svg></button><main id="messages"></main>
<script>
window.submits=0;
document.querySelector('button').onclick=()=>{
  window.submits++;const n=window.submits, box=document.querySelector('textarea'), prompt=box.value;
  const marker=prompt.match(/DOMPROBE_[A-F0-9]{12}/)?.[0];
  const user=document.createElement('article');user.setAttribute('data-content-search-unit-key',`fallback-turn-${n}:0:user`);
  const content=document.createElement('div');content.className='user-text whitespace-pre-wrap';content.textContent=prompt;
  user.append(content);document.querySelector('main').append(user);box.value='';
  const button=document.querySelector('button');button.disabled=true;
  setTimeout(()=>{
    const assistant=document.createElement('article');assistant.setAttribute('data-content-search-unit-key',`fallback-turn-${n}:1:assistant`);
    const p=document.createElement('p');p.className='answer';p.textContent=marker+' OK';assistant.append(p);document.querySelector('main').append(assistant);button.disabled=false;
  },350);
};
</script>'''


class ComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        original=dom_config.config_path
        self.config_patch=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None: original(provider,root=root or self.root))
        self.config_patch.start()
        self.context=self.browser.new_context()
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html; charset=utf-8',body=HTML))
        self.page=self.context.new_page();self.page.goto('https://chatgpt.com/component-fixture')
        dom_config.save_composer('chatgpt',self.page.url,'#composer',{})

    def tearDown(self):
        self.context.close();self.config_patch.stop();self.temp.cleanup()

    def save(self,name,selector):
        return dom_config.save_element('chatgpt',self.page.url,name,selector,{}, {dom_config.VALIDATION_KEYS[name]:True})

    def test_config_merges_and_runtime_loads_all_components(self):
        for name,selector in [('send','button[aria-label="Send"]'),('user_turn',USER),('user_content','.user-text'),('assistant_turn',ASSISTANT)]:
            self.save(name,selector)
        document=dom_config.load_dom_config('chatgpt')
        self.assertEqual(set(document['elements']),{'composer','send','user_turn','user_content','assistant_turn'})
        adapter=create_web_ui_for_page(self.page)
        for name,entry in document['elements'].items():
            self.assertEqual(getattr(adapter.profile,dom_config.ELEMENT_FIELDS[name]),entry['selector'])
        dom_config.save_composer('chatgpt',self.page.url,'#composer',{})
        self.assertIn('send',dom_config.load_dom_config('chatgpt')['elements'])
        self.assertEqual(document['elements']['user_content']['scope'],'user_turn')

    def test_turn_root_change_invalidates_only_relative_content(self):
        self.save('send','button');self.save('user_turn',USER);self.save('user_content','.user-text');self.save('assistant_turn',ASSISTANT)
        self.save('user_turn','article.other-user')
        document=dom_config.load_dom_config('chatgpt')
        self.assertNotIn('user_content',document['elements'])
        self.assertNotIn('assistant_turn',document['elements'])
        self.assertIn('send',document['elements'])

    def test_content_change_invalidates_assistant_ownership_proof(self):
        self.save('user_turn',USER);self.save('user_content','.user-text');self.save('assistant_turn',ASSISTANT)
        self.save('user_content','.other-user-text')
        self.assertNotIn('assistant_turn',dom_config.load_dom_config('chatgpt')['elements'])

    def test_assistant_requires_user_binding_config(self):
        with self.assertRaisesRegex(ValueError,'requires_user_binding'):
            self.save('assistant_turn',ASSISTANT)

    def test_unverified_component_cannot_publish(self):
        before=dom_config.config_path('chatgpt').read_bytes()
        with self.assertRaisesRegex(ValueError,'not_validated'):
            dom_config.save_element('chatgpt',self.page.url,'send','button',{}, {})
        self.assertEqual(before,dom_config.config_path('chatgpt').read_bytes())

    def test_button_picker_clicks_svg_without_submitting(self):
        self.page.evaluate('('+PICKER+')({field:"send",marker:"DOMPROBE_TEST",title:"Send",instructions:"Select"})')
        panel=self.page.locator('#smartagent-component-picker')
        panel.get_by_role('button',name='開始選取',exact=True).click()
        self.page.locator('svg rect').click()
        selected=self.page.evaluate('window.__smartAgentComponentPicker.selected')
        self.assertEqual(selected['selector'],'button[aria-label="Send"]')
        self.assertEqual(self.page.evaluate('window.submits'),0)
        panel.get_by_role('button',name='儲存並測試').click()
        self.assertEqual(self.page.evaluate('window.__smartAgentComponentPicker.action'),'save')

    def test_dynamic_turn_id_is_generalized_by_role(self):
        self.page.locator('main').evaluate('el=>el.innerHTML=`<article data-content-search-unit-key="fallback-turn-42:0:user"><div>Question</div></article>`')
        self.page.evaluate('('+PICKER+')({field:"user_turn",marker:"Question",title:"User",instructions:"Select"})')
        self.page.locator('#smartagent-component-picker').get_by_role('button',name='開始選取',exact=True).click()
        self.page.locator('article div').click()
        selected=self.page.evaluate('window.__smartAgentComponentPicker.selected')
        self.assertEqual(selected['selector'],USER)
        self.assertNotIn('42',selected['selector'])

    def test_assistant_markdown_style_does_not_hide_turn_identity(self):
        self.page.locator('main').evaluate('el=>el.innerHTML=`<article data-content-search-unit-key="fallback-turn-42:1:assistant"><div data-markdown-text-style="assistant-message"><p>Response</p></div></article>`')
        self.page.evaluate('('+PICKER+')({field:"assistant_turn",marker:"Response",title:"Assistant",instructions:"Select"})')
        self.page.locator('#smartagent-component-picker').get_by_role('button',name='開始選取',exact=True).click()
        self.page.locator('article p').click()
        selected=self.page.evaluate('window.__smartAgentComponentPicker.selected')
        self.assertEqual(selected['selector'],ASSISTANT)

    def test_known_sample_can_be_replaced_but_foreign_draft_cannot(self):
        write_sample(self.page)
        prepare_probe(self.page,make_probe(self.page))
        self.page.locator('textarea').fill('My own draft')
        with self.assertRaisesRegex(ValueError,'contains_draft'):
            prepare_probe(self.page,make_probe(self.page))
        self.assertEqual(self.page.locator('textarea').input_value(),'My own draft')

    def test_broad_user_selector_is_rejected(self):
        probe=make_probe(self.page);prepare_probe(self.page,probe)
        send_probe(self.page,probe,'button[aria-label="Send"]',timeout=3)
        with self.assertRaisesRegex(ValueError,'also_matches_assistant'):
            user_root(self.page,'article',probe)

    def test_ambiguous_submit_is_clicked_once_only(self):
        self.page.locator('button').evaluate('el=>el.onclick=()=>window.submits++')
        probe=make_probe(self.page);prepare_probe(self.page,probe)
        with self.assertRaisesRegex(TimeoutError,'no_automatic_resubmit'):
            send_probe(self.page,probe,'button',timeout=.3)
        self.assertEqual(self.page.evaluate('window.submits'),1)

    def test_user_bubble_accessibility_summary_is_not_another_turn(self):
        from ..message_text import read_message_text
        probe=make_probe(self.page)
        root=self.page.locator('main')
        root.evaluate('''(el,prompt)=>{
            const user=document.createElement('article');
            user.setAttribute('data-content-search-unit-key','test:0:user');
            const summary=document.createElement('h5');summary.className='cdk-visually-hidden';
            summary.textContent='你說了 '+prompt.slice(0,-10)+'…';
            const text=document.createElement('p');text.className='query-text-line';text.textContent=prompt;
            user.append(summary,text);el.append(user);
        }''',probe['prompt'])
        bubble=user_root(self.page,USER,probe)
        self.assertEqual(read_message_text(bubble),probe['prompt'])
        self.save('user_turn',USER)
        self.assertEqual(create_web_ui_for_page(self.page).turns('user')[0].raw_text,probe['prompt'])
        # An actually visible second message must still be rejected.
        bubble.evaluate('(el,prompt)=>{const p=document.createElement("p");p.textContent=prompt;el.append(p);}',probe['prompt'])
        with self.assertRaisesRegex(ValueError,'includes_other_turns'):
            user_root(self.page,USER,probe)

    def test_waits_for_send_readiness_without_early_click(self):
        probe=make_probe(self.page);prepare_probe(self.page,probe)
        self.page.locator('button').evaluate('el=>{el.disabled=true;setTimeout(()=>el.disabled=false,400)}')
        result=send_probe(self.page,probe,'button[aria-label="Send"]',timeout=3)
        self.assertTrue(result['user_accepted'])
        self.assertEqual(self.page.evaluate('window.submits'),1)

    def test_all_stages_use_saved_selectors_and_verify_distinct_probes(self):
        selectors={2:'button[aria-label="Send"]',3:USER,4:'.user-text',5:ASSISTANT}
        def select(page,stage,probe):
            page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            return {'selector':selectors[stage],'tag':'article','kind':'fixture'}
        with patch('agent_core.web_ui.component_calibration.select_component',side_effect=select):
            for stage in range(2,6):
                with self.subTest(stage=stage):
                    evidence=self.root/str(stage);evidence.mkdir()
                    result=calibrate_stage(self.page,stage,evidence,no_wait=True)
                    self.assertTrue(result['passed'])
                    self.assertTrue(result['runtime_config_loaded'])
                    if stage>2:self.assertEqual(len(set(result['probe_markers'])),2)
                    if stage==5:self.assertTrue(result['newest_assistant_owned'])
        self.assertEqual(self.page.evaluate('window.submits'),7)

    def test_recycled_single_user_node_does_not_prove_two_turns(self):
        self.save('send','button[aria-label="Send"]')
        self.page.locator('button').evaluate('el=>{const original=el.onclick;el.onclick=()=>{document.querySelector("main").replaceChildren();original();}}')
        def select(page,stage,probe):
            page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            return {'selector':USER,'tag':'article','kind':'user_turn'}
        with patch('agent_core.web_ui.component_calibration.select_component',side_effect=select):
            with self.assertRaisesRegex(AssertionError,'two_distinct_user_turns'):
                calibrate_stage(self.page,3,self.root,no_wait=True)
        self.assertNotIn('user_turn',dom_config.load_dom_config('chatgpt')['elements'])

    def test_assistant_body_without_identity_is_not_published(self):
        self.save('send','button[aria-label="Send"]');self.save('user_turn',USER);self.save('user_content','.user-text')
        def select(page,stage,probe):
            page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            return {'selector':'.answer','tag':'p','kind':'assistant_turn'}
        with patch('agent_core.web_ui.component_calibration.select_component',side_effect=select):
            with self.assertRaisesRegex(ValueError,'訊息識別'):
                calibrate_stage(self.page,5,self.root,no_wait=True)
        self.assertNotIn('assistant_turn',dom_config.load_dom_config('chatgpt')['elements'])


if __name__=='__main__':unittest.main()
