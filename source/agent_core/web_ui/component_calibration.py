"""Stages 2–5: human-selected DOM plus real, submit-once component probes."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from .contracts import normalize_ui_text, ui_text_matches
from .dom_config import ELEMENT_FIELDS, VALIDATION_KEYS, load_dom_config, save_element
from .factory import create_web_ui_for_page, normalize_web_conversation_url
from .manual_adapter import prompt_url, unique_composer
from .manual_browser import open_manual_page
from .provider_registry import provider_from_url
from .message_text import read_message_text
from .tests.write_send_box import write_sample

STAGES = {2: 'send', 3: 'user_turn', 4: 'user_content', 5: 'assistant_turn',6:'assistant_content',7:'stop',9:'blocking_state'}
TITLES = {2: '2. 送出按鈕', 3: '3. 使用者訊息容器', 4: '4. 使用者訊息正文', 5: '5. 本輪模型回答容器'}
TITLES.update({6:'6. 模型回答正文',7:'7. 停止回覆按鈕',9:'9. 阻擋狀態'})
REQUIRES = {2: ('composer',), 3: ('composer','send'),
            4: ('composer','send','user_turn'), 5: ('composer','send','user_turn','user_content')}
REQUIRES.update({6:('composer','send','user_turn','user_content','assistant_turn'),
                 7:('composer','send','user_turn','user_content','assistant_turn','assistant_content'),
                 9:('composer',)})
PICKER = Path(__file__).with_name('component_picker.js').read_text(encoding='utf-8')


def make_probe(page) -> dict:
    marker = 'DOMPROBE_' + uuid.uuid4().hex[:12].upper()
    prompt = f'SmartAgent DOM calibration test {marker}. Please reply with exactly: {marker} OK'
    prompt = create_web_ui_for_page(page).prepare_transport_prompt(prompt)
    return {'marker': marker, 'prompt': prompt}


def require_config(provider: str, stage: int) -> dict:
    document = load_dom_config(provider)
    missing = [name for name in REQUIRES[stage] if name not in (document or {}).get('elements', {})]
    if missing:
        raise ValueError('請先完成前置校準：' + ', '.join(missing))
    return document


def select_component(page, stage: int, probe: dict, *, timeout: float = 900) -> dict | None:
    field = STAGES[stage]
    options = {'field':field,'marker':probe['marker'],'title':TITLES[stage], 'preflightBridge':True,
               'instructions': ('請點可用的送出按鈕。選取不會送出；按「儲存並測試」後，程式才會實際送出一則校準問題。' if stage == 2 else
                  '把使用者發送的訊息框，框選文字框，緊貼文字不含外框' if stage == 4 else
                  '把使用者發送的訊息框完整框選，選外框' if stage == 3 else
                  '把本輪模型回答的完整回答框框選，選外框，不要只選文字。只包含這一則回答，不含使用者問題或其他回答。可用擴大／縮小範圍調整。')}
    if stage>=6:options.update(keepVisibleOnCommit=True,showStateLegend=True)
    if stage == 3:
        options['instructions'] += '\n本輪問題標記：' + probe['marker']
    if stage == 4:
        options['rootSelector'] = load_dom_config(provider_from_url(page.url))['elements']['user_turn']['selector']
    if stage==6:
        options.update(rootSelector=load_dom_config(provider_from_url(page.url))['elements']['assistant_turn']['selector'],
                       instructions='點選本輪回答的完整正文，包含所有段落、清單與程式碼，不含操作列。')
    if stage==7:options['instructions']='模型正在產生較長回答。請選停止按鈕；點選只做標記，按儲存並測試後才會實際停止。若按鈕已消失，請重新執行本步。'
    if stage==9:options['instructions']='請選實際出現的錯誤／限流／登入阻擋視窗，不要選對話內容。只讀取及建立規則，不會自動重送或處理人機驗證。'
    adapter = create_web_ui_for_page(page)
    picker_options = getattr(adapter, 'calibration_picker_options', None)
    if picker_options:
        options.update(picker_options(field))
    if stage==6 and not options.get('contentRootSelector'):
        # Provider-owned content hints improve the first click; the selected
        # scope still must pass the common complete-body and ownership checks.
        hint=adapter.profile.final_content_selector
        if hint and hint!=':not(*)':options['contentRootSelector']=hint
    script = '(' + PICKER + ')(' + json.dumps(options, ensure_ascii=False) + ')'
    deadline = time.monotonic() + timeout
    checked_revision = None
    last_check = 0
    while not page.is_closed() and time.monotonic() < deadline:
        try:
            page.evaluate(script)
            if stage>=6:
                page.evaluate('()=>{const host=document.querySelector("#smartagent-dialogue-panel");if(host)host.style.display="none";}')
            state = page.evaluate('() => {const s=window.__smartAgentComponentPicker; return s ? {action:s.action,selected:s.selected,revision:s.revision}:null;}')
            if state and state['action'] == 'cancel':
                return None
            if state and state['selected'] and (state['revision'] != checked_revision or
                    state['action'] == 'save' or time.monotonic()-last_check >= 1):
                from .selection_preflight import check_component_selection
                result = check_component_selection(page, stage, probe, state['selected'])
                applied = page.evaluate('data => window.__smartAgentComponentPicker.applyPreflight(data.result,data.revision)',
                                       {'result':result,'revision':state['revision']})
                checked_revision, last_check = state['revision'], time.monotonic()
                if applied and result['passed'] and state['action'] == 'save':
                    committed = page.evaluate('revision => window.__smartAgentComponentPicker.commitSelection(revision)',state['revision'])
                    if committed:
                        return {**state['selected'], 'preflight':result}
            page.wait_for_timeout(150)
        except Exception as exc:
            if page.is_closed():
                return None
            if 'Execution context was destroyed' not in str(exc):
                raise
    if page.is_closed():
        return None
    raise TimeoutError('component_selection_timeout')


def send_control(page, selector: str):
    boxes = page.locator(selector)
    visible = [boxes.nth(i) for i in range(boxes.count()) if boxes.nth(i).is_visible()]
    if len(visible) != 1 or not visible[0].is_enabled():
        raise ValueError('send_control_not_unique_enabled')
    button = visible[0]
    if not button.evaluate('el => el.matches("button,[role=button],input[type=submit]")'):
        raise ValueError('selected_element_is_not_button')
    return button


def marker_texts(page, probe: dict) -> list[str]:
    composer = load_dom_config(provider_from_url(page.url))['elements']['composer']['selector']
    return page.evaluate('''({marker,composer}) => {
      const input=document.querySelector(composer), values=new Set();
      const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      while(walker.nextNode()) {
        const n=walker.currentNode;
        if(!n.textContent.includes(marker)||!n.parentElement||input?.contains(n)||n.parentElement.closest('script,style,#smartagent-component-picker,#smartagent-composer-picker'))continue;
        let el=n.parentElement;
        for(let i=0;el&&i<3;i++,el=el.parentElement) {
          if(!el.getClientRects().length)continue;
          const text=el.innerText||'';if(text.length<5000)values.add(text);
        }
      }return [...values];
    }''', {'marker':probe['marker'],'composer':composer})


def clear_probe(page, prompt: str) -> None:
    adapter = create_web_ui_for_page(page)
    if not adapter.validate_composer(prompt).matched:
        raise ValueError('test_draft_changed_do_not_clear')
    box = unique_composer(page, adapter.profile.composer_selector)
    box.click(); page.keyboard.press('Control+A'); page.keyboard.press('Backspace')
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        if not adapter.composer_snapshot().semantic_text.strip():
            return
        page.wait_for_timeout(100)
    raise AssertionError('test_draft_not_cleared')


def send_probe(page, probe: dict, selector: str, *, timeout: float = 120) -> dict:
    """One click only. Ambiguous/blocked submissions are never repeated."""
    adapter = create_web_ui_for_page(page)
    if not adapter.validate_composer(probe['prompt']).matched:
        raise AssertionError('probe_not_in_composer')
    print('[componentAdapter] 等待送出按鈕可用…',flush=True)
    ready_deadline=time.monotonic()+min(timeout,60)
    while True:
        try:
            control=send_control(page,selector)
            break
        except ValueError:
            if time.monotonic()>=ready_deadline:
                raise TimeoutError('send_control_not_ready:no_submit_attempted')
            page.wait_for_timeout(150)
    if not adapter.validate_composer(probe['prompt']).matched:
        raise AssertionError('probe_changed_before_submit')
    page.evaluate('''selector => {
      window.__smartAgentSendStates=[];
      const observe=()=>{
        const xs=[...document.querySelectorAll(selector)].filter(e=>e.getClientRects().length);
        const state={visible:xs.length===1,enabled:xs.length===1&&!xs[0].disabled&&xs[0].getAttribute('aria-disabled')!=='true'};
        const log=window.__smartAgentSendStates, last=log[log.length-1];
        if(log.length<100&&(!last||last.visible!==state.visible||last.enabled!==state.enabled))log.push(state);
      };
      observe();new MutationObserver(observe).observe(document.body,{subtree:true,attributes:true,childList:true});
    }''',selector)
    control.click(timeout=10000)
    deadline = time.monotonic()+timeout
    accepted = response = False
    while time.monotonic()<deadline:
        texts = marker_texts(page, probe)
        accepted = accepted or any(normalize_ui_text(probe['prompt']) in normalize_ui_text(text) for text in texts)
        response = response or any(probe['marker'] in text and 'SmartAgent DOM calibration' not in text and '[WEBUI_SUBMIT_ID]' not in text for text in texts)
        empty = not adapter.composer_snapshot().semantic_text.strip()
        if accepted and response and empty:
            states = page.evaluate('() => window.__smartAgentSendStates || []')
            return {'submitted_once':True,'user_accepted':True,'response_marker_observed':True,
                    'control_states':states,'busy_control_observed':any(not x['visible'] or not x['enabled'] for x in states)}
        page.wait_for_timeout(150)
    raise TimeoutError('submit_unconfirmed_or_response_missing:no_automatic_resubmit')


def prepare_probe(page, probe: dict) -> dict:
    return write_sample(page, text=probe['prompt'], replace_known_sample=True)


def user_root(page, selector: str, probe: dict):
    candidates = page.locator(selector)
    matches = []
    for index in range(candidates.count()):
        item = candidates.nth(index)
        if not item.is_visible():
            continue
        text = read_message_text(item)
        if probe['marker'] not in text:
            continue
        if normalize_ui_text(probe['prompt']) not in normalize_ui_text(text):
            raise ValueError('user_selector_also_matches_assistant')
        if text.count(probe['marker']) != probe['prompt'].count(probe['marker']):
            raise ValueError('user_container_includes_other_turns')
        matches.append(item)
    if len(matches) != 1:
        raise ValueError('new_user_turn_not_unique')
    return matches[0]


def content_text(root, selector: str) -> str:
    if selector == ':scope':
        return read_message_text(root) if root.is_visible() else ''
    nodes = root.element_handle().query_selector_all(selector)
    texts = [read_message_text(node) for node in nodes if node.is_visible()]
    return max(texts,key=len) if texts else ''


def candidate_adapter(page, field: str, selector: str):
    adapter = create_web_ui_for_page(page)
    adapter.profile = replace(adapter.profile, **{ELEMENT_FIELDS[field]:(selector,) if field=='stop' else selector})
    return adapter


def calibrate_stage(page, stage: int, evidence: Path, *, provider: str = '', no_wait: bool = False) -> dict | None:
    provider = provider or provider_from_url(page.url)
    document = require_config(provider, stage)
    composer = document['elements']['composer']['selector']
    page.locator(composer).wait_for(state='visible', timeout=180000)
    first = make_probe(page)
    report = {'stage':stage,'provider':provider,'passed':False,'probe_markers':[first['marker']],
              'full_conversation':False}
    prepare_probe(page, first)
    saved_send = document['elements'].get('send',{}).get('selector','')
    if stage != 2:
        report['initial_submit'] = send_probe(page, first, saved_send)
    selection = select_component(page, stage, first)
    if selection is None:
        return None
    field, selector = STAGES[stage], selection['selector']
    report['selection_preflight'] = selection.get('preflight')
    (evidence/'selection.json').write_text(json.dumps({'stage':stage,'selection':selection,'probe':first},ensure_ascii=False,indent=2),encoding='utf-8')
    page.screenshot(path=str(evidence/'01_selected.png'))
    validation = {VALIDATION_KEYS[field]:True}
    if stage == 2:
        report['submit'] = send_probe(page, first, selector)
        # Empty idle composers can show a voice control instead of Send. Fill a
        # fresh draft to prove the selected send button becomes usable again.
        next_draft = make_probe(page)
        prepare_probe(page,next_draft)
        deadline = time.monotonic()+60
        while True:
            try:
                send_control(page,selector); break
            except ValueError:
                if time.monotonic()>=deadline: raise TimeoutError('send_control_did_not_return')
                page.wait_for_timeout(150)
        report['send_returned_with_draft'] = True
        runtime_button=candidate_adapter(page,'send',selector).send_control()
        if runtime_button is None or not runtime_button.evaluate('(el,selector)=>el.matches(selector)',selector):
            raise AssertionError('runtime_send_control_does_not_match_selection')
        report['runtime_send_control_verified']=True
        clear_probe(page,next_draft['prompt'])
        validation.update(click_verified=True,send_returned_with_draft=True)
    else:
        user_selector = selector if stage == 3 else document['elements']['user_turn']['selector']
        root = user_root(page,user_selector,first)
        if stage == 4 and not ui_text_matches(content_text(root,selector),first['prompt']):
            raise ValueError('selected_user_content_does_not_match_full_prompt')
        adapter = candidate_adapter(page,field,selector)
        if stage == 5:
            assistant_turns = adapter.turns('assistant')
            initial = [t for t in assistant_turns if first['marker'] in t.raw_text and 'SmartAgent DOM calibration' not in t.raw_text]
            diagnostic = {'selector':selector, 'selector_match_count':page.locator(selector).count(),
                          'probe_response_count':len(initial), 'turn_count':len(assistant_turns),
                          'turns':[{'contains_probe':first['marker'] in t.raw_text,
                                    'contains_prompt':'SmartAgent DOM calibration' in t.raw_text,
                                    'attributes':t.stable_attributes} for t in assistant_turns[:100]]}
            (evidence/'assistant_selection_diagnostic.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
            if len(initial)!=1: raise ValueError('selected_assistant_does_not_match_probe_response')
            if not initial[0].stable_attributes:
                raise ValueError('選取範圍沒有可用的訊息識別資訊；請選模型回答容器。若已選外框，可能是網站識別欄位尚未支援。')
        second = make_probe(page)
        scope = adapter.capture_request(second['prompt']) if stage == 5 else None
        prepare_probe(page,second)
        report['second_submit'] = send_probe(page,second,saved_send)
        second_root = user_root(page,user_selector,second)
        report['probe_markers'].append(second['marker'])
        if stage == 3:
            turns = adapter.turns('user')
            first_turns=[turn for turn in turns if ui_text_matches(turn.raw_text,first['prompt'])]
            second_turns=[turn for turn in turns if ui_text_matches(turn.raw_text,second['prompt'])]
            if len(first_turns)!=1 or len(second_turns)!=1 or first_turns[0].structural_id==second_turns[0].structural_id:
                raise AssertionError('runtime_requires_two_distinct_user_turns')
            report['repeated_turn_verified'] = True
            report['distinct_user_turn_ids']=[first_turns[0].structural_id,second_turns[0].structural_id]
        elif stage == 4:
            if not ui_text_matches(content_text(second_root,selector),second['prompt']):
                raise AssertionError('relative_content_selector_not_reusable')
            turns = adapter.turns('user')
            if not any(ui_text_matches(turn.raw_text,second['prompt']) for turn in turns):
                raise AssertionError('runtime_user_content_roundtrip_failed')
            report['relative_content_verified'] = True
        else:
            ownership_deadline=time.monotonic()+30
            owned=None
            while time.monotonic()<ownership_deadline:
                adapter.confirm_user_turn(scope)
                owned=adapter.latest_owned_assistant(scope)
                if owned is not None and second['marker'] in owned.raw_text and first['marker'] not in owned.raw_text:
                    break
                page.wait_for_timeout(150)
            else:
                diagnostic={'selector':selector,'scope_prompt':scope.prompt_text,'request_markers':scope.request_markers,
                            'baseline_assistant_ids':scope.baseline_assistant_ids,
                            'baseline_assistant_content':scope.baseline_assistant_content,
                            'user_bound':scope.user_turn is not None,'users':[],'assistants':[]}
                for turn in adapter.turns('user'):
                    diagnostic['users'].append({'ordinal':turn.ordinal,'attrs':turn.stable_attributes,
                       'second_marker':second['marker'] in turn.raw_text,'length':len(turn.raw_text),
                       'request_nonce_present':any(marker in turn.raw_text for marker in scope.request_markers)})
                for turn in adapter.turns('assistant'):
                    diagnostic['assistants'].append({'ordinal':turn.ordinal,'attrs':turn.stable_attributes,
                        'unit':adapter.conversation_unit_id(turn),'durable_id':adapter.durable_turn_id(turn),
                        'fresh':adapter.assistant_is_fresh(turn,scope),'second_marker':second['marker'] in turn.raw_text,
                        'after_user':bool(scope.user_turn and adapter._is_after(scope.user_turn,turn))})
                (evidence/'ownership_failure.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
                raise AssertionError('runtime_latest_owned_assistant_not_confirmed')
            report.update(newest_assistant_owned=True,old_response_rejected=True,
                          baseline_assistant_count=scope.baseline_assistant_count,
                          owned_assistant_id=adapter.durable_turn_id(owned))
        validation['distinct_probe_markers_verified'] = True
    path = save_element(provider,page.url,field,selector,selection,validation)
    runtime = create_web_ui_for_page(page)
    if getattr(runtime.profile,ELEMENT_FIELDS[field]) != selector:
        raise AssertionError('saved_runtime_selector_not_loaded')
    report.update(passed=True,selector=selector,config_path=str(path),runtime_config_loaded=True,
                  same_page=True,conversation_url=page.url,blank_pages_in_context=sum(p.url=='about:blank' for p in page.context.pages))
    print('[componentAdapter] '+json.dumps(report,ensure_ascii=False),flush=True)
    page.evaluate('message => window.__smartAgentComponentPicker.finish(message)', '校準與功能驗證成功。按「關閉」結束。')
    page.screenshot(path=str(evidence/'02_verified.png'))
    if not no_wait:
        while not page.is_closed():
            try:
                if page.evaluate('() => window.__smartAgentComponentPicker.action')=='cancel': break
                page.wait_for_timeout(200)
            except Exception:
                if page.is_closed(): break
                raise
    return report


def run(url: str, stage: int, *, endpoint: str = '', no_wait: bool = False) -> dict | None:
    from ..paths import log_root
    url = normalize_web_conversation_url(url)
    require_config(provider_from_url(url),stage)
    evidence = log_root()/'manual_dom_it'/('stage'+str(stage)+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    evidence.mkdir(parents=True,exist_ok=True)
    result = {'passed':False,'stage':stage}
    try:
        with open_manual_page(url,endpoint=endpoint) as page:
            try:
                result = calibrate_stage(page,stage,evidence,provider=provider_from_url(url),no_wait=no_wait)
            except Exception:
                if not page.is_closed(): page.screenshot(path=str(evidence/'failure.png'))
                raise
        return result
    except Exception as exc:
        result = {'passed':False,'stage':stage,'error':f'{type(exc).__name__}: {exc}'}
        raise
    finally:
        (evidence/'result.json').write_text(json.dumps(result or {'cancelled':True,'stage':stage},ensure_ascii=False,indent=2),encoding='utf-8')
        print('[componentAdapter] EVIDENCE='+str(evidence),flush=True)


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description='DOM 元件點選、校準與實際送出驗證')
    parser.add_argument('--stage',type=int,choices=range(2,6),required=True)
    parser.add_argument('--url',default='')
    parser.add_argument('--cdp-endpoint',default='')
    parser.add_argument('--no-wait',action='store_true')
    args=parser.parse_args(argv)
    try:
        url=args.url or prompt_url(TITLES[args.stage]+' — 校準與驗證')
        result=run(url,args.stage,endpoint=args.cdp_endpoint,no_wait=args.no_wait)
        print('[componentAdapter] RESULT='+('CALIBRATED_AND_VERIFIED' if result else 'CANCELLED'))
        return 0
    except (InterruptedError,KeyboardInterrupt):
        print('[componentAdapter] RESULT=CANCELLED');return 0
    except Exception as exc:
        print(f'[componentAdapter] FAILED: {type(exc).__name__}: {exc}');return 2


if __name__=='__main__':
    raise SystemExit(main())
