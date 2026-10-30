"""Bounded writer, strict evidence, cancellation and no-replay regression."""
import inspect
import hashlib
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


def test_bounded_chunk_readback_is_strict_and_does_not_retype():
    """Deterministic 11719-char first-chunk delayed DOM and persistent mismatch."""
    from types import SimpleNamespace

    class Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class FakeScraper:
        def __init__(self, clock, *, permanent_mismatch=False):
            self.clock = clock
            self.permanent_mismatch = permanent_mismatch
            self.typed = ''
            self.writes = []
            self.logged = []
            self._last_composer_validation = None

            class Keyboard:
                def press(_, key):
                    assert key == 'Control+End'

                def insert_text(_, value):
                    self.typed += value
                    self.writes.append(value)

            self._page = SimpleNamespace(keyboard=Keyboard())

        def _composer_matches_prompt(self, expected):
            self._last_composer_validation = SimpleNamespace(
                expected_length=len(expected),
                observed_length=len(self.typed),
                expected_sha256=hashlib.sha256(expected.encode()).hexdigest(),
                observed_sha256=hashlib.sha256(self.typed.encode()).hexdigest(),
            )
            return (not self.permanent_mismatch and self.typed == expected
                    and self.clock.now >= 2.4)

        def _focus_composer_dom(self):
            return SimpleNamespace(evaluate=lambda _script: None)

        def _normalize_composer_text(self, value):
            return value

        def _check_cancel_requested(self, _stage):
            return None

        def _log_stage(self, stage, detail):
            self.logged.append((stage, detail))

    prompt = 'SENSITIVE_USER_PROMPT_' + 'x' * (11719 - len('SENSITIVE_USER_PROMPT_'))
    with tempfile.TemporaryDirectory() as folder:
        clock = Clock()
        fake = FakeScraper(clock)
        journal = Path(folder) / 'delayed.json'
        record = dict(state='IN_FLIGHT')
        with patch.object(co.time, 'monotonic', clock.monotonic), \
             patch.object(co.time, 'sleep', clock.sleep), \
             patch.object(co.PIT, 'record', return_value=None):
            co._write_staged_prompt(fake, prompt, record, journal)
        assert fake.writes == [prompt[:4096], prompt[4096:8192], prompt[8192:]]
        assert clock.now >= 2.4 and clock.now <= 8.0
        assert record['confirmed_prefix']['chars'] == 11719
        assert record['pending_prefix']['chars'] == 11719
        assert 'readback_failure' not in record
        assert 'SENSITIVE_USER_PROMPT_' not in journal.read_text(encoding='utf-8')
        assert len([e for e in fake.logged if e[0] == 'composer_chunk_verified']) == 3

    with tempfile.TemporaryDirectory() as folder:
        clock = Clock()
        fake = FakeScraper(clock, permanent_mismatch=True)
        journal = Path(folder) / 'mismatch.json'
        record = dict(state='IN_FLIGHT')
        with patch.object(co.time, 'monotonic', clock.monotonic), \
             patch.object(co.time, 'sleep', clock.sleep), \
             patch.object(co.PIT, 'record', return_value=None):
            try:
                co._write_staged_prompt(fake, prompt, record, journal)
            except co.ComposerOperationError as exc:
                assert str(exc) == 'composer_chunk_readback_failed'
                assert exc.evidence['observed_length'] == 4096
                assert exc.evidence['expected_length'] == 4096
            else:
                raise AssertionError('Unknown DOM state must fail closed')
        assert fake.writes == [prompt[:4096]], 'never automatically rewrite a chunk'
        assert clock.now >= 8.0 and clock.now <= 8.2
        assert record['pending_prefix']['chars'] == 4096
        assert 'confirmed_prefix' not in record
        diag = record['readback_failure']
        assert diag['chunk_start'] == 0 and diag['chunk_end'] == 4096
        assert diag['attempts'] >= 2
        assert diag['observed_sha256'] == hashlib.sha256(prompt[:4096].encode()).hexdigest()
        saved = journal.read_text(encoding='utf-8')
        assert 'SENSITIVE_USER_PROMPT_' not in saved
        assert 'readback_failure' in saved


def test_multiline_first_line_truncation_is_avoided_without_replay():
    """Reproduce a browser that accepts only the first line of bulk insert_text.

    Preserve V9 submit markers, Unicode, blank lines, long logical lines, and
    markdown fences; no Enter/Send, no clear and no writer replay.
    """
    from types import SimpleNamespace

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class FakeProseMirror:
        def __init__(self, *, drop_after_first_newline=False):
            self.text = ''
            self.keyboard_calls = []
            self.logged = []
            self._last_composer_validation = None
            self.drop_after_first_newline = drop_after_first_newline

            class Keyboard:
                def press(_, key):
                    self.keyboard_calls.append(('press', key))
                    assert key in ('Control+End', 'Shift+Enter')
                    if key == 'Shift+Enter':
                        self.text += '\n'

                def insert_text(_, value):
                    self.keyboard_calls.append(('insert_text', value))
                    # This recreates the actual 17-character readback issue:
                    # bulk multiline insert_text is only able to retain line 1.
                    if '\n' in value:
                        value = value.split('\n', 1)[0]
                    if self.drop_after_first_newline and '\n' in self.text:
                        return
                    self.text += value

            self._page = SimpleNamespace(keyboard=Keyboard())

        def _composer_matches_prompt(self, expected):
            expected = expected.replace('\r\n', '\n').replace('\r', '\n')
            self._last_composer_validation = SimpleNamespace(
                expected_length=len(expected), observed_length=len(self.text),
                expected_sha256=hashlib.sha256(expected.encode()).hexdigest(),
                observed_sha256=hashlib.sha256(self.text.encode()).hexdigest(),
            )
            return self.text == expected

        def _focus_composer_dom(self):
            return SimpleNamespace(evaluate=lambda script: None)

        def _normalize_composer_text(self, value):
            return value.replace('\r\n', '\n').replace('\r', '\n')

        def _check_cancel_requested(self, stage):
            assert stage == 'composer_chunk'

        def _log_stage(self, stage, detail):
            self.logged.append((stage, detail))

    header = '[WEBUI_SUBMIT_ID]\nWUS-' + 'A' * 32 + '\n[/WEBUI_SUBMIT_ID]\n'
    rest = '\n\n'.join([
        '中文相機驗證 example line ' + str(i) + ' ü ' + ('x' * 137)
        for i in range(80)
    ])
    prompt = (header + rest + '\n```smartagent_tool\n{}\n```')[:11707]
    assert len(prompt) >= 4096 and '\n' in prompt
    units = list(co._composer_write_units(prompt))
    assert co.composer_write_unit_count(prompt) == len(units)
    assert ''.join(prompt[a:b] for a,b,_,_ in units) == prompt
    assert all(b-a <= 1024 or is_break for a,b,_,is_break in units)
    assert all(('\n' not in value and '\r' not in value)
               for _,_,value,is_break in units if not is_break)
    assert all(not value for _,_,value,is_break in units if is_break)
    assert any(is_break for _,_,_,is_break in units)

    with tempfile.TemporaryDirectory(prefix='gar-composer-multiline-ok-') as tmp:
        journal = Path(tmp) / 'journal.json'
        record = {'state': 'IN_FLIGHT'}
        scraper = FakeProseMirror()
        with patch.object(co.PIT, 'record', return_value=None):
            co._write_staged_prompt(scraper, prompt, record, journal)
        assert scraper.text == prompt
        assert record['pending_prefix']['chars'] == len(prompt)
        assert record['confirmed_prefix']['chars'] == len(prompt)
        assert 'readback_failure' not in record
        assert all(key != 'Enter' for kind,key in scraper.keyboard_calls if kind == 'press')
        assert not any(key in ('Control+A', 'Backspace') for kind,key in scraper.keyboard_calls
                       if kind == 'press')
        assert any(kind == 'press' and key == 'Shift+Enter'
                   for kind,key in scraper.keyboard_calls)
        assert not any(kind == 'insert_text' and '\n' in value
                       for kind,value in scraper.keyboard_calls)
        assert '中文相機驗證' not in journal.read_text(encoding='utf-8')
        assert 'WUS-' not in journal.read_text(encoding='utf-8')
        assert len([x for x in scraper.logged if x[0] == 'composer_chunk_verified']) == len(units)

    with tempfile.TemporaryDirectory(prefix='gar-composer-multiline-fail-') as tmp:
        journal = Path(tmp) / 'journal.json'
        record = {'state': 'IN_FLIGHT'}
        scraper = FakeProseMirror(drop_after_first_newline=True)
        clock = Clock()
        with patch.object(co.time, 'monotonic', clock.monotonic), \
             patch.object(co.time, 'sleep', clock.sleep), \
             patch.object(co.PIT, 'record', return_value=None):
            try:
                co._write_staged_prompt(scraper, prompt, record, journal)
            except co.ComposerOperationError as exc:
                assert str(exc) == 'composer_chunk_readback_failed'
            else:
                raise AssertionError('Unknown draft must not pass')
        assert clock.now >= 8.0
        assert record['readback_failure']['unit_kind'] == 'text'
        assert 'confirmed_prefix' in record
        assert record['confirmed_prefix']['chars'] < len(prompt)
        assert not any(x[1] in ('Enter', 'Control+A', 'Backspace')
                       for x in scraper.keyboard_calls if x[0] == 'press')
        # The first rejected unit is attempted once; never rewrite it.
        inserted = [v for kind,v in scraper.keyboard_calls if kind == 'insert_text']
        assert len(inserted) == len(set(inserted))
        assert not any(v in journal.read_text(encoding='utf-8')
                       for v in ('WUS-' + 'A' * 32, '中文相機驗證'))

    # Preserve the existing short multiline path used by WebGPT2 L4 reviews.
    short = 'first\nsecond'
    assert list(co._composer_write_units(short)) == [(0, len(short), short, False)]


def run():
    test_bounded_chunk_readback_is_strict_and_does_not_retype()
    test_multiline_first_line_truncation_is_avoided_without_replay()
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
