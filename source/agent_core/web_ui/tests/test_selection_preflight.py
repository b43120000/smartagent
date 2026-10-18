"""Panel states, provider-backed read-only checks and stale-selection protection."""
import json
import unittest

from . import test_components as fixtures
from ..component_calibration import PICKER, make_probe, prepare_probe, send_probe, select_component, candidate_adapter
from ..selection_preflight import check_component_selection
from ..manual_adapter import install_picker
from .write_send_box import verify_composer_on_page


class SelectionPreflightTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ComponentTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ComponentTests.tearDownClass.__func__)
    setUp = fixtures.ComponentTests.setUp
    tearDown = fixtures.ComponentTests.tearDown
    save = fixtures.ComponentTests.save

    def prepare(self, stage):
        probe=make_probe(self.page);prepare_probe(self.page,probe)
        if stage!=2:send_probe(self.page,probe,'button',timeout=3)
        options=dict(field={2:'send',3:'user_turn',4:'user_content',5:'assistant_turn'}[stage],
                     marker=probe['marker'],title='Preflight test',instructions='Select',preflightBridge=True)
        if stage in (4,5):
            self.save('user_turn',fixtures.USER)
            if stage==5:self.save('user_content','.user-text')
        if stage==4:options['rootSelector']=fixtures.USER
        self.page.evaluate('('+PICKER+')('+json.dumps(options)+')')
        return probe

    def choose(self,selector):
        panel=self.page.locator('#smartagent-component-picker')
        panel.locator('#pick').click()
        self.page.locator(selector).click()
        return self.page.evaluate('''()=>{const s=window.__smartAgentComponentPicker;return {selection:s.selected,revision:s.revision}}''')

    def apply(self,stage,probe,state):
        result=check_component_selection(self.page,stage,probe,state['selection'])
        self.page.evaluate('data=>window.__smartAgentComponentPicker.applyPreflight(data.result,data.revision)',dict(result=result,revision=state['revision']))
        return result

    def test_correct_assistant_three_states_and_preflight_never_submits(self):
        probe=self.prepare(5)
        state=self.choose(fixtures.ASSISTANT+' p')
        panel=self.page.locator('#smartagent-component-picker')
        self.assertTrue(panel.get_by_role('button',name='儲存並測試').is_disabled())
        result=self.apply(5,probe,state)
        self.assertTrue(result['passed'])
        self.assertIn('初步檢查通過，待功能驗證',panel.locator('#status').inner_text())
        self.assertEqual(self.page.evaluate('window.submits'),1)
        self.assertFalse(panel.get_by_role('button',name='儲存並測試').is_disabled())
        panel.locator('#save').click()
        self.assertTrue(self.page.evaluate('revision=>window.__smartAgentComponentPicker.commitSelection(revision)',state['revision']))
        self.page.evaluate('window.__smartAgentComponentPicker.finish("已通過兩輪測試")')
        self.assertIn('功能驗證通過',panel.locator('#status').inner_text())

    def test_wrong_role_reports_reason_and_disables_save(self):
        probe=self.prepare(5)
        state=self.choose(fixtures.USER+' div')
        result=self.apply(5,probe,state)
        self.assertFalse(result['passed'])
        panel=self.page.locator('#smartagent-component-picker')
        self.assertIn('選取不符',panel.locator('#status').inner_text())
        self.assertIn('不能包含使用者問題',panel.locator('#checks').inner_text())
        self.assertTrue(panel.get_by_role('button',name='儲存並測試').is_disabled())

    def test_shared_testid_preserves_messages_and_reports_identity_pending(self):
        probe=self.prepare(5)
        self.page.locator(fixtures.ASSISTANT).evaluate('''el=>{
            el.removeAttribute('data-content-search-unit-key');el.dataset.testid='assistant-message';
            el.querySelector('p').id='current-reply';
            const older=el.cloneNode(true);older.querySelector('p').removeAttribute('id');
            older.querySelector('p').textContent='Older response';el.before(older);
        }''')
        selector='article[data-testid="assistant-message"]'
        adapter=candidate_adapter(self.page,'assistant_turn',selector)
        turns=adapter.turns('assistant')
        self.assertEqual(len(turns),2)
        self.assertEqual(turns[0].structural_id,turns[1].structural_id)
        self.assertFalse(adapter.turn_identity_candidates(turns[0]))
        state=self.choose('#current-reply')
        result=self.apply(5,probe,state)
        self.assertTrue(result['passed'])
        identity=next(row for row in result['checks'] if row['label']=='訊息識別')
        self.assertIsNone(identity['passed'])
        self.assertEqual(identity['detail'],'已找到識別欄位，唯一性待確認')
        # Neither changing text nor adding another shared-testid node proves a
        # fresh response. Only the later ownership test can establish that.
        scope=adapter.capture_request('next probe')
        self.page.locator(selector).last.evaluate('''el=>{
            const newer=el.cloneNode(true);newer.querySelector('p').id='next-reply';
            newer.querySelector('p').textContent='New text, same type';el.after(newer);
        }''')
        self.assertEqual(len(adapter.turns('assistant')),3)
        self.assertFalse(adapter.assistant_is_fresh(adapter.turns('assistant')[-1],scope))
        from dataclasses import replace
        changed_state=replace(adapter.turns('assistant')[-1],structural_id='streaming-state-changed',
                              stable_attributes=(('data-testid','assistant-message'),('data-is-streaming','false')))
        self.assertFalse(adapter.assistant_is_fresh(changed_state,scope))

    def test_nested_shared_type_nodes_still_fail_unique_mapping(self):
        probe=self.prepare(5)
        self.page.locator(fixtures.ASSISTANT).evaluate('''el=>{
            el.removeAttribute('data-content-search-unit-key');el.dataset.testid='assistant-message';
            const inner=document.createElement('div');inner.dataset.testid='assistant-message';
            inner.append(el.querySelector('p'));el.append(inner);inner.querySelector('p').id='current-reply';
        }''')
        state=self.choose('#current-reply')
        state['selection']['selector']='[data-testid="assistant-message"]'
        result=self.apply(5,probe,state)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checks'][-1]['label'],'本輪唯一定位')

    def test_broad_locator_and_missing_identity_are_rejected(self):
        probe=self.prepare(5)
        state=self.choose(fixtures.ASSISTANT+' p')
        state['selection']['selector']='main'
        self.assertFalse(self.apply(5,probe,state)['passed'])
        # A text-only node can contain the marker while lacking message identity.
        self.page.locator('article p').evaluate('el=>el.className="body-only"')
        self.page.evaluate('''()=>{
          const s=window.__smartAgentComponentPicker;
          s.getSelectedElement=()=>document.querySelector('.body-only');
        }''')
        state['selection']['selector']='.body-only'
        result=self.apply(5,probe,state)
        self.assertFalse(result['passed'])
        self.assertIn('識別資訊',result['checks'][-1]['detail'])

    def test_old_result_cannot_validate_new_selection_and_detached_cannot_save(self):
        probe=self.prepare(5)
        old=self.choose(fixtures.ASSISTANT+' p')
        result=check_component_selection(self.page,5,probe,old['selection'])
        new=self.choose(fixtures.USER+' div')
        applied=self.page.evaluate('data=>window.__smartAgentComponentPicker.applyPreflight(data.result,data.revision)',dict(result=result,revision=old['revision']))
        self.assertFalse(applied)
        self.assertFalse(self.page.evaluate('revision=>window.__smartAgentComponentPicker.commitSelection(revision)',old['revision']))
        self.page.locator(fixtures.USER).evaluate('el=>el.remove()')
        self.assertFalse(self.apply(5,probe,new)['passed'])

    def test_cancel_cannot_be_overwritten_by_pending_preflight(self):
        probe=self.prepare(5)
        state=self.choose(fixtures.ASSISTANT+' p')
        result=check_component_selection(self.page,5,probe,state['selection'])
        self.page.locator('#smartagent-component-picker').locator('#cancel').click()
        applied=self.page.evaluate('data=>window.__smartAgentComponentPicker.applyPreflight(data.result,data.revision)',dict(result=result,revision=state['revision']))
        self.assertFalse(applied)
        self.assertEqual(self.page.evaluate('window.__smartAgentComponentPicker.action'),'cancel')

    def test_real_polling_loop_rechecks_and_commits_without_submitting(self):
        probe=make_probe(self.page);prepare_probe(self.page,probe)
        self.page.evaluate('''() => {
            const timer=setInterval(()=>{
                const s=window.__smartAgentComponentPicker;
                if(!s)return;
                const ui=document.querySelector('#smartagent-component-picker').shadowRoot;
                if(!s.selected){ui.querySelector('#pick').click();document.querySelector('button[aria-label="Send"]').click();}
                if(s.phase==='preflight'){ui.querySelector('#save').click();clearInterval(timer);}
            },50);
        }''')
        result=select_component(self.page,2,probe,timeout=8)
        self.assertTrue(result['preflight']['passed'])
        self.assertEqual(self.page.evaluate('window.submits'),0)

    def test_send_user_and_content_checks(self):
        for stage,selector in [(2,'button[aria-label="Send"]'),(3,fixtures.USER+' div'),(4,fixtures.USER+' div')]:
            with self.subTest(stage=stage):
                if stage!=2:
                    # A fresh document/picker per stage, with the same saved fixtures.
                    self.page.reload()
                probe=self.prepare(stage)
                state=self.choose(selector)
                self.assertTrue(self.apply(stage,probe,state)['passed'])

    def test_composer_preflight_and_verified_state_require_actual_roundtrip(self):
        install_picker(self.page)
        panel=self.page.locator('#smartagent-composer-picker')
        panel.get_by_role('button',name='開始選取',exact=True).click()
        self.page.locator('textarea').click()
        self.assertIn('初步檢查通過，待功能驗證',panel.locator('#status').inner_text())
        self.page.evaluate('window.__smartAgentComposerPicker.finish("設定已儲存")')
        self.assertEqual(self.page.evaluate('window.__smartAgentComposerPicker.phase'),'preflight')
        result=verify_composer_on_page(self.page,no_wait=True)
        self.assertTrue(result['roundtrip_match'])
        self.assertIn('功能驗證通過',panel.locator('#status').inner_text())
        self.assertEqual(self.page.evaluate('window.submits'),0)


if __name__=='__main__':unittest.main()
