"""Request-bound, redacted diagnostics cannot change user/assistant admission."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from ..contracts import RequestScope, TurnRef, text_digest
from ..base_adapter import BaseWebUIAdapter
from ..user_binding_diagnostic import diagnose_user_binding


class BindingDiagnosticTests(unittest.TestCase):
    def setup_case(self,expected='expected private prompt',texts=None):
        texts=texts if texts is not None else ['observed private prompt']
        profile=SimpleNamespace(user_content_selector='.body',user_turn_selector='.user')
        turns=tuple(TurnRef('user',i+1,'old-'+str(i),text_digest(text),text,
                    element=SimpleNamespace(evaluate=lambda js:True,query_selector_all=lambda selector:[object()]))
                    for i,text in enumerate(texts))
        scope=RequestScope('WEBUI-TEST','private-url','fixture',text_digest(expected),expected,
                           tuple(t.structural_id for t in turns),tuple(t.content_digest for t in turns),(),len(turns),0)
        adapter=SimpleNamespace(_active_profile=lambda:profile,turns=lambda role:turns,
            turn_identity_candidates=lambda turn:(),page=SimpleNamespace(locator=lambda selector:SimpleNamespace(count=lambda:len(turns))))
        return adapter,scope,turns

    def test_mismatch_is_redacted_and_does_not_bind(self):
        adapter,scope,turns=self.setup_case();before=scope.__dict__.copy()
        result=diagnose_user_binding(adapter,scope)
        self.assertEqual(result['diagnostic_reason'],'no_text_match')
        self.assertEqual(result['candidates'][0]['first_difference'],0)
        for secret in ['expected private prompt','observed private prompt','private-url']:
            self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(scope.__dict__,before)

    def test_whitespace_only_difference_matches_without_newness(self):
        adapter,scope,_=self.setup_case('hello world',['hello\nworld'])
        result=diagnose_user_binding(adapter,scope)
        self.assertTrue(result['candidates'][0]['text_match'])
        self.assertEqual(result['diagnostic_reason'],'text_match_without_newness')
        self.assertIsNone(BaseWebUIAdapter._confirmed_user_turn_candidate(None,scope,adapter.turns('user')))

    def test_truncation_and_nonwhitespace_difference(self):
        adapter,scope,_=self.setup_case('abcdef',['abc','abZdef'])
        a,b=diagnose_user_binding(adapter,scope)['candidates']
        self.assertTrue(a['actual_is_prefix']);self.assertEqual(a['first_difference'],3)
        self.assertEqual(b['first_nonwhitespace_difference'],2)

    def test_missing_content_and_detached_element(self):
        adapter,scope,turns=self.setup_case()
        turns[0].element.query_selector_all=lambda selector:[]
        self.assertEqual(diagnose_user_binding(adapter,scope)['candidates'][0]['observation_reason'],'content_selector_missing')
        turns[0].element.evaluate=lambda js:False
        self.assertEqual(diagnose_user_binding(adapter,scope)['candidates'][0]['observation_reason'],'element_detached')

    def test_no_candidates_and_multiple_submit_markers(self):
        adapter,scope,_=self.setup_case(texts=[])
        self.assertEqual(diagnose_user_binding(adapter,scope)['diagnostic_reason'],'no_user_candidates')
        adapter,scope,_=self.setup_case(texts=['WUS-ABC','WUS-ABC'])
        scope.request_markers=('WUS-ABC',)
        self.assertEqual(diagnose_user_binding(adapter,scope)['diagnostic_reason'],'submit_marker_not_unique')

    def test_original_request_not_final_render_is_compared(self):
        expected='[WEBAGENT_ACTIVE_REQUEST]\n{"request_id":"WA-TEST-123"}\nprivate task'
        adapter,scope,_=self.setup_case(expected,[expected,'[WEBAGENT_FINAL_RENDER] private display'])
        result=diagnose_user_binding(adapter,scope)
        self.assertEqual(result['request_id'],'WA-TEST-123')
        self.assertEqual(result['prompt_kind'],'WEBAGENT_ACTIVE_REQUEST')
        self.assertTrue(result['candidates'][0]['text_match'])
        self.assertFalse(result['candidates'][1]['text_match'])
        self.assertEqual(result['candidates'][1]['prompt_kind'],'WEBAGENT_FINAL_RENDER')

    def test_bounded_output_keeps_early_matching_candidate(self):
        adapter,scope,_=self.setup_case('expected',['expected']+['x'*100000]*20)
        result=diagnose_user_binding(adapter,scope)
        self.assertLessEqual(len(result['candidates']),6)
        self.assertEqual(result['candidates'][0]['ordinal'],1)
        self.assertLess(len(json.dumps(result)),10000)

    def test_newness_metrics_do_not_change_candidate_decision(self):
        adapter,scope,turns=self.setup_case('expected',['expected'])
        scope.baseline_user_ids=();scope.baseline_user_content_digests=();scope.baseline_user_count=0
        before=BaseWebUIAdapter._confirmed_user_turn_candidate(None,scope,turns)
        result=diagnose_user_binding(adapter,scope)
        self.assertEqual(result['fresh_text_match_count'],1)
        self.assertIs(before,BaseWebUIAdapter._confirmed_user_turn_candidate(None,scope,turns))
        self.assertIsNone(scope.user_turn)

    def test_runtime_tag_and_error_are_bound_to_scope_without_exception_text(self):
        from agent_core.web_runtime import WebLLMScraper
        adapter,scope,_=self.setup_case();events=[]
        runtime=SimpleNamespace(_web_ui_adapter=lambda:adapter,_log_stage=lambda tag,detail:events.append((tag,json.loads(detail))))
        WebLLMScraper._log_user_binding_diagnostic(runtime,{'_web_ui_scope':scope},'assistant_start_stalled')
        self.assertEqual(events[0][0],'user_binding_diagnostic')
        self.assertEqual(events[0][1]['scope_id'],scope.scope_id)
        self.assertEqual(events[0][1]['trigger'],'assistant_start_stalled')
        adapter.turns=Mock(side_effect=RuntimeError('private exception text'))
        WebLLMScraper._log_user_binding_diagnostic(runtime,{'_web_ui_scope':scope},'generation_fallback')
        self.assertEqual(events[-1][0],'user_binding_diagnostic_failed')
        self.assertNotIn('private exception',json.dumps(events))

    def test_broken_log_sink_never_escapes(self):
        from agent_core.web_runtime import WebLLMScraper
        adapter,scope,_=self.setup_case()
        runtime=SimpleNamespace(_web_ui_adapter=lambda:adapter,
            _log_stage=Mock(side_effect=BrokenPipeError('private sink failure')))
        WebLLMScraper._log_user_binding_diagnostic(runtime,{'_web_ui_scope':scope},'generation_fallback')
        adapter.turns=Mock(side_effect=RuntimeError('private diagnostic failure'))
        WebLLMScraper._log_user_binding_diagnostic(runtime,{'_web_ui_scope':scope},'assistant_start_stalled')
        self.assertIsNone(scope.user_turn)

    def test_real_stall_path_logs_original_scope_and_still_rejects(self):
        from agent_core.web_runtime import WebLLMScraper, WebScraperStageError
        adapter,scope,_=self.setup_case()
        adapter.latest_owned_assistant=lambda scope:None
        rt=object.__new__(WebLLMScraper);rt._generation_stall_sec=0
        rt._active_generation_emergency_sec=0;rt._web_ui_adapter=lambda:adapter
        for name in ['_run_control_hook','_check_cancel_requested','_check_provider_transport_error']:
            setattr(rt,name,lambda *args:None)
        rt._maybe_recover_disconnected_generation=lambda:False
        rt._turn_elements=lambda role:[];rt._response_elements=lambda:[]
        rt._is_generation_active=lambda:False;rt._disconnect_signature_visible=lambda:False
        rt._assistant_wait_diagnostics=lambda *args:{}
        events=[];rt._log_stage=lambda tag,detail:events.append((tag,detail))
        with self.assertRaisesRegex(WebScraperStageError,'WEB_ASSISTANT_START_STALLED'):
            rt._wait_for_new_assistant_turn({'_web_ui_scope':scope},allow_fresh_ready_image=False)
        report=json.loads(next(detail for tag,detail in events if tag=='user_binding_diagnostic'))
        self.assertEqual(report['scope_id'],scope.scope_id)
        self.assertEqual(report['trigger'],'assistant_start_stalled')
        self.assertIsNone(scope.user_turn)


if __name__=='__main__':unittest.main()
