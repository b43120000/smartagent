"""Observed M365 outer article / nested reply identity and stage-5 regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright

from .. import dom_config
from ..component_calibration import PICKER, calibrate_stage, candidate_adapter
from ..factory import create_web_ui_for_page

HTML = '''<!doctype html><meta charset="utf-8">
<textarea id="composer"></textarea><button aria-label="Send">Send</button><main></main>
<script>
window.submits=0;
document.querySelector('button').onclick=()=>{
 const n=++window.submits, box=document.querySelector('textarea'), text=box.value;
 const marker=text.match(/DOMPROBE_[A-F0-9]+/)[0];
 const user=document.createElement('div');user.className='fai-UserMessage__message';user.textContent=text;
 document.querySelector('main').append(user);box.value='';
 const button=document.querySelector('button');button.disabled=true;
 setTimeout(()=>{
  const article=document.createElement('div');article.className='fai-CopilotMessage';
  article.setAttribute('role','article');article.setAttribute('name','[object Object]');
  const reply=document.createElement('div');reply.dataset.testid='markdown-reply';
  reply.dataset.messageId='m365-fixture-'+n;reply.setAttribute('aria-hidden','true');
  const p=document.createElement('p');p.textContent=marker+' OK';reply.append(p);article.append(reply);
  const controls=document.createElement('button');controls.textContent='Copy';article.append(controls);
  document.querySelector('main').append(article);button.disabled=false;
 },200);
};
</script>'''


class M365AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        original=dom_config.config_path
        patcher=patch.object(dom_config,'config_path',side_effect=lambda provider,root=None:original(provider,root=root or self.root))
        patcher.start();self.addCleanup(patcher.stop)
        self.context=self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=HTML))
        self.page=self.context.new_page();self.page.goto('https://m365.cloud.microsoft/chat')
        dom_config.save_composer('m365',self.page.url,'#composer',{})
        for name,selector in [('send','button[aria-label="Send"]'),('user_turn','div.fai-UserMessage__message'),('user_content',':scope')]:
            dom_config.save_element('m365',self.page.url,name,selector,{}, {dom_config.VALIDATION_KEYS[name]:True})

    def article(self, ids):
        self.page.locator('main').evaluate('''(main,ids)=>{
          const article=document.createElement('div');article.className='fai-CopilotMessage';
          article.setAttribute('name','[object Object]');
          for(const id of ids){const reply=document.createElement('div');reply.dataset.testid='markdown-reply';
          reply.dataset.messageId=id;reply.textContent='Response '+id;article.append(reply);}
          main.append(article);
        }''',ids)

    def test_outer_identity_comes_from_unique_nested_reply_and_ignores_busy_state(self):
        self.article(['one']);self.article(['two'])
        adapter=candidate_adapter(self.page,'assistant_turn','div.fai-CopilotMessage')
        first=adapter.turns('assistant')
        self.assertEqual(len(first),2)
        self.assertNotEqual(first[0].structural_id,first[1].structural_id)
        self.assertEqual(first[0].stable_attributes,(('data-message-id','one'),))
        self.page.locator('.fai-CopilotMessage').first.evaluate('el=>el.setAttribute("aria-busy","false")')
        self.assertEqual(first[0].structural_id,adapter.turns('assistant')[0].structural_id)

    def test_identity_cannot_be_borrowed_from_sibling_or_multiple_replies(self):
        self.article(['one','two']);self.article(['three'])
        adapter=candidate_adapter(self.page,'assistant_turn','div.fai-CopilotMessage')
        turns=adapter.turns('assistant')
        self.assertFalse(turns[0].stable_attributes)
        self.assertEqual(turns[1].stable_attributes,(('data-message-id','three'),))
        self.page.locator('.fai-CopilotMessage').first.locator('[data-message-id]').evaluate_all('els=>els.forEach(el=>el.removeAttribute("data-message-id"))')
        self.assertFalse(adapter.turns('assistant')[0].stable_attributes)

    def test_clicking_reply_selects_outer_semantic_frame(self):
        self.article(['one'])
        options={'field':'assistant_turn','marker':'Response','title':'Test','instructions':'Select',
                 **create_web_ui_for_page(self.page).calibration_picker_options('assistant_turn')}
        import json
        self.page.evaluate('('+PICKER+')('+json.dumps(options)+')')
        panel=self.page.locator('#smartagent-component-picker')
        panel.get_by_role('button',name='開始選取',exact=True).click()
        self.page.locator('[data-testid="markdown-reply"]').click()
        selected=self.page.evaluate('window.__smartAgentComponentPicker.selected')
        self.assertEqual(selected['selector'],'div.fai-CopilotMessage')
        self.assertNotIn('[object Object]',selected['selector'])

    def test_body_only_and_recycled_reply_ids_cannot_prove_fresh_answer(self):
        self.article(['same']);self.article(['same'])
        outer=candidate_adapter(self.page,'assistant_turn','div.fai-CopilotMessage')
        self.assertEqual(len(outer.turns('assistant')),1)
        inner=candidate_adapter(self.page,'assistant_turn','div[data-testid="markdown-reply"]')
        self.assertTrue(all(not t.stable_attributes for t in inner.turns('assistant')))

    def test_existing_role_marked_container_with_direct_identity_still_works(self):
        self.page.locator('main').evaluate('''main=>{
          const article=document.createElement('div');article.dataset.role='assistant';
          article.dataset.messageId='legacy-message-id';article.textContent='Legacy answer';main.append(article);
        }''')
        adapter=candidate_adapter(self.page,'assistant_turn','div[data-role="assistant"]')
        turn=adapter.turns('assistant')[0]
        self.assertIn(('data-message-id','legacy-message-id'),turn.stable_attributes)

    def test_completed_loading_message_wrapper_is_not_busy_but_live_signals_are(self):
        self.article(['one'])
        article=self.page.locator('.fai-CopilotMessage')
        article.evaluate('''el=>{
            const wrapper=document.createElement('div');wrapper.dataset.testid='loading-message';
            while(el.firstChild)wrapper.append(el.firstChild);el.append(wrapper);
        }''')
        adapter=candidate_adapter(self.page,'assistant_turn','div.fai-CopilotMessage')
        turn=adapter.turns('assistant')[0]
        self.assertFalse(adapter.media_state(turn)['media_pending'])

        wrapper=article.locator('[data-testid="loading-message"]')
        wrapper.evaluate('el=>el.setAttribute("aria-busy","true")')
        self.assertTrue(adapter.media_state(turn)['media_pending'])
        wrapper.evaluate('''el=>{el.removeAttribute('aria-busy');const progress=document.createElement('div');
            progress.setAttribute('role','progressbar');progress.textContent='Working';el.append(progress);}''')
        self.assertTrue(adapter.media_state(turn)['media_pending'])
        wrapper.locator('[role="progressbar"]').evaluate('el=>el.remove()')
        self.assertFalse(adapter.media_state(turn)['media_pending'])

    def test_request_failure_alert_blocks_before_stage9_without_retry(self):
        adapter=create_web_ui_for_page(self.page)
        self.page.evaluate('''()=>{
            const alert=document.createElement('div');alert.setAttribute('role','alert');
            alert.textContent='無法完成您的要求。重新整理以再試一次。';document.body.append(alert);
        }''')
        result=adapter.blocking_transport_error()
        self.assertEqual(result['reason'],'PROVIDER_ERROR')
        self.assertFalse(result['auto_resubmit'])
        self.page.locator('[role="alert"]').evaluate('el=>el.style.display="none"')
        self.assertEqual(adapter.blocking_transport_error(),{})
        self.article(['quoted-error'])
        self.page.locator('[data-message-id="quoted-error"]').evaluate('''el=>{
            const quote=document.createElement('div');quote.setAttribute('role','alert');
            quote.textContent='無法完成您的要求。重新整理以再試一次。';el.append(quote);
        }''')
        self.assertEqual(adapter.blocking_transport_error(),{})

    def test_full_stage5_two_turns_and_saved_runtime_config(self):
        def human(page,stage,probe,**kwargs):
            return {'selector':'div.fai-CopilotMessage','tag':'div','kind':'assistant_turn'}
        with patch('agent_core.web_ui.component_calibration.select_component',side_effect=human):
            # calibrate_stage finishes by notifying the normal picker.
            self.page.evaluate('window.__smartAgentComponentPicker={finish:()=>{}}')
            report=calibrate_stage(self.page,5,self.root,no_wait=True)
        self.assertTrue(report['newest_assistant_owned'])
        self.assertTrue(report['old_response_rejected'])
        self.assertTrue(report['runtime_config_loaded'])
        self.assertEqual(self.page.evaluate('window.submits'),2)
        self.assertEqual(len(create_web_ui_for_page(self.page).turns('assistant')),2)


if __name__=='__main__':unittest.main()
