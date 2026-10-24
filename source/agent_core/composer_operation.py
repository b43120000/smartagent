"""Bound one CDP composer operation; never submit or restart a browser."""
from __future__ import annotations
from agent_core import path_integrity_trace as PIT

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from .json_state_io import read_json_retry, write_json_atomic


class ComposerOperationError(RuntimeError):
    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.evidence = evidence or {}


class _WindowsJob:
    """Contain the helper before stdin is released; retained CDP Edge is external."""
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_=[('ProcessTime',c.c_int64),('JobTime',c.c_int64),('Flags',w.DWORD),
                ('Min',c.c_size_t),('Max',c.c_size_t),('Active',w.DWORD),
                ('Affinity',c.c_size_t),('Priority',w.DWORD),('Scheduling',w.DWORD)]
        class IO(c.Structure):
            _fields_=[(name,c.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
        class Extended(c.Structure):
            _fields_=[('Basic',Basic),('IO',IO),('ProcessMemory',c.c_size_t),
                ('JobMemory',c.c_size_t),('PeakProcess',c.c_size_t),('PeakJob',c.c_size_t)]
        kernel=c.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        kernel.CloseHandle.argtypes=[w.HANDLE]
        handle=kernel.CreateJobObjectW(None,None)
        if not handle:
            raise c.WinError(c.get_last_error())
        info=Extended();info.Basic.Flags=0x2000 # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle,9,c.byref(info),c.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):
            error=c.get_last_error();kernel.CloseHandle(handle);raise c.WinError(error)
        self.handle=handle;self.kernel=kernel

    def resume(self, process):
        if os.name=='nt':
            import ctypes as c
            from ctypes import wintypes as w
            resume=c.WinDLL('ntdll').NtResumeProcess
            resume.argtypes=[w.HANDLE];resume.restype=w.LONG
            if resume(w.HANDLE(int(process._handle)))!=0:
                raise ComposerOperationError('composer_helper_resume_failed')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle);self.handle=None


def page_key(endpoint, url):
    path = urlsplit(url).path
    conversation = path.split('/c/', 1)[-1] if '/c/' in path else path
    return hashlib.sha256((endpoint+'\n'+conversation).encode()).hexdigest()


def logical_prompt_sha(prompt):
    body=re.sub(r'^\[WEBUI_SUBMIT_ID\]\s*WUS-[A-F0-9]{32}\s*\[/WEBUI_SUBMIT_ID\]\s*','',prompt,count=1)
    return hashlib.sha256(body.encode()).hexdigest()


def stop_owned_helper(process):
    """Only our Popen tree: helper/Playwright driver, never retained Edge."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError,subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)


def run_helper(payload, timeout_sec, cancel_check=None):
    payload = {**payload, 'connect_timeout_ms': max(500, min(12000, int((timeout_sec - 2) * 1000)))}
    trace_context = PIT.context()
    if trace_context:
        payload['path_trace_context'] = trace_context
    command=[sys.executable,'-B','-m','agent_core.composer_operation']
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name=='nt' else 0)
    deadline=time.monotonic()+timeout_sec
    first=True
    job=None
    try:
        # Child blocks on stdin before importing Playwright or touching a page.
        job=_WindowsJob(process)
        # Suspend even the venv launcher until contained: otherwise its real
        # Python child could be created before assignment and escape the job.
        job.resume(process)
        while True:
            if cancel_check:
                cancel_check()
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise subprocess.TimeoutExpired(command,timeout_sec)
            try:
                output,error=process.communicate(json.dumps(payload).encode() if first else None,
                    timeout=min(0.25,remaining))
                break
            except subprocess.TimeoutExpired:
                first=False
        lines=[line[len('COMPOSER_RESULT '):] for line in output.decode('utf-8',errors='replace').splitlines()
               if line.startswith('COMPOSER_RESULT ')]
        if process.returncode or len(lines)!=1:
            # Only expose our controlled error code, never a traceback/prompt.
            match=re.search(r'ComposerOperationError: (composer_[a-z_]+)',error.decode('utf-8',errors='replace'))
            reason=match.group(1) if match else 'unknown'
            raise ComposerOperationError('composer_helper_failed:'+reason,{'returncode':process.returncode})
        return json.loads(lines[0])
    finally:
        if job:
            job.close()
        stop_owned_helper(process)


def observe_composer(payload, timeout_sec, cancel_check=None):
    """Retry only a failed read-only CDP connection; never replay a writer."""
    for attempt in range(2):
        try:
            result = run_helper({**payload, 'mode': 'observe'}, max(15.0, timeout_sec), cancel_check)
        except subprocess.TimeoutExpired:
            return dict(status='OBSERVER_TIMEOUT', error_code='OBSERVER_TIMEOUT')
        except ComposerOperationError as exc:
            return dict(status='OBSERVER_FAILED', error_code=str(exc).split(':')[-1][:80])
        if result.get('status') not in {'CDP_CONNECT_TIMEOUT', 'CDP_CONNECTION_CLOSED'} or attempt:
            return result
        if cancel_check:
            cancel_check()
    return result


def prepare_composer(*,endpoint,url,prompt,journal_root,timeout_sec=30.0,
                     observation_timeout_sec=8.0,cancel_check=None):
    from .process_file_lock import exclusive_process_lock
    root=Path(journal_root)
    with exclusive_process_lock(root/(page_key(endpoint,url)+'.lock'),timeout_sec=1.0,label='composer'):
        return _prepare_locked(endpoint=endpoint,url=url,prompt=prompt,journal_root=root,
            timeout_sec=timeout_sec,observation_timeout_sec=observation_timeout_sec,cancel_check=cancel_check)


def _prepare_locked(*,endpoint,url,prompt,journal_root,timeout_sec,
                    observation_timeout_sec,cancel_check):
    parsed=urlsplit(endpoint)
    if parsed.scheme not in {'http','ws'} or parsed.hostname not in {'127.0.0.1','localhost','::1'}:
        raise ComposerOperationError('composer_endpoint_not_local')
    root=Path(journal_root);root.mkdir(parents=True,exist_ok=True)
    journal=root/(page_key(endpoint,url)+'.json')
    previous=read_json_retry(journal) if journal.exists() else {}
    # No mutation is issued until the child has durably saved baseline. A
    # completed exception before that point is a connection/preflight failure,
    # not an unknown draft. This permits a later explicit request after repair.
    if previous.get('reason')=='writer_exception_or_cancelled' and not previous.get('baseline'):
        previous={**previous,'state':'FAILED_PRE_WRITE'}
    if previous.get('state') in {'IN_FLIGHT','QUARANTINED','COMPLETE','SUBMIT_RESERVED','SUBMIT_ATTEMPTED'}:
        _recover_locked(endpoint=endpoint,url=url,journal=journal,record=previous,cancel_check=cancel_check,new_prompt=prompt)
    record=dict(operation_id=uuid.uuid4().hex,state='IN_FLIGHT',url=url,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),prompt_chars=len(prompt),
        prompt_bytes=len(prompt.encode()),started_at=time.time())
    marker=re.search(r'WUS-[A-F0-9]{32}',prompt)
    record['submit_id']=marker.group(0) if marker else ''
    record['owner_pid']=os.getpid()
    record['logical_prompt_sha256']=logical_prompt_sha(prompt)
    write_json_atomic(journal,record)
    payload=dict(mode='write',endpoint=endpoint,url=url,prompt=prompt,journal=str(journal),
                 operation_id=record['operation_id'])
    started=time.monotonic()
    try:
        timed_out=False
        try:
            run_helper(payload,timeout_sec,cancel_check)
        except subprocess.TimeoutExpired:
            timed_out=True
        # The writer has exited before observation begins. Never start a second
        # write; a late browser command may still have changed the draft.
        try:
            result=observe_composer(payload,observation_timeout_sec,cancel_check)
        except Exception as exc:
            result=dict(status='OBSERVER_FAILED',error_type=type(exc).__name__,error_code='observer_exception')
        if timed_out:
            result['writer_timed_out']=True
            # A browser command can outlive its CDP client; never submit after timeout.
            result['status']='WRITER_TIMEOUT'
    except BaseException as exc:
        record=read_json_retry(journal)
        record.update(state='QUARANTINED' if record.get('baseline') else 'FAILED_PRE_WRITE',
                      reason='writer_exception_or_cancelled')
        write_json_atomic(journal,record)
        if not record.get('baseline') and isinstance(exc,Exception):
            raise ComposerOperationError('composer_pre_write_failed',
                {'mutation_possible':False,'error_type':type(exc).__name__}) from exc
        raise
    # Preserve the child's durable baseline, including on timeout/cancellation.
    record=read_json_retry(journal)
    if record.get('operation_id')!=payload['operation_id']:
        raise ComposerOperationError('composer_operation_identity_changed')
    if not record.get('baseline'):
        record.update(state='FAILED_PRE_WRITE',reason=result.get('status','UNKNOWN'),result=result)
        write_json_atomic(journal,record)
        raise ComposerOperationError('composer_pre_write_failed',{'mutation_possible':False,**result})
    result['elapsed_sec']=round(time.monotonic()-started,3)
    if cancel_check:
        try:
            cancel_check()
        except BaseException:
            record.update(state='QUARANTINED',reason='cancelled_before_submit')
            write_json_atomic(journal,record)
            raise
    if result.get('status')=='VERIFIED_SAFE_TO_SUBMIT':
        result['operation_id']=record['operation_id']
        record.update(state='COMPLETE',result=result)
        write_json_atomic(journal,record)
        return result
    record.update(state='QUARANTINED',reason=result.get('status','UNKNOWN'),result=result)
    write_json_atomic(journal,record)
    raise ComposerOperationError('composer_write_unconfirmed:'+str(result.get('status','UNKNOWN')),result)


def verify_prepared_composer(*,endpoint,url,prompt,journal_root,cancel_check=None,operation_id=None):
    """Recheck pinned turn anchors after attachment/rate waits, before click."""
    from .process_file_lock import exclusive_process_lock
    with exclusive_process_lock(Path(journal_root)/(page_key(endpoint,url)+'.lock'),timeout_sec=1.0,label='composer_submit'):
        return _verify_prepared_locked(endpoint=endpoint,url=url,prompt=prompt,journal_root=journal_root,
            cancel_check=cancel_check,operation_id=operation_id)


def _verify_prepared_locked(*,endpoint,url,prompt,journal_root,cancel_check,operation_id):
    journal=Path(journal_root)/(page_key(endpoint,url)+'.json')
    record=read_json_retry(journal)
    if record.get('state')!='COMPLETE' or record.get('prompt_sha256')!=hashlib.sha256(prompt.encode()).hexdigest() or (operation_id and record.get('operation_id')!=operation_id):
        raise ComposerOperationError('composer_submit_identity_unconfirmed')
    payload=dict(mode='observe',endpoint=endpoint,url=url,prompt=prompt,journal=str(journal),operation_id=record['operation_id'])
    try:
        result=observe_composer(payload,15.0,cancel_check)
        if cancel_check:cancel_check()
        if result.get('status')!='VERIFIED_SAFE_TO_SUBMIT':
            raise ComposerOperationError('composer_submit_scope_changed',result)
        record.update(state='SUBMIT_RESERVED')
        write_json_atomic(journal,record)
        return result
    except BaseException:
        record.update(state='QUARANTINED',reason='submit_revalidation_failed')
        write_json_atomic(journal,record)
        raise


def mark_composer_submitted(*,endpoint,url,journal_root,operation_id):
    from .process_file_lock import exclusive_process_lock
    root=Path(journal_root)
    with exclusive_process_lock(root/(page_key(endpoint,url)+'.lock'),timeout_sec=1.0,label='composer_sent'):
        path=root/(page_key(endpoint,url)+'.json');record=read_json_retry(path)
        if record.get('operation_id')!=operation_id or record.get('state')!='SUBMIT_RESERVED':
            raise ComposerOperationError('composer_submit_reservation_changed')
        record['state']='SUBMIT_ATTEMPTED';write_json_atomic(path,record)


def mark_delivery_confirmed(*,endpoint,url,journal_root,operation_id):
    from .process_file_lock import exclusive_process_lock
    root=Path(journal_root)
    with exclusive_process_lock(root/(page_key(endpoint,url)+'.lock'),timeout_sec=1.0,label='composer_delivery'):
        path=root/(page_key(endpoint,url)+'.json');record=read_json_retry(path)
        if record.get('operation_id')!=operation_id or record.get('state') not in {'SUBMIT_ATTEMPTED','SUBMIT_RESERVED','DELIVERED'}:
            raise ComposerOperationError('composer_delivery_identity_changed')
        record.update(state='DELIVERED',delivery_confirmed_at=time.time())
        write_json_atomic(path,record)


def recover_composer(*,endpoint,url,journal_root,cancel_check=None,new_prompt=None):
    from .process_file_lock import exclusive_process_lock
    root=Path(journal_root)
    with exclusive_process_lock(root/(page_key(endpoint,url)+'.lock'),timeout_sec=1.0,label='composer_recovery'):
        path=root/(page_key(endpoint,url)+'.json')
        return _recover_locked(endpoint=endpoint,url=url,journal=path,record=read_json_retry(path),cancel_check=cancel_check,new_prompt=new_prompt)


def _recover_locked(*,endpoint,url,journal,record,cancel_check,new_prompt=None):
    if record.get('state') in {'IN_FLIGHT','COMPLETE','SUBMIT_RESERVED'}:
        from .process_file_lock import _pid_alive
        owner=int(record.get('owner_pid') or 0)
        if (record.get('state')=='IN_FLIGHT' and (not owner or _pid_alive(owner))) or (owner!=os.getpid() and _pid_alive(owner)):
            raise ComposerOperationError('composer_writer_still_active')
    payload=dict(mode='recover',endpoint=endpoint,url=url,prompt='',journal=str(journal),operation_id=record['operation_id'])
    result=run_helper(payload,12.0,cancel_check)
    if cancel_check:cancel_check()
    if result.get('status')=='ALREADY_DELIVERED' and result.get('observation',{}).get('composer_empty') is not True:
        raise ComposerOperationError('composer_recovery_required:DELIVERED_DRAFT_NOT_READY',result)
    if result.get('status')=='ALREADY_DELIVERED' and not result['observation']['generation_active']:
        if new_prompt is not None and record.get('logical_prompt_sha256') and logical_prompt_sha(new_prompt)!=record['logical_prompt_sha256']:
            result['status']='RECOVERED_PRIOR_DELIVERY'
    if result.get('status') not in {'RECOVERED_EMPTY','RECOVERED_OWNED_DRAFT','RECOVERED_PRIOR_DELIVERY'}:
        raise ComposerOperationError('composer_recovery_required:'+str(result.get('status','UNKNOWN')),result)
    record=read_json_retry(journal)
    record.update(state='RECOVERED',recovery=result)
    archive=journal.with_name(journal.stem+'.history');archive.mkdir(exist_ok=True)
    write_json_atomic(archive/(record['operation_id']+'.json'),record)
    write_json_atomic(journal,record)
    return result


def _page_snapshot(scraper,prompt):
    adapter=scraper._web_ui_adapter()
    users=adapter.observation_turns('user')
    assistants=adapter.observation_turns('assistant')
    return dict(composer_matches=scraper._composer_matches_prompt(prompt),
        composer_empty=not scraper._normalize_composer_text(scraper._read_composer_text()),
        url=scraper._page.url,
        user_anchors=[[t.structural_id,t.content_digest] for t in users[-2:]],
        assistant_anchors=[[t.structural_id,t.content_digest] for t in assistants[-2:]],
        user_native_ids=[_native_identity(t) for t in users[-2:]],
        assistant_native_ids=[_native_identity(t) for t in assistants[-2:]],
        user_count=len(users),assistant_count=len(assistants),generation_active=adapter.generation_active())


def _draft_evidence(scraper,candidates=None):
    candidates=scraper._read_composer_text_candidates() if candidates is None else candidates
    values=list(candidates.values()) if isinstance(candidates,dict) else list(candidates)
    return {'candidate_hashes':[hashlib.sha256(str(v).encode()).hexdigest() for v in values],
            'normalized_hashes':[hashlib.sha256(scraper._normalize_composer_text(v).encode()).hexdigest() for v in values],
            'candidate_lengths':[len(str(v)) for v in values]}


def _atomic_clear_owned(scraper,primary):
    """Compare and delete in one synchronous page task; manual edits win."""
    loc=scraper._composer_locator()
    if loc is None:return False
    return bool(loc.evaluate('''(el,expected) => {
        const actual=typeof el.value==='string'?el.value:(el.innerText||el.textContent||'');
        if(actual!==expected)return false;
        el.focus();
        if(typeof el.setSelectionRange==='function')el.setSelectionRange(0,el.value.length);
        else {const r=document.createRange();r.selectNodeContents(el);const s=window.getSelection();s.removeAllRanges();s.addRange(r);}
        return document.execCommand('delete',false,null);
    }''',primary))


def classify_observation(before,after):
    if not isinstance(before,dict) or not before or not isinstance(after,dict):
        return 'UNKNOWN'
    unchanged=_same_turns(before,after)
    if not unchanged or before.get('generation_active') or after.get('generation_active'):
        return 'TURN_CHANGED_OR_SUBMIT_UNCERTAIN'
    return 'VERIFIED_SAFE_TO_SUBMIT' if after.get('composer_matches') is True else 'PARTIAL_OR_EMPTY'


def _native_identity(turn):
    attrs=dict(turn.stable_attributes)
    return next((name+':'+str(attrs[name]) for name in ('data-message-id','data-chatgpt-selection-message-id',
        'data-chatgpt-search-message-ids','id') if attrs.get(name)),'')


def _same_turns(before,after):
    if not before or not after:return False
    if any(before.get(k)!=after.get(k) for k in ('url','user_count','assistant_count')):return False
    for role in ('user','assistant'):
        a=before.get(role+'_anchors',[]);b=after.get(role+'_anchors',[])
        if [v[1] for v in a]!=[v[1] for v in b]:return False
        old=before.get(role+'_native_ids');new=after.get(role+'_native_ids')
        if old is not None and new is not None and old!=new:return False
        # Legacy records did not store native IDs. They can migrate only via
        # equal counts+ordered content tails and two stable fresh observations.
        # Once recorded, native IDs may never change (checked above).
    return True


def _recover_page(scraper,record):
    first=_page_snapshot(scraper,'')
    time.sleep(.35)
    second=_page_snapshot(scraper,'')
    marker=str(record.get('submit_id') or '')
    users=scraper._web_ui_adapter().observation_turns('user')
    from .web_ui.base_adapter import _request_markers
    delivered=[t for t in users if (marker and marker in _request_markers(t.raw_text)) or
        hashlib.sha256(t.raw_text.encode()).hexdigest()==record.get('prompt_sha256')]
    if len(delivered)>1:
        return dict(status='DELIVERY_AMBIGUOUS',sent_count=len(delivered),observation=second)
    # Delivery proves the previous message arrived, not that the composer is
    # ready for another message. After delivery the pre-submit baseline is no
    # longer applicable, but two fresh stable observations are still required.
    if not _same_turns(first,second) or (not delivered and not _same_turns(record.get('baseline'),second)) or first['generation_active'] or second['generation_active']:
        return dict(status='SCOPE_CHANGED_OR_BUSY',observation=second)
    if not delivered and record.get('state') in {'SUBMIT_RESERVED','SUBMIT_ATTEMPTED'}:
        return dict(status='SUBMIT_OUTCOME_UNKNOWN',observation=second)
    if first['composer_empty'] and second['composer_empty']:
        return dict(status='ALREADY_DELIVERED' if delivered else 'RECOVERED_EMPTY',sent_count=len(delivered),observation=second)
    candidates=scraper._read_composer_text_candidates()
    # Prove ownership of the exact primary value we will compare-and-clear;
    # an owned alternate DOM representation cannot authorize a foreign primary.
    if not candidates:return dict(status='UNOWNED_DRAFT',observation=second)
    primary=candidates[0]
    draft=_draft_evidence(scraper,[primary])
    proof=[record.get('pending_prefix') or {},record.get('confirmed_prefix') or {}]
    owned=record.get('prompt_sha256') in draft['candidate_hashes'] or any(
        p.get('normalized_sha256') in draft['normalized_hashes'] for p in proof if p.get('normalized_sha256'))
    if not owned:return dict(status='UNOWNED_DRAFT',observation=second)
    if not _atomic_clear_owned(scraper,primary):
        return dict(status='DRAFT_CHANGED_BEFORE_CLEAR',observation=second)
    final=_page_snapshot(scraper,'')
    ready=final['composer_empty'] and _same_turns(second,final) and not final['generation_active']
    return dict(status=('ALREADY_DELIVERED' if delivered else 'RECOVERED_OWNED_DRAFT') if ready else 'CLEAR_UNCONFIRMED',
        sent_count=len(delivered),draft_cleanup='OWNED_DRAFT_CLEARED',observation=final)


def _write_staged_prompt(scraper,prompt,record,journal):
    PIT.record('09_helper_input', prompt)
    if scraper._composer_matches_prompt(prompt):return
    scraper._focus_composer_dom()
    for start in range(0,len(prompt),4096):
        if start and not scraper._composer_matches_prompt(prompt[:start]):
            raise ComposerOperationError('composer_prefix_changed')
        loc=scraper._focus_composer_dom()
        if loc is not None:
            loc.evaluate('''el => {
                el.focus();
                if (typeof el.setSelectionRange === 'function') {
                    el.setSelectionRange(el.value.length,el.value.length);
                } else {
                    const r=document.createRange();r.selectNodeContents(el);r.collapse(false);
                    const s=window.getSelection();s.removeAllRanges();s.addRange(r);
                }
            }''')
        # Synchronize ProseMirror's editor selection as well as the DOM range.
        scraper._page.keyboard.press('Control+End')
        end=min(start+4096,len(prompt));prefix=prompt[:end]
        checkpoint=dict(chars=end,sha256=hashlib.sha256(prefix.encode()).hexdigest(),
            normalized_sha256=hashlib.sha256(scraper._normalize_composer_text(prefix).encode()).hexdigest())
        record['pending_prefix']=checkpoint;write_json_atomic(journal,record)
        scraper._check_cancel_requested('composer_chunk')
        PIT.record('09_keyboard_insert_text', prompt[start:end], extra={'chunk_start': start, 'chunk_end': end})
        scraper._page.keyboard.insert_text(prompt[start:end])
        deadline=time.monotonic()+2
        while not scraper._composer_matches_prompt(prefix):
            if time.monotonic()>=deadline:raise ComposerOperationError('composer_chunk_readback_failed')
            time.sleep(.1)
        record['confirmed_prefix']=checkpoint;write_json_atomic(journal,record)
        scraper._log_stage('composer_chunk_verified',f'chars={end}/{len(prompt)}')


def helper_main():
    payload=json.loads(sys.stdin.buffer.read())
    PIT.restore(payload.get('path_trace_context'))
    # The parent owns the whole-operation deadline, including connect, DOM
    # calls and Playwright teardown. No timers run on a blocked sync thread.
    from playwright.sync_api import sync_playwright
    from .web_runtime import WebLLMScraper
    journal=Path(payload['journal']);record=read_json_retry(journal)
    if record.get('operation_id')!=payload['operation_id']:
        raise ComposerOperationError('composer_operation_identity_changed')
    with sync_playwright() as pw:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            browser=pw.chromium.connect_over_cdp(payload['endpoint'],timeout=payload.get('connect_timeout_ms',12000))
        except PlaywrightTimeoutError:
            print('COMPOSER_RESULT '+json.dumps({'status':'CDP_CONNECT_TIMEOUT','error_code':'CDP_CONNECT_TIMEOUT'}),flush=True)
            return
        except Exception as exc:
            if 'closed' not in str(exc).lower():
                raise
            print('COMPOSER_RESULT '+json.dumps({'status':'CDP_CONNECTION_CLOSED','error_code':'CDP_CONNECTION_CLOSED'}),flush=True)
            return
        pages=[page for context in browser.contexts for page in context.pages if page.url==payload['url']]
        if len(pages)!=1:
            print('COMPOSER_RESULT '+json.dumps({'status':'TARGET_NOT_UNIQUE'}),flush=True)
            return
        scraper=WebLLMScraper('chatgpt',slow_mo=0)
        scraper._page=pages[0];scraper._browser=pages[0].context
        if payload['mode']=='inspect':
            target=payload.get('prompt','')
            comparison=[]
            if target:
                for value in scraper._read_composer_text_candidates():
                    comparison.append(dict(first_diff=next((i for i,(a,b) in enumerate(zip(value,target)) if a!=b),min(len(value),len(target))),
                        first_fragment_at=value.find(target[:120]),second_fragment_at=value.find(target[4096:4216])))
            print('COMPOSER_RESULT '+json.dumps(dict(status='INSPECTED',observation=_page_snapshot(scraper,''),draft=_draft_evidence(scraper),comparison=comparison)),flush=True)
            return
        if payload['mode']=='recover':
            print('COMPOSER_RESULT '+json.dumps(_recover_page(scraper,record)),flush=True)
            return
        if payload['mode']=='clear_verified_draft':
            # Explicit cleanup only; never clear an unknown or changed draft.
            before=record.get('baseline');after=_page_snapshot(scraper,payload['prompt'])
            if record.get('state') not in {'COMPLETE','SUBMIT_RESERVED'} or classify_observation(before,after)!='VERIFIED_SAFE_TO_SUBMIT':
                raise ComposerOperationError('composer_clear_identity_unconfirmed')
            scraper._focus_composer_dom();scraper._clear_composer_keyboard()
            after=_page_snapshot(scraper,payload['prompt'])
            result=dict(status='CLEARED' if after['composer_empty'] else 'CLEAR_UNCONFIRMED',observation=after)
            if after['composer_empty']:
                record['state']='CLEARED';write_json_atomic(journal,record)
            print('COMPOSER_RESULT '+json.dumps(result),flush=True)
            return
        if payload['mode']=='write':
            before=_page_snapshot(scraper,payload['prompt'])
            record['baseline']=before;write_json_atomic(journal,record)
            if before['generation_active']:
                print('COMPOSER_RESULT '+json.dumps({'status':'GENERATION_ACTIVE'}),flush=True)
                return
            if not before['composer_empty'] and not before['composer_matches']:
                raise ComposerOperationError('composer_unrelated_draft_present')
            _write_staged_prompt(scraper,payload['prompt'],record,journal)
            after=_page_snapshot(scraper,payload['prompt'])
        else:
            before=record.get('baseline')
            after=_page_snapshot(scraper,payload['prompt'])
        status=classify_observation(before,after)
        result=dict(status=status,observation=after)
        print('COMPOSER_RESULT '+json.dumps(result),flush=True)
        # Do not browser.close/page.close: this client does not own Edge.


if __name__=='__main__':helper_main()
