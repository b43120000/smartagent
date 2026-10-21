"""Exercise actual11–15 BATs in an owned tab. Caller must check account quota first."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .live_composer_it import driver_context
from ..file_calibration import NAMES
from ..factory import create_web_ui_for_page
from ..provider_registry import provider_from_url


def run_direct(url,stage):
    """Single CDP client fallback: same runtime/picker, not a BAT-launcher proof."""
    from playwright.sync_api import sync_playwright
    from unittest.mock import patch
    from .. import file_calibration as flow
    from ...paths import log_root,local_data_root
    from ..manual_browser import active_agent_endpoint
    evidence=log_root()/'manual_dom_it'/('files_direct_'+provider_from_url(url)+'_'+str(stage)+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    evidence.mkdir(parents=True,exist_ok=True)
    result={'stage':stage,'provider':provider_from_url(url),'passed':False,'real_site':True,'actual_numbered_batch':False}
    with sync_playwright() as p:
        page=None
        try:
            browser=p.chromium.connect_over_cdp(active_agent_endpoint(provider_from_url(url)) or 'http://127.0.0.1:12801',timeout=15000)
            context=browser.contexts[0];original=list(context.pages);page=context.new_page()
            page.goto(url,wait_until='commit',timeout=30000)
            page.locator('textarea:visible,[contenteditable=true]:visible').first.wait_for(timeout=45000)
            def pick(page,stage):
                flow.show(page,stage)
                target=target_for(page,stage)
                panel=page.locator('#smartagent-file-picker')
                panel.locator('#pick').click()
                target.evaluate('el=>el.dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true}))')
                panel.locator('#save').click()
                page.screenshot(path=str(evidence/'selection.png'))
                return page.evaluate('window.__smartAgentFilePicker.selection.selector')
            with patch.object(flow,'select',side_effect=pick):
                result.update(flow.run_stage(page,stage,local_data_root()/'manual_dom_file_it'/evidence.name),passed=True)
            result['shared_tabs_preserved']=all(not item.is_closed() for item in original)
        except Exception as exc:
            result['error']=type(exc).__name__+': '+str(exc)
        finally:
            if page is not None and not page.is_closed():
                try:page.screenshot(path=str(evidence/'final.png'),timeout=5000)
                except Exception:pass
                page.close()
    (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**result,'evidence':str(evidence)},ensure_ascii=False),flush=True)
    return result


def target_for(page,stage):
    adapter=create_web_ui_for_page(page)
    if stage==11:
        if adapter.provider_name=='m365':return page.get_by_test_id('PlusMenuButton')
        target=adapter.attachment_button()
        if target:return target
        return page.get_by_role('button',name=re.compile('新增檔案|新增檔案和|上傳檔案|Upload files|Add files|附件|加入檔案|Add attachments',re.I)).first
    if stage==12:
        return page.locator('[role=menuitem],button').filter(has_text=re.compile('新增相片和檔案|新增相片與檔案|上傳影像和檔案|上傳檔案|Upload files|Upload from computer|從電腦上傳|從裝置上傳|新增檔案|Add files',re.I)).filter(visible=True).last
    if stage==13:
        inputs=page.locator('input[type=file]')
        names=page.evaluate('()=>window.__smartAgentAttachmentProbe?.selectedFiles?.map(f=>f.name)||[...document.querySelectorAll("input[type=file]")].flatMap(el=>[...(el.files||[])].map(f=>f.name))')
        if not names:raise ValueError('no_test_file_selected')
        text=page.get_by_text(names[0],exact=True).filter(visible=True).last
        text.wait_for(timeout=45000)
        node=text
        for _ in range(7):
            if node.locator('button[aria-label*="remove" i],button[aria-label*="移除"],button[aria-label*="刪除"],button[data-testid*="remove"]').count():
                return node
            # Upload completion can replace the card between discovery/click.
            # A locator resolves against the current DOM; ElementHandle cannot.
            node=node.locator('..')
        raise ValueError('test_attachment_chip_with_remove_not_found')
    if stage==14:
        document=__import__('importlib').import_module('..dom_config',__package__).load_dom_config(adapter.provider_name)
        root=page.locator(document['elements']['assistant_turn']['selector']).last
        controls=root.locator('a[download],a[href*="sandbox"],a[href*="/files/"],a').filter(has_text=re.compile('result|下載|Download',re.I)).or_(root.locator('button[aria-label*="Download" i],button[aria-label*="\u4e0b\u8f09"]'))
        controls.first.wait_for(timeout=240000)
        return controls.first
    raise ValueError('unknown_selection_stage')


def run(url,stage):
    from ...paths import log_root
    provider=provider_from_url(url)
    evidence=log_root()/'manual_dom_it'/('files_'+provider+'_'+str(stage)+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    evidence.mkdir(parents=True,exist_ok=True)
    result={'stage':stage,'provider':provider,'passed':False,'real_site':True}
    with driver_context(provider,'') as (context,endpoint):
        original=list(context.pages);app=Path(__file__).resolve().parents[4]
        env=dict(os.environ,SMARTAGENT_TEST_NO_PAUSE='1',PYTHONIOENCODING='utf-8')
        args=['cmd.exe','/c',str(app/'adapterUI'/f'{stage}.{NAMES[stage]}.bat'),'--url',url,'--cdp-endpoint',endpoint,'--no-wait']
        process=None;page=None
        try:
            with context.expect_page(timeout=60000) as fresh:
                process=subprocess.Popen(args,cwd=app,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8')
            page=fresh.value
            panel=page.locator('#smartagent-file-picker');panel.wait_for(timeout=60000)
            if stage!=15:
                page.wait_for_function("()=>window.__smartAgentFilePicker?.selectionReady===true",timeout=240000 if stage==14 else 60000)
                if stage==13:
                    page.wait_for_function('()=>window.__smartAgentAttachmentProbe?.selectedFiles?.length||[...document.querySelectorAll("input[type=file]")].some(e=>e.files?.length)',timeout=45000)
                    page.wait_for_timeout(1000)
                if stage==14:
                    page.wait_for_function('()=>!document.querySelector("#smartagent-file-picker")?.shadowRoot?.querySelector("#status")?.textContent?.includes("附件已就緒")',timeout=240000)
                target=target_for(page,stage)
                panel.locator('#pick').click()
                try:
                    target.click(timeout=3000 if stage==14 else 15000)
                except Exception as exc:
                    if stage!=14 or 'intercepts pointer events' not in str(exc):raise
                    target.focus();target.press('Enter')
                if stage==13:
                    for _ in range(7):
                        complete=page.evaluate('''()=>{const s=window.__smartAgentFilePicker?.selection;const n=s&&document.querySelector(s.selector);return !!n?.querySelector('button[aria-label*="remove" i],button[aria-label*="delete" i],button[aria-label*="\u79fb\u9664"],button[aria-label*="\u522a\u9664"],button[data-testid*="remove"]');}''')
                        if complete:break
                        panel.locator('#expand').click()
                    else:raise ValueError('selected_attachment_missing_remove_control')
                (evidence/'selection.json').write_text(json.dumps(page.evaluate('()=>window.__smartAgentFilePicker.selection'),ensure_ascii=False,indent=2),encoding='utf-8')
                page.screenshot(path=str(evidence/'selection.png'))
                panel.locator('#save').click()
            output,_=process.communicate(timeout=360)
            (evidence/'launcher.log').write_text(output,encoding='utf-8')
            records=[json.loads(line.split(' ',1)[1]) for line in output.splitlines() if line.startswith('[fileAdapter] {')]
            if records:result.update(records[-1])
            result['shared_tabs_preserved']=all(not p.is_closed() for p in original)
            result['actual_numbered_batch']=True
            if not result['passed']:raise ValueError('file_batch_failed:'+output[-2000:])
        except Exception as exc:
            result['error']=type(exc).__name__+': '+str(exc)
            if page and not page.is_closed():
                try:page.screenshot(path=str(evidence/'failure.png'),timeout=5000)
                except Exception:pass
        finally:
            if process is not None and process.poll() is None:
                if page and not page.is_closed():page.close()
                try:output,_=process.communicate(timeout=5)
                except subprocess.TimeoutExpired:process.terminate();output,_=process.communicate(timeout=5)
                (evidence/'launcher.log').write_text(output,encoding='utf-8')
            log=evidence/'launcher.log'
            if log.exists() and not result.get('passed'):
                lines=log.read_text(encoding='utf-8').splitlines()
                records=[json.loads(line.split(' ',1)[1]) for line in lines if line.startswith('[fileAdapter] {')]
                if records and records[-1].get('error'):
                    result['driver_error']=result.get('error','')
                    result['error']=records[-1]['error']
            result['shared_tabs_preserved']=all(not p.is_closed() for p in original)
    (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**result,'evidence':str(evidence)},ensure_ascii=False),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--stage',type=int,required=True,choices=NAMES)
    p.add_argument('--direct',action='store_true')
    args=p.parse_args();raise SystemExit(0 if (run_direct if args.direct else run)(args.url,args.stage)['passed'] else 2)
