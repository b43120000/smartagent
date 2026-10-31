"""Bounded writer, strict evidence, cancellation and no-replay regression."""
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'source'))
from agent_core import composer_operation as co
from agent_core.web_runtime import WebLLMScraper, WebScraperStageError


def run():
    # bbdaf06 backport: short multiline text must be typed via Shift+Enter,
    # not by insert_text('first\nsecond') (ProseMirror may drop the suffix).
    assert list(co._composer_write_units('first\nsecond')) == [
        (0,5,'first',False), (5,6,'',True), (6,12,'second',False),
    ]
    assert list(co._composer_write_units('first\r\nsecond')) == [
        (0,5,'first',False), (5,7,'',True), (7,13,'second',False),
    ]
    assert list(map(lambda x: (x[0],x[1]), co._composer_write_units('A'*5000))) == [
        (0,4096), (4096,5000),
    ]
    entered=['']
    calls=[]
    class Keyboard:
        def press(self, key):
            calls.append(('press', key))
            if key == 'Shift+Enter':
                entered[0] += '\n'
        def insert_text(self, value):
            assert '\n' not in value and '\r' not in value
            calls.append(('insert', value))
            entered[0] += value
    class Scraper:
        _page=type('Page',(),{'keyboard':Keyboard()})()
        def _composer_matches_prompt(self, expected):return entered[0] == expected
        def _focus_composer_dom(self):return None
        def _normalize_composer_text(self, value):return value
        def _check_cancel_requested(self, stage):return None
        def _log_stage(self,*args):return None
    with tempfile.TemporaryDirectory() as temp:
        journal=Path(temp)/'composer.json'
        co._write_staged_prompt(Scraper(),'first\nsecond',{},journal)
        assert entered[0]=='first\nsecond'
        assert ('press','Shift+Enter') in calls
        assert all('\n' not in value for op,value in calls if op=='insert')
        assert json.loads(journal.read_text(encoding='utf-8'))['confirmed_prefix']['chars']==12
    before=dict(url='https://chatgpt.com/c/test',user_count=2,assistant_count=2,
                user_anchors=[['u','abc']],assistant_anchors=[['a','def']],generation_active=False)
    after={**before,'composer_matches':True}
    def fake_result(status):
        def result(payload,*args,**kwargs):
            if payload['mode']=='recover':return {'status':'UNOWNED_DRAFT'}
            if payload['mode']=='write':
                path=Path(payload['journal']);record=json.loads(path.read_text(encoding='utf-8'))
                record['baseline']=before
                co.write_json_atomic(path,record)
            return {'status':status}
        return result
    assert co.classify_observation(json.loads(json.dumps(before)),after)=='VERIFIED_SAFE_TO_SUBMIT'
    for change in ({'user_count':3},{'assistant_anchors':[['a2','x']]},{'generation_active':True}, {'url':'other'}):
        assert co.classify_observation(before,{**after,**change})=='TURN_CHANGED_OR_SUBMIT_UNCERTAIN'
    assert co.classify_observation(before,{**after,'composer_matches':False})=='PARTIAL_OR_EMPTY'
    assert co.classify_observation(None,after)=='UNKNOWN'
    assert co.page_key('http://localhost:1272','https://chatgpt.com/g/test/c/id?q=1')==co.page_key('http://localhost:1272','https://chatgpt.com/c/id')
    for status in ('VERIFIED_SAFE_TO_SUBMIT','PARTIAL_OR_EMPTY','UNKNOWN','TURN_CHANGED_OR_SUBMIT_UNCERTAIN'):
        with tempfile.TemporaryDirectory() as temp:
            args=dict(endpoint='http://127.0.0.1:1272',url=before['url'],prompt='secret prompt',journal_root=temp)
            with patch.object(co,'run_helper',side_effect=fake_result(status)) as helper:
                if status=='VERIFIED_SAFE_TO_SUBMIT':
                    assert co.prepare_composer(**args)['status']==status
                    assert helper.call_count==2 # independent observer
                else:
                    try: co.prepare_composer(**args)
                    except co.ComposerOperationError: pass
                    else: raise AssertionError(status)
                    calls=helper.call_count
                    try: co.prepare_composer(**args)
                    except co.ComposerOperationError as exc: assert 'recovery_required' in str(exc)
                    else: raise AssertionError('replay')
                    assert helper.call_count==calls+1 # readonly recovery, no writer replay
            journal=json.loads(next(Path(temp).glob('*.json')).read_text())
            assert 'secret prompt' not in json.dumps(journal)
    with tempfile.TemporaryDirectory() as temp:
        def timeout_with_baseline(payload,*args,**kwargs):
            result=fake_result('VERIFIED_SAFE_TO_SUBMIT')(payload)
            if payload['mode']=='write':raise subprocess.TimeoutExpired('writer',.1)
            return result
        with patch.object(co,'run_helper',side_effect=timeout_with_baseline):
            try: co.prepare_composer(endpoint='http://localhost:1272',url=before['url'],prompt='x',journal_root=temp)
            except co.ComposerOperationError as exc: assert exc.evidence['status']=='WRITER_TIMEOUT'
            else: raise AssertionError('late complete must not submit')
    # A real hanging Python child, contained before stdin release. No browser.
    original_popen=subprocess.Popen
    children=[]
    def hanging_child(_command,**kwargs):
        proc=original_popen([sys.executable,'-c','import sys,time;sys.stdin.buffer.read();time.sleep(60)'],**kwargs)
        children.append(proc);return proc
    started=time.monotonic()
    with patch.object(co.subprocess,'Popen',side_effect=hanging_child):
        try: co.run_helper({},.4)
        except subprocess.TimeoutExpired: pass
        else: raise AssertionError('deadline')
    assert time.monotonic()-started<7 and children[0].poll() is not None
    with patch.object(co.subprocess,'Popen',side_effect=hanging_child):
        try: co.run_helper({},3,cancel_check=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        except KeyboardInterrupt: pass
        else: raise AssertionError('cancel')
    assert children[-1].poll() is not None
    with tempfile.TemporaryDirectory() as temp:
        args=dict(endpoint='http://localhost:1272',url=before['url'],prompt='x',journal_root=temp)
        with patch.object(co,'run_helper',side_effect=fake_result('VERIFIED_SAFE_TO_SUBMIT')):
            try: co.prepare_composer(**args,cancel_check=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
            except KeyboardInterrupt: pass
            else: raise AssertionError('cancel overrides completion')
        assert json.loads(next(Path(temp).glob('*.json')).read_text())['state']=='QUARANTINED'
    with tempfile.TemporaryDirectory() as temp:
        args=dict(endpoint='http://localhost:1272',url=before['url'],prompt='x',journal_root=temp)
        with patch.object(co,'run_helper',side_effect=fake_result('VERIFIED_SAFE_TO_SUBMIT')) as helper:
            prepared=co.prepare_composer(**args)
            co.verify_prepared_composer(**args,operation_id=prepared['operation_id'])
            calls=helper.call_count
            try:co.prepare_composer(**args)
            except co.ComposerOperationError:pass
            else:raise AssertionError('reserved submit overwritten')
            assert helper.call_count==calls+1 and helper.call_args.args[0]['mode']=='recover'
            co.mark_composer_submitted(endpoint=args['endpoint'],url=args['url'],journal_root=temp,operation_id=prepared['operation_id'])
            co.mark_delivery_confirmed(endpoint=args['endpoint'],url=args['url'],journal_root=temp,operation_id=prepared['operation_id'])
            co.prepare_composer(**args) # subsequent request only after actual sent marker
    with tempfile.TemporaryDirectory() as temp:
        args=dict(endpoint='http://localhost:1272',url=before['url'],prompt='x',journal_root=temp)
        with patch.object(co,'run_helper',return_value={'status':'TARGET_NOT_UNIQUE'}) as helper:
            for _ in range(2):
                try:co.prepare_composer(**args)
                except co.ComposerOperationError as exc:assert exc.evidence['mutation_possible'] is False
                else:raise AssertionError('duplicate target')
            assert helper.call_count==4 # later explicit request not quarantined forever
    scraper=WebLLMScraper.__new__(WebLLMScraper)
    scraper.service='chatgpt';scraper._page=type('Page',(),{'url':before['url']})()
    scraper.get_cdp_endpoint=lambda: 'http://127.0.0.1:1272'
    scraper._log_stage=lambda *a:None;scraper._set_request_state=lambda *a:None
    with patch.object(co,'prepare_composer',side_effect=co.ComposerOperationError('UNKNOWN')):
        try:scraper._write_prompt_to_composer('x')
        except WebScraperStageError as exc:assert not exc.safe_to_retry
        else:raise AssertionError('unconfirmed')
    assert scraper._composer_io_unconfirmed
    try:scraper._reconcile_pending_submit_before_request()
    except WebScraperStageError as exc:assert exc.stage=='composer_recovery_required'
    else:raise AssertionError('unconfirmed scope reconciled as submit')
    with patch.object(co,'prepare_composer',side_effect=co.ComposerOperationError('CONNECT',{'mutation_possible':False})):
        try:scraper._write_prompt_to_composer('x')
        except WebScraperStageError as exc:assert not exc.safe_to_retry
        else:raise AssertionError('prewrite failure')
    assert not scraper._composer_io_unconfirmed
    for provider in ('gemini','claude','m365'):
        scraper.service=provider
        scraper._write_prompt_to_composer_legacy=lambda prompt,timeout: 'UNCHANGED'
        assert scraper._write_prompt_to_composer('x')=='UNCHANGED'
    ask=inspect.getsource(WebLLMScraper.ask)
    assert ask.count('not getattr(self, "_composer_io_unconfirmed", False)')>=3
    helper_source=inspect.getsource(co.helper_main)
    assert '.click(' not in helper_source and '.press(' not in helper_source
    print('COMPOSER_OPERATION_OK: strict/partial/unknown/turn-change/quarantine/timeout/cancel/cleanup')


if __name__=='__main__':run()
