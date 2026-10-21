"""Observed Claude user bubble selection and repeated transcript-row identity."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright

from .. import dom_config
from ..component_calibration import PICKER, calibrate_stage, candidate_adapter, make_probe, prepare_probe, send_probe, user_root
from ..factory import create_web_ui_for_page

BUBBLE='div[data-cds="UserMessage"] > div.relative:has(.cds-user-message-body)'
HTML='''<!doctype html><textarea id="composer"></textarea><button id="send">Send</button><main class="relative"></main>
<script>
window.submits=0;document.querySelector('#send').onclick=()=>{
 const n=window.submits++,editor=document.querySelector('textarea'),text=editor.value;
 const row=document.createElement('div');row.dataset.testid='transcript-row';row.dataset.perfRow='human';row.dataset.index=String(n*2);
 const user=document.createElement('div');user.dataset.cds='UserMessage';
 const bubble=document.createElement('div');bubble.className='relative bubble';
 const body=document.createElement('div');body.className='cds-user-message-body';
 const message=document.createElement('div');message.dataset.testid='user-message';message.className='relative';
 const p=document.createElement('p');p.textContent=text;message.append(p);body.append(message);bubble.append(body);user.append(bubble);row.append(user);
 document.querySelector('main').append(row);editor.value='';document.querySelector('#send').disabled=true;
 setTimeout(()=>{const answer=document.createElement('div');answer.dataset.testid='assistant-message';
 const replyRow=document.createElement('div');replyRow.dataset.testid='transcript-row';replyRow.dataset.perfRow='assistant';replyRow.dataset.index=String(n*2+1);
 answer.dataset.isStreaming='false';answer.textContent=text.match(/DOMPROBE_[A-F0-9]+/)[0]+' OK';replyRow.append(answer);document.querySelector('main').append(replyRow);document.querySelector('#send').disabled=false;},200);
};
</script>'''


class ClaudeUserTurnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        original=dom_config.config_path
        patcher=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:original(provider,root=root or self.root))
        patcher.start();self.addCleanup(patcher.stop)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=HTML))
        self.page=self.context.new_page();self.page.goto('https://claude.ai/chat/fixture')
        dom_config.save_composer('claude',self.page.url,'#composer',{})
        dom_config.save_element('claude',self.page.url,'send','#send',{}, {'click_verified':True})

    def submit(self):
        probe=make_probe(self.page);prepare_probe(self.page,probe)
        send_probe(self.page,probe,'#send',timeout=3)
        return probe

    def test_click_selects_full_bubble_not_generic_relative_class(self):
        probe=self.submit()
        options=dict(field='user_turn',marker=probe['marker'],title='User',instructions='Select',
                     **create_web_ui_for_page(self.page).calibration_picker_options('user_turn'))
        self.page.evaluate('('+PICKER+')('+json.dumps(options)+')')
        self.page.locator('#smartagent-component-picker').get_by_role('button',name='開始選取',exact=True).click()
        self.page.locator('[data-testid="user-message"] p').click()
        self.assertEqual(self.page.evaluate('window.__smartAgentComponentPicker.selected.selector'),BUBBLE)
        self.assertEqual(user_root(self.page,BUBBLE,probe).count(),1)

    def test_broad_selector_still_rejected_and_repeated_testids_are_distinct(self):
        probe=self.submit();self.submit()
        with self.assertRaisesRegex(ValueError,'includes_other_turns'):
            user_root(self.page,'div.relative, main.relative',probe)
        adapter=candidate_adapter(self.page,'user_turn',BUBBLE)
        turns=adapter.turns('user')
        self.assertEqual(len(turns),2)
        self.assertNotEqual(turns[0].structural_id,turns[1].structural_id)
        # The old shared testid no longer collapses multiple user messages.
        self.assertEqual(len(candidate_adapter(self.page,'user_turn','[data-testid="user-message"]').turns('user')),2)
        # Virtualized transcripts may unmount an earlier row. The surviving
        # user anchor must not change just because its visible ordinal changes.
        self.page.locator('[data-testid="transcript-row"]').first.evaluate('el=>el.remove()')
        self.assertEqual(adapter.turns('user')[0].structural_id,turns[1].structural_id)

    def test_stage3_verifies_two_user_turns_and_publishes_config(self):
        with patch.object(sys.modules[calibrate_stage.__module__],'select_component',return_value=dict(selector=BUBBLE,tag='div',kind='user_turn')):
            self.page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            result=calibrate_stage(self.page,3,self.root,no_wait=True)
        self.assertTrue(result['repeated_turn_verified'])
        self.assertTrue(result['runtime_config_loaded'])
        self.assertEqual(len(set(result['distinct_user_turn_ids'])),2)

    def test_assistant_identity_survives_streaming_and_virtualization(self):
        self.submit();self.submit()
        adapter=candidate_adapter(self.page,'assistant_turn','div[data-testid="assistant-message"]')
        turns=adapter.turns('assistant')
        self.assertEqual(len(turns),2)
        self.assertNotEqual(adapter.durable_turn_id(turns[0]),adapter.durable_turn_id(turns[1]))
        self.page.locator('[data-testid="assistant-message"]').last.evaluate('el=>el.dataset.isStreaming="true"')
        self.assertEqual(adapter.durable_turn_id(adapter.turns('assistant')[-1]),adapter.durable_turn_id(turns[-1]))
        self.page.locator('[data-perf-row="assistant"]').first.evaluate('el=>el.remove()')
        self.assertEqual(adapter.durable_turn_id(adapter.turns('assistant')[0]),adapter.durable_turn_id(turns[-1]))

    def test_stage5_verifies_fresh_owned_assistant_and_publishes_config(self):
        dom_config.save_element('claude',self.page.url,'user_turn',BUBBLE,{}, {dom_config.VALIDATION_KEYS['user_turn']:True})
        dom_config.save_element('claude',self.page.url,'user_content','.cds-user-message-body',{}, {dom_config.VALIDATION_KEYS['user_content']:True})
        flow=sys.modules[calibrate_stage.__module__]
        with patch.object(flow,'select_component',return_value=dict(selector='div[data-testid="assistant-message"]',tag='div',kind='assistant_turn')):
            self.page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            result=calibrate_stage(self.page,5,self.root,no_wait=True)
        self.assertTrue(result['newest_assistant_owned'])
        self.assertTrue(result['old_response_rejected'])
        self.assertTrue(result['runtime_config_loaded'])

    def test_long_rendered_prompt_binds_with_fixed_count_and_unique_submit_id(self):
        self.submit()
        dom_config.save_element('claude',self.page.url,'user_turn',BUBBLE,{}, {dom_config.VALIDATION_KEYS['user_turn']:True})
        dom_config.save_element('claude',self.page.url,'user_content','p.whitespace-pre-wrap',{}, {dom_config.VALIDATION_KEYS['user_content']:True})
        adapter=candidate_adapter(self.page,'assistant_turn','div[data-testid="assistant-message"]')
        prompt=adapter.prepare_transport_prompt('Initialize `canonical` protocol\n'+'long body '*3000+'\nEnd')
        scope=adapter.capture_request(prompt)
        # The transcript recycles a mounted row; Markdown strips backticks.
        self.page.locator('.cds-user-message-body').evaluate('''(el,text)=>{
            el.replaceChildren();for(const part of text.replaceAll('`','').split('\\n')){
                const p=document.createElement('p');p.className='whitespace-pre-wrap';p.textContent=part;el.append(p);
            }el.closest('[data-testid="transcript-row"]').dataset.index='100';
        }''',prompt)
        self.assertEqual(len(adapter.turns('user')),scope.baseline_user_count)
        self.assertGreater(len(adapter.turns('user')[0].raw_text),30000)
        self.assertIsNotNone(adapter.confirm_user_turn(scope))
        self.assertIsNone(adapter.latest_owned_assistant(scope)) # Old response is still rejected.
        self.page.locator('[data-testid="assistant-message"]').evaluate('''el=>{
            el.textContent='fresh response';el.closest('[data-testid="transcript-row"]').dataset.index='101';
        }''')
        self.assertIsNotNone(adapter.latest_owned_assistant(scope))
        other=adapter.capture_request(adapter.prepare_transport_prompt('different request'))
        self.assertIsNone(adapter.confirm_user_turn(other))


if __name__=='__main__':unittest.main()
