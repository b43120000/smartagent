"""Run the real numbered BATs 6–10 in the authenticated shared browser."""
import argparse
import json
import os
import subprocess
from datetime import datetime,timezone
from pathlib import Path
from .live_composer_it import driver_context
from ..dom_config import load_dom_config
from ..dialogue_calibration import NAMES
from ..provider_registry import provider_from_url
from ..factory import create_web_ui_for_page
from ..dialogue_state import DEFAULT_BLOCKING_RULES


def run(url,stages,*,mark_blocker=False):
    from ...paths import log_root
    provider=provider_from_url(url)
    root=log_root()/'manual_dom_it'/('dialogue_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    root.mkdir(parents=True,exist_ok=True);results=[]
    try:
        for stage in stages:
            evidence=root/str(stage);evidence.mkdir()
            with driver_context(provider,'') as (context,endpoint):
                original=list(context.pages);opened=[]
                context.on('page',lambda p:opened.append(p))
                app=Path(__file__).resolve().parents[4]
                env=dict(os.environ,SMARTAGENT_TEST_NO_PAUSE='1',PYTHONIOENCODING='utf-8')
                args=['cmd.exe','/c',str(app/'adapterUI'/f'{stage}.{NAMES[stage]}.bat'),
                      '--url',url,'--cdp-endpoint',endpoint,'--no-wait']
                process=None;page=None
                try:
                    with context.expect_page(timeout=60000) as fresh:
                        process=subprocess.Popen(args,cwd=app,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8')
                    page=fresh.value
                    guidance=page.locator('#smartagent-dialogue-panel');guidance.wait_for(timeout=60000)
                    page.screenshot(path=str(evidence/'initial-guidance.png'))
                    if stage==9 and mark_blocker:guidance.locator('#mark').click()
                    if stage in (6,7) or (stage==9 and mark_blocker):
                        panel=page.locator('#smartagent-component-picker');panel.wait_for(timeout=240000)
                        document=load_dom_config(provider)
                        if stage==6:
                            marker=page.evaluate('window.__smartAgentComponentPicker.marker')
                            adapter=create_web_ui_for_page(page)
                            hint=getattr(adapter,'calibration_picker_options',lambda _:{} )('assistant_content').get('contentRootSelector') or adapter.profile.final_content_selector
                            root_turn=page.locator(document['elements']['assistant_turn']['selector']).filter(has_text=marker).last
                            bodies=[el for el in root_turn.locator(hint).all() if el.is_visible()]
                            target=max(bodies,key=lambda el:len(el.inner_text())) if bodies else root_turn
                        elif stage==7:
                            target=page.locator('button[data-testid="stop-button"],button[aria-label*="停止"],button[aria-label*="stop" i]').filter(visible=True).first
                        else:
                            tokens=[token.casefold() for rule in DEFAULT_BLOCKING_RULES for token in rule['tokens']]
                            candidates=[el for el in page.locator('[role="alert"],[role="dialog"]').all() if el.is_visible() and any(token in el.inner_text().casefold() for token in tokens)]
                            if not candidates:raise AssertionError('no_actual_known_blocker_to_mark')
                            target=max(candidates,key=lambda el:len(el.inner_text()))
                        panel.locator('#pick').click();target.click()
                        panel.locator('#status').filter(has_text='初步檢查通過，待功能驗證').wait_for(timeout=15000)
                        page.screenshot(path=str(evidence/'selection.png'))
                        panel.locator('#save').click()
                    else:
                        panel=page.locator('#smartagent-dialogue-panel');panel.wait_for(timeout=60000)
                        panel.locator('#start').click()
                    output,_=process.communicate(timeout=600)
                    (evidence/'launcher.log').write_text(output,encoding='utf-8')
                    if process.returncode or 'RESULT=CALIBRATED_AND_VERIFIED' not in output:
                        raise AssertionError('dialogue_batch_failed:'+output[-2500:])
                    result=next(json.loads(line.split(' ',1)[1]) for line in output.splitlines() if line.startswith('[dialogueAdapter] {'))
                    if len(opened)!=1 or any(p.is_closed() for p in original):raise AssertionError('shared_page_ownership_regression')
                    result.update(actual_numbered_batch=True,real_site=True,shared_tabs_preserved=True)
                    (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                    results.append(result);url=result['conversation_url']
                    print(json.dumps(dict(stage=stage,passed=True,evidence=str(evidence)),ensure_ascii=False),flush=True)
                except Exception:
                    if page is not None and not page.is_closed():
                        try:page.screenshot(path=str(evidence/'failure.png'))
                        except Exception:pass # Preserve the original BAT failure if its owned page has closed.
                    raise
                finally:
                    if process is not None and process.poll() is None:
                        if page is not None and not page.is_closed():page.close()
                        try:
                            output,_=process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.terminate();output,_=process.communicate(timeout=5)
                        (evidence/'launcher.log').write_text(output,encoding='utf-8')
    finally:
        (root/'summary.json').write_text(json.dumps(dict(passed=len(results)==len(stages),stages=results),ensure_ascii=False,indent=2),encoding='utf-8')
        print('DIALOGUE_IT_EVIDENCE='+str(root),flush=True)
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='https://chatgpt.com/')
    parser.add_argument('--stages',nargs='+',type=int,choices=NAMES,default=list(NAMES))
    parser.add_argument('--mark-blocker',action='store_true',help='Stage 9: mark an actual visible blocker without dismissing it.')
    args=parser.parse_args();run(args.url,args.stages,mark_blocker=args.mark_blocker)
