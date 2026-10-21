"""Stages 6–10: answer extraction, Stop, lifecycle, blockers and complete text IT."""
import argparse
import copy
import json
import time
from datetime import datetime,timezone
from pathlib import Path

from .component_calibration import (make_probe,prepare_probe,send_control,select_component,
                                    candidate_adapter,require_config,clear_probe)
from .dialogue_state import (DEFAULT_BLOCKING_RULES,detect_blocking,arm_generation_trace,
                             finish_generation_trace,wait_owned_response)
from .dom_config import load_dom_config,save_element,save_capability,load_capability
from .factory import create_web_ui_for_page,normalize_web_conversation_url
from .manual_adapter import prompt_url
from .manual_browser import open_manual_page
from .provider_registry import provider_from_url

NAMES={6:'assistantContent',7:'stopButton',8:'generationState',9:'blockingState',10:'conversationLoopIT'}
TITLES={6:'6. 模型回答正文',7:'7. 停止回覆按鈕',8:'8. 回覆狀態驗證',9:'9. 阻擋與登入狀態',10:'10. 完整純文字對話驗證'}


PANEL_SCRIPT='''data=>{
        if(document.querySelector('#smartagent-dialogue-panel')&&window.__smartAgentDialoguePanel?.stage===data.stage)return;
        document.querySelector('#smartagent-dialogue-panel')?.remove();
        const host=document.createElement('div');host.id='smartagent-dialogue-panel';
        host.style.cssText='all:initial;position:fixed;top:16px;right:16px;z-index:2147483647';
        const ui=host.attachShadow({mode:'open'}), box=document.createElement('div');
        box.style.cssText='font:14px system-ui;color:#e6edf7;background:#142238;border:1px solid #6381a7;border-radius:12px;padding:18px;width:350px;max-width:calc(100vw - 32px);box-sizing:border-box;overflow-wrap:anywhere;box-shadow:0 5px 24px #0006';
        const title=document.createElement('h3');title.textContent=data.title;box.append(title);
        const help=document.createElement('p');help.style.whiteSpace='pre-line';help.textContent=data.help;box.append(help);
        const legend=document.createElement('p');legend.id='state-legend';legend.style.cssText='font-size:12px;line-height:1.7;white-space:pre-line';
        for(const [color,label] of [['#fca5a5','選取不符'],['#fde68a','初步檢查通過，待功能驗證'],['#93e5c0','功能驗證通過']]){
            const row=document.createElement('div');row.style.color=color;row.textContent='● '+label;legend.append(row);
        }box.append(legend);
        const status=document.createElement('p');status.id='status';status.textContent='等待開始驗證';status.style.whiteSpace='pre-line';box.append(status);
        const state=window.__smartAgentDialoguePanel={action:'',phase:'waiting',stage:data.stage};
        for(const [action,label] of [...(data.stage>=8?[['start','開始驗證']]:[]),...(data.stage===9?[['mark','標記目前阻擋視窗']]:[]),['cancel','取消']]){
            const b=document.createElement('button');b.id=action;b.textContent=label;b.style.cssText='padding:9px;margin:4px;border-radius:6px;cursor:pointer';
            b.onclick=()=>{state.action=action;if(action!=='cancel'){state.phase='checking';status.textContent='正在驗證，請稍候…';for(const x of ui.querySelectorAll('button'))if(x.id!=='cancel')x.disabled=true;}};box.append(b);
        }
        state.update=(phase,message)=>{
            state.phase=phase;
            const picker=document.querySelector('#smartagent-component-picker,#smartagent-file-picker');
            host.style.display=picker&&picker.getClientRects().length&&getComputedStyle(picker).display!=='none'?'none':'block';
            const labels={invalid:'選取不符',preflight:'初步檢查通過，待功能驗證',verified:'功能驗證通過'};
            status.style.color={invalid:'#fca5a5',preflight:'#fde68a',verified:'#93e5c0'}[phase]||'#cbd5e1';
            status.textContent=(labels[phase]?labels[phase]+'\\n':'')+message;
        };
        state.finish=(message,passed)=>{
            state.update(passed?'verified':'invalid',message);
            for(const b of ui.querySelectorAll('button'))if(b.id!=='cancel')b.disabled=true;
            ui.querySelector('#cancel').textContent='關閉';
        };
        ui.append(box);document.documentElement.append(host);
    }'''


def navigation_changed(exc):
    return any(token in str(exc) for token in ('Execution context was destroyed','Cannot find context with specified id'))


def panel(page,stage,*,wait=True):
    data={'stage':stage,'title':TITLES[stage],'help':
         '正在準備本輪測試回答。準備完成後會切換到選取面板。\n請框選模型回答的完整文字區，包含所有段落與程式碼，不含下方操作按鈕。等待期間不需自行送出訊息。' if stage==6 else
         '正在準備較長的測試回答。出現選取面板後，在模型生成期間點選停止按鈕；按「儲存並測試」才會實際停止。' if stage==7 else
         '本步沿用第 6、7 步已標記的正文與停止按鈕，不需再框選。按開始後觀察生成中、內容變動與持續穩定的完成狀態。' if stage==8 else
         '正常頁面可直接驗證預設規則；有實際阻擋時可標記視窗。\n不會刻意登出、觸發限流或自動重送。真實阻擋未出現時會如實標示。' if stage==9 else
         '載入第 1～9 步設定，實際送出兩輪測試問題，驗證內容、歸屬、完成狀態與單次送出。'}
    registered=getattr(page,'_dialogue_guidance_stages',set())
    if stage not in registered:
        script='('+PANEL_SCRIPT+')('+json.dumps(data,ensure_ascii=False)+')'
        page.add_init_script("document.addEventListener('DOMContentLoaded',()=>{"+script+"},{once:true});")
        page._dialogue_guidance_stages=registered|{stage}
    deadline=time.monotonic()+60
    while True:
        try:
            page.evaluate(PANEL_SCRIPT,data)
            break
        except Exception as exc:
            if not navigation_changed(exc) or time.monotonic()>=deadline:raise
            page.wait_for_timeout(150)
    if stage in (6,7):update_panel(page,'waiting','正在準備頁面與校準設定，請稍候…')
    return wait_panel_action(page) if wait else None


def wait_panel_action(page):
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        try:action=page.evaluate('()=>window.__smartAgentDialoguePanel?.action||""')
        except Exception as exc:
            if not navigation_changed(exc):raise
            page.wait_for_timeout(150);continue
        if action=='cancel':return None
        if action in {'start','mark'}:return action
        page.wait_for_timeout(150)
    raise TimeoutError('dialogue_confirmation_timeout')


def update_panel(page,phase,message):
    try:
        page.evaluate('data=>window.__smartAgentDialoguePanel?.update(data.phase,data.message)',
                      {'phase':phase,'message':message})
    except Exception as exc:
        if not navigation_changed(exc):raise


def wait_panel_close(page):
    while not page.is_closed():
        if page.evaluate('()=>window.__smartAgentDialoguePanel?.action==="cancel"||window.__smartAgentComponentPicker?.action==="cancel"||window.__smartAgentFilePicker?.action==="cancel"'):break
        page.wait_for_timeout(200)


def begin_request(page,probe,*,stop_selector=None):
    if page.evaluate('()=>window.__smartAgentDialoguePanel?.action==="cancel"||window.__smartAgentComponentPicker?.action==="cancel"||window.__smartAgentFilePicker?.action==="cancel"'):
        raise InterruptedError('calibration_cancelled')
    adapter=create_web_ui_for_page(page)
    blocked=adapter.blocking_transport_error()
    if blocked:raise RuntimeError('provider_blocked:'+blocked['reason'])
    if adapter.generation_active():raise ValueError('previous_response_still_generating')
    scope=adapter.capture_request(probe['prompt'])
    prepare_probe(page,probe)
    document=load_dom_config(adapter.provider_name)
    selector=stop_selector or document['elements'].get('stop',{}).get('selector') or ','.join(adapter.profile.stop_selectors)
    arm_generation_trace(page,selector)
    control=wait_send_ready(page,document['elements']['send']['selector'])
    if not adapter.validate_composer(probe['prompt']).matched:raise AssertionError('probe_changed_before_submit')
    from .component_calibration import click_automation_send
    click_automation_send(adapter,control)
    # A successful click is not proof that the website accepted a new turn.
    # Never spend the response timeout waiting on a previous/stopped answer.
    update_panel(page,'preflight','已點擊送出一次，正在確認本輪測試訊息已出現在對話中；不會自動重送。')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if page.evaluate('()=>window.__smartAgentDialoguePanel?.action==="cancel"||window.__smartAgentComponentPicker?.action==="cancel"||window.__smartAgentFilePicker?.action==="cancel"'):
            raise InterruptedError('calibration_cancelled')
        blocked=adapter.blocking_transport_error()
        if blocked:raise RuntimeError('provider_blocked:'+blocked['reason'])
        adapter.confirm_user_turn(scope)
        if scope.user_turn is not None:
            update_panel(page,'preflight','已確認本輪測試訊息送出。正在等待本輪回答完成、停止按鈕消失及正文持續穩定；不會自動重送。')
            return scope
        page.wait_for_timeout(150)
    raise TimeoutError('test_message_not_accepted:未確認本輪測試訊息出現在對話中，已停止等待回答；請確認網站狀態後重新執行，不會自動重送')


def wait_send_ready(page,selector):
    deadline=time.monotonic()+15
    while True:
        if page.evaluate('()=>window.__smartAgentDialoguePanel?.action==="cancel"||window.__smartAgentComponentPicker?.action==="cancel"||window.__smartAgentFilePicker?.action==="cancel"'):
            raise InterruptedError('calibration_cancelled')
        try:return send_control(page,selector)
        except ValueError:
            if time.monotonic()>=deadline:raise TimeoutError('send_control_not_ready:no_resubmit')
            page.wait_for_timeout(150)


def content_probe(page):
    probe=make_probe(page);marker=probe['marker']
    expected=f'{marker}\n第一段：文字校準。\n第二段：保留換行。\nprint("{marker}")\n{marker}_END'
    raw=f'Please reply with these exact paragraphs and Python code, with no explanation. First line: {marker}. Then two separate paragraphs: 第一段：文字校準。 and 第二段：保留換行。. Then a fenced python code block containing print("{marker}"). Last line: {marker}_END.'
    probe.update(prompt=create_web_ui_for_page(page).prepare_transport_prompt(raw),expected=expected,end_marker=marker+'_END')
    return probe


def long_probe(page):
    probe=make_probe(page)
    raw=f'Begin with {probe["marker"]}. Then write 600 numbered lines, each containing a different short English sentence about everyday objects. Do not summarize or skip numbers. This is a UI Stop-button test.'
    probe['prompt']=create_web_ui_for_page(page).prepare_transport_prompt(raw)
    return probe


def policy(provider):
    return load_capability(provider,'generation_state') or {'quiet_seconds':5}


def check_body(text,probe):
    for required in (probe['marker'],probe['end_marker'],'第一段：文字校準。','第二段：保留換行。',f'print("{probe["marker"]}")'):
        if required not in text:raise AssertionError('answer_content_missing_expected_part:'+required)
    if '\n' not in text or any(t in text for t in ('Copy code','複製程式碼','分享這個對話')):
        raise AssertionError('answer_content_includes_controls_or_loses_newlines')


def calibrate_content(page,evidence):
    provider=provider_from_url(page.url);require_config(provider,6)
    update_panel(page,'preflight','前置校準設定已載入。正在填入並送出本輪測試問題…')
    first=content_probe(page);scope=begin_request(page,first)
    adapter=create_web_ui_for_page(page)
    update_panel(page,'preflight','已送出一次測試問題。正在等待本輪回答與結尾標記，完成後會顯示手動選取面板；不會自動重送。')
    _,_,_=wait_owned_response(adapter,scope,first['end_marker'],allow_raw_preview=True)
    selection=select_component(page,6,first)
    if selection is None:return None
    candidate=candidate_adapter(page,'assistant_content',selection['selector'])
    turn=candidate.latest_owned_assistant(scope)
    if turn is None:raise AssertionError('selected_content_not_owned')
    check_body(candidate.extract_final_text(turn),first)
    second=content_probe(page);scope2=begin_request(page,second)
    turn2,text,_=wait_owned_response(candidate,scope2,second['marker'])
    check_body(text,second)
    if first['marker'] in text or candidate.durable_turn_id(turn)==candidate.durable_turn_id(turn2):
        raise AssertionError('answer_content_reused_old_response')
    path=save_element(provider,page.url,'assistant_content',selection['selector'],selection,{'content_verified':True,'distinct_probe_markers_verified':True})
    runtime=create_web_ui_for_page(page)
    if runtime.profile.final_content_selector!=selection['selector']:raise AssertionError('runtime_content_config_not_loaded')
    return dict(passed=True,selector=selection['selector'],config_path=str(path),runtime_config_loaded=True,
                two_turns_verified=True,code_and_paragraphs_verified=True,selection_preflight=selection.get('preflight'))


def calibrate_stop(page,evidence):
    provider=provider_from_url(page.url);require_config(provider,7)
    update_panel(page,'preflight','前置校準設定已載入。正在送出較長的測試問題，稍後請選停止按鈕…')
    probe=long_probe(page);scope=begin_request(page,probe)
    adapter=create_web_ui_for_page(page)
    # First-time manual selection must not depend on a built-in Stop selector.
    # The selected control itself is checked again before the one Stop click.
    selection=select_component(page,7,probe)
    if selection is None:return None
    control=send_control(page,selection['selector'])
    arm_generation_trace(page,selection['selector'])
    control.click(timeout=10000) # One explicit Stop, never a second submit.
    deadline=time.monotonic()+45
    while candidate_adapter(page,'stop',selection['selector']).generation_active():
        if time.monotonic()>deadline:raise TimeoutError('stop_control_did_not_disappear')
        page.wait_for_timeout(150)
    adapter.confirm_user_turn(scope)
    if scope.user_turn is None:raise AssertionError('stop_probe_user_not_accepted')
    # Re-arm a draft to distinguish Send from the idle voice button, without sending.
    draft=make_probe(page);prepare_probe(page,draft)
    wait_send_ready(page,load_dom_config(provider)['elements']['send']['selector'])
    clear_probe(page,draft['prompt'])
    trace=finish_generation_trace(page)
    if not any(x['active'] for x in trace):raise AssertionError('stop_active_state_not_observed')
    path=save_element(provider,page.url,'stop',selection['selector'],selection,{'stop_verified':True,'stop_clicked_once':True})
    if create_web_ui_for_page(page).profile.stop_selectors!=(selection['selector'],):raise AssertionError('runtime_stop_config_not_loaded')
    return dict(passed=True,selector=selection['selector'],config_path=str(path),runtime_config_loaded=True,
                stop_clicked_once=True,submit_count=1,send_returned=True,trace=trace)


def verify_lifecycle(page,evidence):
    provider=provider_from_url(page.url);require_config(provider,7)
    if 'stop' not in load_dom_config(provider)['elements']:raise ValueError('please_complete_stage7')
    update_panel(page,'preflight','已載入正文與停止按鈕。正在送出測試問題，驗證生成中→待機及正文持續穩定至少 5 秒。')
    probe=content_probe(page);scope=begin_request(page,probe)
    adapter=create_web_ui_for_page(page)
    turn,text,samples=wait_owned_response(adapter,scope,probe['marker'])
    check_body(text,probe);trace=finish_generation_trace(page)
    if not trace or trace[0]['active'] or not any(x['active'] for x in trace) or trace[-1]['active']:
        raise AssertionError('generation_idle_active_idle_not_observed')
    settings={'strategy':'stop_busy_and_stable_text','quiet_seconds':5,
              'validation':{'lifecycle_verified':True,'active_to_idle_verified':True},
              'trace':trace,'response_samples':samples,'response_id':adapter.durable_turn_id(turn)}
    path=save_capability(provider,'generation_state',settings)
    if load_capability(provider,'generation_state').get('quiet_seconds')!=5:raise AssertionError('lifecycle_config_not_loaded')
    return dict(passed=True,config_path=str(path),runtime_config_loaded=True,active_to_idle_verified=True,quiet_seconds=5,trace=trace)


def verify_blocking(page,evidence,action):
    provider=provider_from_url(page.url);rules=copy.deepcopy(DEFAULT_BLOCKING_RULES);real=False
    if action=='mark':
        page.locator('#smartagent-dialogue-panel').evaluate('el=>el.style.display="none"')
        selected=select_component(page,9,{'marker':'','prompt':''})
        if selected is None:return None
        element=page.locator(selected['selector']);text=element.inner_text().strip()
        matched=next((r for r in rules if any(t.casefold() in text.casefold() for t in r['tokens'])),None)
        if matched:matched['selectors'].insert(0,selected['selector'])
        else:rules.append({'kind':'provider_error','selectors':[selected['selector']],'tokens':[text[:120]]})
        observed=detect_blocking(page,rules)
        if not observed:raise AssertionError('selected_blocker_not_detected')
        real=True
    else:
        if not create_web_ui_for_page(page).authentication_state().authenticated:raise ValueError('login_required_before_normal_page_verification')
        if detect_blocking(page,rules):raise ValueError('actual_blocker_present_use_mark_mode')
        update_panel(page,'preflight','正常頁面未發現阻擋。正在確認規則可由 runtime 載入；真實阻擋未出現時不會宣稱已實測。')
    settings={'rules':rules,'real_blocker_observed':real,
              'positive_validation':'real_site' if real else 'fixture_only',
              'validation':{'rules_verified':True,'normal_page_verified':not real}}
    path=save_capability(provider,'blocking_state',settings)
    current=create_web_ui_for_page(page).blocking_transport_error()
    if bool(current)!=real:raise AssertionError('runtime_blocking_rules_not_loaded')
    return dict(passed=True,config_path=str(path),runtime_config_loaded=True,real_blocker_observed=real,
                positive_validation=settings['positive_validation'],automatic_dismissal=False,automatic_resubmit=False)


def verify_conversation(page,evidence):
    provider=provider_from_url(page.url);document=load_dom_config(provider)
    missing=set(('composer','send','user_turn','user_content','assistant_turn','assistant_content','stop'))-set(document['elements'])
    if missing or not all(load_capability(provider,n) for n in ('generation_state','blocking_state')):
        raise ValueError('please_complete_stages1_through9:'+','.join(sorted(missing)))
    results=[];old_marker=None;ids=[]
    for index in range(2):
        probe=content_probe(page);scope=begin_request(page,probe)
        adapter=create_web_ui_for_page(page)
        turn,text,samples=wait_owned_response(adapter,scope,probe['marker'],quiet_seconds=policy(provider)['quiet_seconds'])
        check_body(text,probe)
        if old_marker and old_marker in text:raise AssertionError('previous_round_leaked_into_current_response')
        if adapter.composer_snapshot().semantic_text.strip():raise AssertionError('composer_not_empty_after_submit')
        identity=adapter.durable_turn_id(turn)
        if identity in ids:raise AssertionError('response_identity_reused')
        ids.append(identity);old_marker=probe['marker']
        results.append(dict(round=index+1,marker=probe['marker'],scope_id=scope.scope_id,
                            response_id=identity,user_bound=True,submit_count=1,content_verified=True,
                            idle_verified=True,trace=finish_generation_trace(page),samples=samples))
    settings={'rounds':results,'validation':{'loop_verified':True},'verified_at':datetime.now(timezone.utc).isoformat()}
    path=save_capability(provider,'conversation_loop',settings)
    return dict(passed=True,full_conversation=True,config_path=str(path),runtime_config_loaded=True,
                rounds=results,submit_count=2,automatic_resubmit=False,attachments_verified=False)


def run(url,stage,*,endpoint='',no_wait=False):
    from ..paths import log_root
    provider=provider_from_url(url)
    evidence=log_root()/'manual_dom_it'/f'stage{stage}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}'
    evidence.mkdir(parents=True,exist_ok=True);result={'passed':False,'stage':stage}
    try:
        with open_manual_page(url,endpoint=endpoint) as page:
            try:
                # Show guidance before config checks, composer hydration or a
                # model response can delay the actual component picker.
                panel(page,stage,wait=False)
                update_panel(page,'waiting','頁面載入中，正在等待瀏覽器完成初始化…')
                from playwright.sync_api import TimeoutError as BrowserTimeout
                try:page.wait_for_load_state('load',timeout=5000)
                except BrowserTimeout:
                    # Some sites keep auxiliary frames loading indefinitely.
                    # Their actual composer readiness is the controlling gate.
                    pass
                if not page.locator('#smartagent-dialogue-panel').count():panel(page,stage,wait=False)
                document=load_dom_config(provider)
                if not document:raise ValueError('please_complete_stage1')
                # An identity-provider redirect must finish before site DOM
                # selection; the guidance itself reinstalls on each document.
                deadline=time.monotonic()+180
                while True:
                    try:at_provider=provider_from_url(page.url)==provider
                    except ValueError:at_provider=False
                    if at_provider:break
                    update_panel(page,'waiting','等待目標網站登入完成，請在此頁完成登入後再繼續。')
                    if time.monotonic()>=deadline:raise TimeoutError('target_provider_not_ready')
                    page.wait_for_timeout(200)
                # A login/error screen may have no composer; stage 9 must still
                # allow the user to mark that actual blocking panel.
                if stage!=9:
                    update_panel(page,'waiting','等待輸入框可用；若尚未登入，請先在此頁登入。完成前不會送出測試問題。')
                    deadline=time.monotonic()+180
                    composer=page.locator(document['elements']['composer']['selector'])
                    while not composer.count() or not composer.first.is_visible():
                        if page.evaluate('window.__smartAgentDialoguePanel.action')=='cancel':return None
                        if time.monotonic()>=deadline:raise TimeoutError('composer_not_ready:請先登入或重新校準第1步輸入框')
                        page.wait_for_timeout(200)
                    if stage>=8:update_panel(page,'waiting','頁面已就緒，請按「開始驗證」。')
                action=wait_panel_action(page) if stage>=8 else 'start'
                if action is None:return None
                if stage==6:result=calibrate_content(page,evidence)
                elif stage==7:result=calibrate_stop(page,evidence)
                elif stage==8:result=verify_lifecycle(page,evidence)
                elif stage==9:result=verify_blocking(page,evidence,action)
                else:result=verify_conversation(page,evidence)
                if result is None:return None
                result.update(stage=stage,provider=provider,conversation_url=page.url,
                              full_conversation=stage==10,blank_pages_in_context=sum(p.url=='about:blank' for p in page.context.pages))
                message=('正常頁面與預設規則已驗證。真實阻擋尚未出現；正向辨識以本機測試驗證。' if stage==9 and not result['real_blocker_observed'] else
                         '本輪校準與功能驗證完成。按「關閉」結束。')
                if stage in (6,7) or (stage==9 and action=='mark'):
                    page.evaluate('message=>{document.querySelector("#smartagent-dialogue-panel").style.display="none";window.__smartAgentComponentPicker.finish(message);}',message)
                else:page.evaluate('data=>{document.querySelector("#smartagent-dialogue-panel").style.display="block";window.__smartAgentDialoguePanel.finish(data,true);}',message)
                page.screenshot(path=str(evidence/'verified.png'))
                print('[dialogueAdapter] '+json.dumps(result,ensure_ascii=False),flush=True)
                if not no_wait:wait_panel_close(page)
                return result
            except InterruptedError:
                return None
            except Exception as exc:
                if not page.is_closed():
                    message=f'{type(exc).__name__}: {exc}'
                    page.evaluate('message=>{if(window.__smartAgentComponentPicker){document.querySelector("#smartagent-dialogue-panel").style.display="none";window.__smartAgentComponentPicker.fail(message);}else{window.__smartAgentDialoguePanel?.finish(message,false);}}',message)
                    page.screenshot(path=str(evidence/'failure.png'))
                    print('[dialogueAdapter] 驗證未通過：'+message+'\n[dialogueAdapter] EVIDENCE='+str(evidence),flush=True)
                    if not no_wait:wait_panel_close(page)
                raise
            finally:
                if not page.is_closed():finish_generation_trace(page)
    except Exception as exc:
        result={'passed':False,'stage':stage,'error':f'{type(exc).__name__}: {exc}'}
        raise
    finally:
        (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('[dialogueAdapter] EVIDENCE='+str(evidence),flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=int,choices=NAMES,required=True)
    parser.add_argument('--url',default='');parser.add_argument('--cdp-endpoint',default='');parser.add_argument('--no-wait',action='store_true')
    args=parser.parse_args(argv)
    try:
        result=run(normalize_web_conversation_url(args.url or prompt_url(TITLES[args.stage])),args.stage,endpoint=args.cdp_endpoint,no_wait=args.no_wait)
        print('[dialogueAdapter] RESULT='+('CALIBRATED_AND_VERIFIED' if result else 'CANCELLED'));return 0
    except (InterruptedError,KeyboardInterrupt):print('[dialogueAdapter] RESULT=CANCELLED');return 0
    except Exception as exc:print(f'[dialogueAdapter] FAILED: {type(exc).__name__}: {exc}');return 2


if __name__=='__main__':raise SystemExit(main())
