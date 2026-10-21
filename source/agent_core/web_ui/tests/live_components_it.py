"""Real Web IT for numbered stage 2–5 launchers; never uses fixture HTML."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from ..dom_config import load_dom_config
from ..provider_registry import provider_from_url
from .live_composer_it import driver_context

NAMES={2:'sendButton',3:'userTurn',4:'userContent',5:'assistantTurn'}


def run_stage(url: str, stage: int, evidence: Path, *, check_invalid: bool = False) -> dict:
    provider=provider_from_url(url)
    app_root=Path(__file__).resolve().parents[4]
    evidence.mkdir(parents=True,exist_ok=True)
    process=None
    result={'passed':False,'stage':stage,'real_site':True}
    try:
        with driver_context(provider,'') as (context,endpoint):
            original=[p for p in context.pages if p not in context._manual_it_bootstrap_pages]
            opened=[]
            context.on('page',lambda page:opened.append(page))
            args=['cmd.exe','/c',str(app_root/'adapterUI'/f'{stage}.{NAMES[stage]}.bat'),
                  '--url',url,'--cdp-endpoint',endpoint,'--no-wait']
            env=dict(os.environ,SMARTAGENT_TEST_NO_PAUSE='1',PYTHONIOENCODING='utf-8')
            with context.expect_page(timeout=60000) as new_page:
                process=subprocess.Popen(args,cwd=app_root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8')
            page=new_page.value
            panel=page.locator('#smartagent-component-picker')
            panel.wait_for(state='visible',timeout=180000)
            page.wait_for_timeout(1000)
            marker=page.evaluate('() => window.__smartAgentComponentPicker.marker')
            document=load_dom_config(provider)
            if provider=='gemini' and stage==2:
                target=page.get_by_role('button',name='傳送訊息',exact=True)
            elif provider=='gemini' and stage==3:
                target=page.locator('span.user-query-bubble-with-background').filter(has_text=marker).last
            elif provider=='gemini' and stage==4:
                target=page.locator(document['elements']['user_turn']['selector']).filter(has_text=marker).last.locator('p.query-text-line').filter(has_text=marker).last
            elif provider=='gemini' and stage==5:
                target=page.locator('div.response-container-content').filter(has_text=marker).last
            elif provider=='m365' and stage==2:
                from ..factory import create_web_ui_for_page
                target=create_web_ui_for_page(page).send_control()
            elif provider=='m365' and stage==4:
                root=page.locator(document['elements']['user_turn']['selector']).filter(has_text=marker).last
                target=root.locator(document['elements']['user_content']['selector']).last
            elif provider=='m365' and stage==5:
                target=page.locator('div.fai-CopilotMessage').filter(has_text=marker).last
            elif provider=='claude' and stage==3:
                target=page.locator('div[data-cds="UserMessage"] > div.relative:has(.cds-user-message-body)').filter(has_text=marker).last
            elif provider=='claude' and stage==5:
                target=page.locator('div[data-testid="assistant-message"]').filter(has_text=marker).last
            elif stage==2:
                target=page.get_by_role('button',name='傳送',exact=True)
                if not target.count():target=page.get_by_role('button',name='Send',exact=True)
            elif stage in (3,4):
                selector=document['elements'].get('user_turn',{}).get('selector',"[data-content-search-unit-key$=':user'],[data-message-author-role='user']")
                target=page.locator(selector).filter(has_text=marker).last
                if stage==4:target=target.locator('div.whitespace-pre-wrap').last
            else:
                target=page.locator("[data-content-search-unit-key$=':assistant'],[data-message-author-role='assistant']").filter(has_text=marker).last
            target.wait_for(state='visible',timeout=30000)
            if check_invalid and stage in (3,5):
                wrong=(page.locator(document['elements']['user_turn']['selector']).filter(has_text=marker).last
                       if stage==5 else page.get_by_text(marker+' OK',exact=True).last)
                panel.locator('#pick').click()
                wrong.click()
                panel.locator('#status').filter(has_text='選取不符').wait_for(timeout=15000)
                if not panel.locator('#save').is_disabled():raise AssertionError('invalid_selection_can_save')
                page.screenshot(path=str(evidence/'invalid_selection.png'))
            panel.locator('#pick').click()
            if provider=='gemini' and stage==5:
                # Click observed frame padding, not the inner <p> (which has
                # no stable reusable turn selector on Gemini).
                target.scroll_into_view_if_needed()
                position=target.evaluate('''el=>{
                    const r=el.getBoundingClientRect();
                    for(const y of [2,r.height/2,r.height-2])for(const x of [2,r.width/2,r.width-2])
                        if(document.elementFromPoint(r.left+x,r.top+y)===el)return {x,y};
                    return null;
                }''')
                if not position:raise AssertionError('gemini_frame_padding_not_visible')
                target.click(position=position)
            else:
                target.click()
            selection=page.evaluate('() => window.__smartAgentComponentPicker.selected')
            if not selection:raise AssertionError('human_click_did_not_select_component')
            if provider=='gemini' and stage in (3,5):
                # A center click can land on the inner paragraph. Expand to
                # the actual bubble so IT verifies the requested outer frame.
                for _ in range(6):
                    selected=page.locator(selection['selector']).filter(has_text=marker).last
                    expected='span.user-query-bubble-with-background' if stage==3 else 'div.response-container-content'
                    if selected.evaluate('(el,selector)=>el.matches(selector)',expected):
                        break
                    panel.get_by_role('button',name='擴大範圍',exact=True).click()
                    selection=page.evaluate('() => window.__smartAgentComponentPicker.selected')
                else:
                    raise AssertionError('gemini_outer_bubble_not_selected')
            # Selection itself must never submit stage 2's draft.
            if stage==2:
                composer=page.locator(document['elements']['composer']['selector'])
                if marker not in composer.inner_text():raise AssertionError('selection_accidentally_submitted_probe')
            page.screenshot(path=str(evidence/'human_selection.png'))
            panel.locator('#status').filter(has_text='初步檢查通過，待功能驗證').wait_for(timeout=15000)
            page.screenshot(path=str(evidence/'preflight_passed.png'))
            panel.get_by_role('button',name='儲存並測試',exact=True).click()
            output,_=process.communicate(timeout=240)
            (evidence/'launcher.log').write_text(output,encoding='utf-8')
            if process.returncode or 'RESULT=CALIBRATED_AND_VERIFIED' not in output:
                raise AssertionError(f'component_batch_exit={process.returncode}: {output[-3000:]}')
            result=next(json.loads(line[line.index('{'):]) for line in output.splitlines() if line.startswith('[componentAdapter] {'))
            if not result['passed'] or not result['runtime_config_loaded']:raise AssertionError('component_verification_not_passed')
            if not result.get('selection_preflight',{}).get('passed'):raise AssertionError('selection_preflight_not_passed')
            if len(opened)!=1 or any(p.is_closed() for p in original):raise AssertionError('page_ownership_regression')
            result.update(real_site=True,numbered_batch_completed=True,opened_page_count=len(opened),shared_tabs_preserved=True)
            if check_invalid and stage in (3,5):result['invalid_selection_rejected_before_save']=True
        return result
    except Exception as exc:
        result.update(passed=False,error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if process is not None and not (evidence/'launcher.log').exists():
            try:
                output,_=process.communicate(timeout=5)
                (evidence/'launcher.log').write_text(output,encoding='utf-8')
                print(output[-1800:],flush=True)
            except subprocess.TimeoutExpired:pass
        if process is not None and process.poll() is None:process.terminate()
        (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({**result,'evidence':str(evidence)},ensure_ascii=False),flush=True)


def run(url: str, stages: list[int], *, check_invalid: bool = False) -> list[dict]:
    from ...paths import log_root
    root=log_root()/'manual_dom_it'/('components_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    results=[]
    try:
        for stage in stages:
            result=run_stage(url,stage,root/str(stage),check_invalid=check_invalid)
            results.append(result)
            url=result['conversation_url']
        return results
    finally:
        root.mkdir(parents=True,exist_ok=True)
        (root/'summary.json').write_text(json.dumps({'passed':len(results)==len(stages) and all(r['passed'] for r in results),'stages':results},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='https://chatgpt.com/')
    parser.add_argument('--stages',nargs='+',type=int,choices=NAMES,default=[2,3,4,5])
    parser.add_argument('--check-invalid',action='store_true')
    args=parser.parse_args()
    run(args.url,args.stages,check_invalid=args.check_invalid)
