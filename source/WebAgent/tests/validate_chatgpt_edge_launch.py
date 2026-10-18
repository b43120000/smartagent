"""Configured browser switch affects owned ChatGPT launches, not CDP attachments."""
import os,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from agent_core import web_runtime as runtime

class Captured(Exception):pass

def run():
    with tempfile.TemporaryDirectory(prefix='smartagent-edge-test-') as value:
        root=Path(value)
        with patch.object(runtime,'PROFILE_DIR',root),patch.dict(os.environ,{
            'SMARTAGENT_ATTACH_CDP':'','SMARTAGENT_ISOLATED_BROWSER':''}):
            for provider in ('chatgpt','gemini','claude','m365'):
                pw=Mock();pw.chromium.launch_persistent_context.side_effect=Captured()
                scraper=runtime.WebLLMScraper(provider)
                try:scraper.start(playwright=pw)
                except Captured:pass
                else:raise AssertionError('launch not reached')
                kwargs=pw.chromium.launch_persistent_context.call_args.kwargs
                if provider=='chatgpt':
                    if runtime.CHATGPT_BROWSER_CHANNEL:
                        assert kwargs['channel']==runtime.CHATGPT_BROWSER_CHANNEL
                    else:
                        assert 'channel' not in kwargs
                    assert Path(kwargs['user_data_dir'])==root/runtime.CHATGPT_BROWSER_PROFILE
                else:
                    assert 'channel' not in kwargs
                    assert Path(kwargs['user_data_dir'])==root/provider
            state=root/'isolated_state.json';state.write_text('{"cookies":[],"origins":[]}',encoding='utf-8')
            with patch.dict(os.environ,{'SMARTAGENT_ISOLATED_BROWSER':'1','SMARTAGENT_REMOTE_BROWSER_STATE':str(state)}):
                pw=Mock();pw.chromium.launch.side_effect=Captured()
                scraper=runtime.WebLLMScraper('chatgpt')
                try:scraper.start(playwright=pw)
                except Captured:pass
                else:raise AssertionError('isolated launch not reached')
                kwargs=pw.chromium.launch.call_args.kwargs
                if runtime.CHATGPT_BROWSER_CHANNEL:
                    assert kwargs['channel']==runtime.CHATGPT_BROWSER_CHANNEL
                else:
                    assert 'channel' not in kwargs
            with patch.dict(os.environ,{'SMARTAGENT_ATTACH_CDP':'1'}):
                pw=Mock();pw.chromium.connect_over_cdp.side_effect=Captured()
                scraper=runtime.WebLLMScraper('chatgpt')
                try:scraper.start(playwright=pw)
                except Captured:pass
                else:raise AssertionError('CDP attachment not reached')
                assert pw.chromium.connect_over_cdp.call_count==1
                assert not pw.chromium.launch.called and not pw.chromium.launch_persistent_context.called
    print('CHATGPT_CONFIGURED_BROWSER_PASS persistent_isolated_provider_isolation')

def smoke():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel=runtime.CHATGPT_BROWSER_CHANNEL,headless=True)
        try:
            page=browser.new_page()
            agent=page.evaluate('navigator.userAgent')
            if runtime.CHATGPT_BROWSER_CHANNEL=='msedge':assert 'Edg/' in agent,agent
            else:assert 'Chrome/' in agent and 'Edg/' not in agent,agent
            print('CONFIGURED_BROWSER_REAL_LAUNCH_PASS',browser.version)
        finally:browser.close()

if __name__=='__main__':
    import sys
    smoke() if '--smoke' in sys.argv else run()
