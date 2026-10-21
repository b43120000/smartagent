"""Manual attachment stages11–15. Never upload anything except owned test fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone

from .manual_browser import open_manual_page
from .manual_adapter import prompt_url
from .factory import create_web_ui_for_page
from .file_config import load_files, save_files
from .file_runtime import unique, wait_attachment, download_owned
from .attachment_probe import install as install_attachment_probe, input_files, track_chip
from .dialogue_calibration import begin_request, update_panel
from .dialogue_state import wait_owned_response
from .dom_config import load_dom_config

NAMES = {11:'attachmentEntry',12:'fileUpload',13:'attachmentState',14:'downloadArtifact',15:'fileRoundtripIT'}
TITLES = {11:'11. 附件上傳入口',12:'12. 選檔與上傳',13:'13. 附件完成狀態',14:'14. 回答檔案下載',15:'15. 上傳下載完整驗證'}
HELP = {11:'點選輸入框旁的附件／加號按鈕。儲存後只開啟附件選單，不送出訊息。',
        12:'附件選單開啟後，點選「從電腦上傳」項目。儲存後由程式選擇自己產生的無敏感小型文字檔，不需手動選檔。',
        13:'框選目前輸入框中的單一測試附件卡片，需包含檔名及移除按鈕，不要選歷史訊息的附件。儲存後檢查上傳進度、錯誤與送出就緒。',
        14:'先準備一輪測試附件與檔案回覆，完成後點選本輪模型產生檔案的下載連結／按鈕。程式會實際下載並比對內容。',
        15:'載入11～14設定，送出一次自行產生的測試附件，確認模型讀取附件並產生檔案，再下載比對；不會自動重送。'}
PICKER = Path(__file__).with_name('file_picker.js').read_text(encoding='utf-8')


def show(page, stage):
    adapter=create_web_ui_for_page(page)
    page.evaluate(PICKER,dict(title=TITLES[stage],help=HELP[stage],stage=stage,
         userRoot=adapter.profile.user_turn_selector,assistantRoot=adapter.profile.assistant_turn_selector))


def status(page, phase, message):
    page.evaluate('(d)=>window.__smartAgentFilePicker?.update(d.phase,d.message)',dict(phase=phase,message=message))
    if phase=='preflight':
        page.evaluate('()=>{const root=document.querySelector("#smartagent-file-picker")?.shadowRoot;for(const id of ["pick","expand","shrink","save"]){const b=root?.querySelector("#"+id);if(b)b.disabled=true;}}')


def select(page, stage):
    show(page,stage)
    page.evaluate("()=>window.__smartAgentFilePicker.selectionReady=true")
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        value=page.evaluate('()=>({action:window.__smartAgentFilePicker?.action,selection:window.__smartAgentFilePicker?.selection})')
        if value['action']=='cancel':raise InterruptedError('calibration_cancelled')
        if value['action']=='save':return value['selection']['selector']
        page.wait_for_timeout(150)
    raise TimeoutError('manual_selection_timeout')


def fixture(folder):
    folder.mkdir(parents=True,exist_ok=True)
    # Alphanumeric payload avoids model Markdown escaping in native text files.
    marker='DOMFILE'+uuid.uuid4().hex[:12].upper()
    path=folder/('attachment_'+uuid.uuid4().hex[:8]+'.txt')
    path.write_bytes((marker+'\n').encode('utf-8'))
    return path,marker


def selector_for(element):
    """Return a safe, unique file-input locator; MIME accept strings are not selectors."""
    page=element.owner_frame().page
    candidates=[]
    if page.locator('input[type=file]').count()==1:
        candidates.append('input[type=file]')
    tag=element.evaluate('el=>el.tagName.toLowerCase()')
    for attr in ('data-testid','name','aria-label','id'):
        value=element.get_attribute(attr)
        if attr=='id' and value and value.startswith('_r_'):
            continue
        if value:
            candidates.append(tag+'['+attr+'='+json.dumps(value,ensure_ascii=False)+']')
    from .profile_store import _validate_selector
    for candidate in candidates:
        try:
            _validate_selector(candidate,field='file_input')
            if page.locator(candidate).count()==1:
                return candidate
        except ValueError:
            continue
    raise ValueError('file_input_selector_not_safe_and_unique')


def open_entry(adapter):
    config=load_files(adapter.provider_name)
    if not config.get('entry_verified'):raise ValueError('calibrate_stage11_first')
    adapter.page.locator(config['entry']).first.wait_for(state='visible',timeout=20000)
    unique(adapter.page,config['entry']).click(timeout=10000)


def choose_file(adapter,path):
    page=adapter.page;config=load_files(adapter.provider_name)
    # Each upload gets a fresh event capture; frameworks may clear FileList immediately.
    install_attachment_probe(page,config['input'])
    inputs=page.locator(config['input'])
    if inputs.count()!=1:
        open_entry(adapter)
        if config.get('upload_action'):
            with page.expect_file_chooser(timeout=10000) as pending:
                unique(page,config['upload_action']).click(timeout=10000)
            pending.value.set_files(str(path))
            return
    unique(page,config['input'],visible=False).set_input_files(str(path))


def ensure_clean(adapter):
    if adapter.composer_snapshot().primary_text.strip() or adapter.composer_attachment_count():
        raise ValueError('existing_draft_or_attachment_preserved:請先處理原有草稿與附件')
    if adapter.generation_active():raise ValueError('previous_response_still_generating')


def bootstrap_attachment_baseline(adapter):
    """Prove an empty, active composer without relying on optional native parsers."""
    if adapter.composer_snapshot().primary_text.strip():
        raise ValueError('existing_draft_or_attachment_preserved:請先處理原有草稿與附件')
    if adapter.generation_active():raise ValueError('previous_response_still_generating')
    config=load_files(adapter.provider_name)
    install_attachment_probe(adapter.page,config.get('input', 'input[type=file]'))
    baseline=input_files(adapter.page)
    if baseline:
        raise ValueError('existing_draft_or_attachment_preserved:請先處理原有草稿與附件')
    removable=adapter.page.evaluate("""()=>Array.from(document.querySelectorAll('button[aria-label*="Remove" i],button[aria-label*="Delete" i],button[aria-label*="移除"],button[aria-label*="刪除"],[data-testid*="remove"]')).filter(n=>!n.closest('article,[data-message-author-role]')).map(n=>(n.closest('div,li,section')?.innerText||'').trim()).filter(Boolean)""")
    if removable:
        raise ValueError('existing_draft_or_attachment_preserved:removable_attachment_inventory')
    native=adapter.attachments.attachment_dom_state(adapter.page,[]) if adapter.attachments is not None else None
    if native and native.get('supported') is not False and adapter.attachments.composer_attachment_count(adapter.page):
        raise ValueError('existing_draft_or_attachment_preserved:native_attachment_inventory')
    return dict(draft='', input_files=baseline)


def wait_file_reply(adapter,scope,marker,*,stage=14,timeout=180):
    """Recover a native reply-render failure with bounded reads, never another submit."""
    page=adapter.page;url=page.url;deadline=time.monotonic()+timeout;reloads=0;last_reload=0
    original_user_id=adapter.durable_turn_id(scope.user_turn) if scope.user_turn else ''
    load_errors={'Unable to load this response','\u7121\u6cd5\u8f09\u5165\u9019\u5247\u56de\u8986'}
    while time.monotonic()<deadline:
        try:
            turn,text,samples=wait_owned_response(adapter,scope,marker,quiet_seconds=5,timeout=min(12,max(1,deadline-time.monotonic())))
            return turn,text,samples,reloads
        except TimeoutError:
            if time.monotonic()>=deadline:raise
        turn=adapter.latest_owned_assistant(scope)
        lines={line.strip() for line in (turn.raw_text if turn else '').splitlines()}
        if reloads<2 and lines.intersection(load_errors) and (not reloads or time.monotonic()-last_reload>=45):
            if page.url!=url or not original_user_id:
                raise ValueError('file_reply_reload_requires_bound_same_conversation')
            reloads+=1;last_reload=time.monotonic()
            status(page,'preflight','Reply rendering failed; restoring this owned page ('+str(reloads)+'/2). No resubmit.')
            if reloads==1:
                page.reload(wait_until='domcontentloaded',timeout=45000)
            else:
                page.goto(url,wait_until='domcontentloaded',timeout=45000)
            page.locator(adapter.profile.composer_selector).first.wait_for(state='visible',timeout=30000)
            nonces=[value for value in scope.request_markers if value.startswith('WUS-')]
            if nonces:
                page.wait_for_function('''d=>[...document.querySelectorAll(d.selector)].filter(e=>d.nonces.every(n=>(e.innerText||'').includes(n))).length===1''',arg=dict(selector=adapter.profile.user_turn_selector,nonces=nonces),timeout=30000)
            rebound=adapter.rebind_user_turn(scope)
            restored_id=adapter.durable_turn_id(rebound) if rebound is not None else ''
            # Search-renderer DOM IDs can change after reload. The original
            # per-submit nonce remains an exact, unique logical user anchor.
            nonce_matches=[turn for turn in adapter.turns('user') if nonces and all(n in turn.raw_text for n in nonces)]
            nonce_bound=bool(rebound is not None and len(nonce_matches)==1 and all(n in rebound.raw_text for n in nonces))
            if page.url!=url or rebound is None or not (restored_id==original_user_id or nonce_bound):
                before_hash=hashlib.sha256(original_user_id.encode()).hexdigest()[:12]
                after_hash=hashlib.sha256(restored_id.encode()).hexdigest()[:12]
                raise ValueError('file_reply_reload_user_identity_changed:'+before_hash+':'+after_hash)
            show(page,stage)
            status(page,'preflight','Same request restored; checking the response marker and downloaded bytes. No resubmit.')
    raise TimeoutError('owned_file_response_not_ready:no_automatic_resubmit')


def require(provider,*fields):
    config=load_files(provider)
    if not all(config.get(field) for field in fields):raise ValueError('calibrate_required_file_stages_first:'+','.join(fields))
    return config


def run_stage(page,stage,folder):
    adapter=create_web_ui_for_page(page);provider=adapter.provider_name
    if stage==11:
        selector=select(page,stage);control=unique(page,selector)
        if control.evaluate('el=>!el.matches("button,[role=button]")'):raise ValueError('attachment_entry_must_be_button')
        # File chooser events are observed rather than opening an unmanaged OS dialog.
        events=[]
        listener=lambda chooser:events.append(chooser)
        page.on('filechooser',listener)
        before_inputs=page.locator('input[type=file]').count()
        before_visible_menus=page.locator('[role=menu]:visible,[role=menuitem]:visible').count()
        before_expanded=control.get_attribute('aria-expanded')
        before_state=control.get_attribute('data-state')
        try:
            control.click(timeout=10000)
            deadline=time.monotonic()+3
            expanded=False
            while time.monotonic()<deadline:
                expanded=((before_expanded in (None,'false') and control.get_attribute('aria-expanded')=='true') or
                          (before_state in (None,'closed') and control.get_attribute('data-state')=='open'))
                if expanded:break
                page.wait_for_timeout(100)
            revealed=bool(events or page.locator('input[type=file]').count()>before_inputs or
                          page.locator('[role=menu]:visible,[role=menuitem]:visible').count()>before_visible_menus or expanded)
            if not revealed:raise ValueError('attachment_entry_did_not_reveal_upload_ui')
        finally:
            page.remove_listener('filechooser',listener)
        save_files(provider,dict(entry=selector,entry_verified=True))
        page.keyboard.press('Escape')
        return dict(entry_verified=True,selector=selector)
    if stage==12:
        # Recalibrating upload must not depend on stage 13's chip parser:
        # stage 12 supplies the input locator needed to calibrate that parser.
        # Prove an empty baseline from factual file inputs/removal controls.
        bootstrap_attachment_baseline(adapter);open_entry(adapter)
        selector=select(page,stage);control=unique(page,selector)
        path,marker=fixture(folder)
        with page.expect_file_chooser(timeout=10000) as pending:
            control.click(timeout=10000)
        chooser=pending.value;input_selector=selector_for(chooser.element)
        unique(page,input_selector,visible=False)
        chooser.set_files(str(path));page.wait_for_timeout(500)
        save_files(provider,dict(upload_action=selector,input=input_selector,upload_verified=True))
        return dict(upload_verified=True,filename=path.name,marker=marker,input_selector=input_selector,
                    note='附件完成狀態需接續第13步驗證')
    if stage==13:
        require(provider,'upload_verified')
        # Native inventory is optional.  This scoped empty baseline never clears a draft or file.
        baseline=bootstrap_attachment_baseline(adapter);path,marker=fixture(folder);choose_file(adapter,path)
        from .attachment_probe import selected_files
        selected=selected_files(page)
        actual=dict(name=path.name,size=path.stat().st_size,type='text/plain',lastModified=int(path.stat().st_mtime*1000))
        if len(selected)!=1 or any(selected[0].get(key)!=value for key,value in actual.items()):
            raise ValueError('attachment_file_selection_not_observed')
        selector=select(page,stage);chip=unique(page,selector)
        descriptor=chip.inner_text()+' '+(chip.get_attribute('aria-label') or '')+' '+(chip.get_attribute('title') or '')
        if path.name not in descriptor:raise ValueError('selected_chip_does_not_contain_test_filename')
        remove='button[aria-label*="Remove" i],button[aria-label*="Delete" i],button[aria-label*="移除"],button[aria-label*="刪除"],button[data-testid*="remove"]'
        # Providers commonly hide the remove action while a file is uploading.
        # Its existence is structural evidence; visibility is needed only to clean up.
        unique(chip,remove,visible=False)
        settings=dict(chip=selector,busy='[aria-busy="true"],[role="progressbar"],[data-state="uploading"],[data-state="processing"]',
                      error='[role="alert"]',remove=remove,
                      attachment_probe={'completion':'busy_idle','file':actual},attachment_inventory_verified=True,attachment_verified=True)
        track_chip(page,chip,settings['busy'])
        # Candidate checks run before saving; failed selection never replaces runtime config.
        candidate={**load_files(provider),**settings}
        state=wait_attachment(adapter,path.name,config=candidate)
        send=adapter.send_control()
        if not send or not send.is_visible() or not send.is_enabled():raise ValueError('attachment_send_not_ready')
        chip.hover(timeout=10000)
        unique(chip,remove).click(timeout=10000)
        chip.wait_for(state='hidden',timeout=10000)
        if input_files(page)!=baseline['input_files'] or adapter.composer_snapshot().primary_text.strip()!=baseline['draft']:
            raise ValueError('attachment_cleanup_did_not_restore_baseline')
        save_files(provider,settings)
        return dict(attachment_verified=True,filename=path.name,ready=True,automatic_resubmit=False,
                    attachment_inventory_verified=True)
    require(provider,'entry_verified','upload_verified','attachment_verified')
    ensure_clean(adapter);path,marker=fixture(folder);choose_file(adapter,path);wait_attachment(adapter,path.name)
    show(page,stage);status(page,'preflight','附件已就緒；正在送出一次測試，確認模型讀取附件並產生可下載檔案。')
    raw=('Read the attached text file. Reply with its exact first line, then create a downloadable plain text file named result.txt'
         ' containing exactly that first line and a newline. Use your native file creation tool. Do not show code or a data URL. No explanation.')
    # Do not expose marker in prompt: proving the answer knows it proves file reading.
    prompt=adapter.prepare_transport_prompt(raw)
    probe=dict(prompt=prompt,marker=marker)
    # The shared context can contain the Agent input bridge. Only this owned
    # calibration page bypasses interception during the single software submit.
    adapter.set_automation_submit(True)
    try:
        scope=begin_request(page,probe)
    finally:
        adapter.set_automation_submit(False)
    turn,text,_samples,reply_reloads=wait_file_reply(adapter,scope,marker,stage=stage,timeout=180)
    if marker not in text:raise AssertionError('model_did_not_read_attachment')
    if stage==14:
        selector=select(page,stage)
        # Candidate download mapping remains relative to the owned fresh answer.
        candidate={**load_files(provider), 'download':selector,'download_verified':True}
        result=download_owned(adapter,scope,folder/(marker+'_download.txt'),config=candidate)
    else:
        require(provider,'download_verified')
        result=download_owned(adapter,scope,folder/(marker+'_download.txt'))
    actual=Path(result['path']).read_text(encoding='utf-8-sig').replace('\r\n','\n')
    if actual!=marker+'\n':raise AssertionError('download_content_mismatch')
    if stage==14:save_files(provider,dict(download=selector,download_verified=True))
    else:save_files(provider,dict(roundtrip=True))
    return dict(model_read_attachment=True,download_content_verified=True,submitted_once=True,automatic_resubmit=False,reply_reload_count=reply_reloads,download=result)


def run(stage,url,*,endpoint='',no_wait=False):
    from ..paths import log_root, local_data_root
    evidence=log_root()/'manual_dom_it'/('stage'+str(stage)+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    evidence.mkdir(parents=True,exist_ok=True)
    folder=local_data_root()/'manual_dom_file_it'/evidence.name
    result=dict(stage=stage,passed=False)
    with open_manual_page(url,endpoint=endpoint,navigation_wait='commit') as page:
        try:
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if page.evaluate('()=>window.__smartAgentFilePicker?.action==="cancel"'):
                    raise InterruptedError('calibration_cancelled')
                try:
                    if not page.locator('#smartagent-file-picker').count():
                        show(page,stage)
                        page.evaluate('()=>{for(const b of document.querySelector("#smartagent-file-picker").shadowRoot.querySelectorAll("button"))if(b.id!=="cancel")b.disabled=true;}')
                        status(page,'waiting','正在等待輸入框就緒；登入或頁面載入完成後會開啟選取操作。')
                    adapter=create_web_ui_for_page(page)
                    if adapter.visible_composer() and adapter.visible_composer().is_visible():break
                except Exception:pass
                page.wait_for_timeout(250)
            else:raise TimeoutError('composer_not_ready:請先登入')
            result.update(provider=adapter.provider_name,**run_stage(page,stage,folder))
            result['passed']=not result.get('preliminary_only',False)
            if result['passed']:
                page.evaluate('(m)=>window.__smartAgentFilePicker?.finish(m,true)','功能驗證通過。本輪已完成，按關閉結束。')
            else:
                status(page,'preflight',result['limitation'])
        except Exception as exc:
            result.update(error=type(exc).__name__+': '+str(exc))
            if not page.is_closed():
                page.evaluate('(m)=>window.__smartAgentFilePicker?.finish(m,false)',result['error'])
        if not page.is_closed():page.screenshot(path=str(evidence/'result.png'))
        (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('[fileAdapter] EVIDENCE='+str(evidence),flush=True)
        print('[fileAdapter] '+json.dumps(result,ensure_ascii=False),flush=True)
        if not no_wait:
            while not page.is_closed() and page.evaluate('()=>window.__smartAgentFilePicker?.action!=="cancel"'):
                page.wait_for_timeout(200)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage',type=int,required=True,choices=range(11,16));parser.add_argument('--url',default='');parser.add_argument('--cdp-endpoint',default='');parser.add_argument('--no-wait',action='store_true')
    args=parser.parse_args();url=args.url or prompt_url(TITLES[args.stage]+' — 目標網頁 URL')
    result=run(args.stage,url,endpoint=args.cdp_endpoint,no_wait=args.no_wait)
    return 0 if result['passed'] else 2


if __name__=='__main__':raise SystemExit(main())
