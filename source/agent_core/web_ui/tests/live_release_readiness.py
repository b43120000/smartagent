"""Read-only live-site checks in temporary tabs; never submits or changes bindings."""
import argparse
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright
from agent_core.remote_binding import validate
from agent_core.runtime_environment import planner_environment_snapshot
from agent_core.web_ui.factory import create_web_ui_for_page, provider_from_url


def run(endpoint, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'submitted': False, 'live_sites': True, 'environment': planner_environment_snapshot(), 'sites': []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
        context = browser.contexts[0]
        for name, url in [('chatgpt','https://chatgpt.com/'), ('gemini','https://gemini.google.com/app'),
                          ('claude','https://claude.ai/new'), ('m365','https://m365.cloud.microsoft/chat')]:
            print('OPENING '+name, flush=True)
            page = context.new_page()
            page.set_default_timeout(10000)
            result = dict(provider=name, requested_url=url, status='BLOCKED', reason='')
            try:
                validate(dict(workspace=str(output), gpt_url=url, skill_path=str(output)))
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
                page.wait_for_timeout(3000)
                result['actual_url'] = page.url
                if provider_from_url(page.url) != name:
                    raise RuntimeError('site redirected to a different provider/login origin')
                deadline = time.monotonic() + 30
                while True:
                    try:
                        adapter = create_web_ui_for_page(page)
                        composer = adapter.visible_composer()
                        result['composer_visible'] = composer is not None and composer.is_visible()
                        if not result['composer_visible']:
                            raise RuntimeError('no visible composer; login, loading or DOM calibration required')
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise
                        page.wait_for_timeout(500)
                result['status'] = 'PASS'
            except Exception as exc:
                result['reason'] = str(exc)[:1200]
            finally:
                try:
                    page.screenshot(path=str(output/(name+'.png')), timeout=10000)
                except Exception:
                    pass
                page.close()
            report['sites'].append(result)
            (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(name, result['status'], result['reason'], flush=True)
    report['passed'] = all(x['status']=='PASS' for x in report['sites'])
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--output', required=True)
    args=parser.parse_args()
    run(args.endpoint,args.output)
