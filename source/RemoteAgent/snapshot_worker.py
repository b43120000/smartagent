"""Read-only screenshot client, isolated from the main Playwright thread."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from agent_core.conversation_identity import conversation_id

MARKER = 'SMARTAGENT_REMOTE_AGENT0_V1'

def capture(browser, target_url: str, output: Path) -> dict:
    target_id = conversation_id(target_url)
    if not target_id:
        raise RuntimeError('snapshot_target_missing')
    matches = []
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed() or conversation_id(page.url) != target_id:
                continue
            # Conversation identity is the ownership boundary. window.name is
            # only a hint because the shared runtime may replace the RemoteAgent
            # marker with SMARTAGENT_CANONICAL_CONVERSATION:<id> on the same page.
            try:
                marker = str(page.evaluate('() => window.name') or '')
            except Exception:
                marker = ''
            matches.append((context, page, marker))
    if len(matches) != 1:
        raise RuntimeError(f'snapshot_execution_page_not_unique:matches={len(matches)}')
    context, page, _marker = matches[0]
    session = context.new_cdp_session(page)
    try:
        # Capture beyond the viewport without resizing, scrolling, focusing,
        # navigation, DOM writes, or waiting for the main worker's page lease.
        metrics = session.send('Page.getLayoutMetrics')
        size = metrics.get('cssContentSize') or metrics['contentSize']
        data = session.send('Page.captureScreenshot', {
            'format': 'png', 'captureBeyondViewport': True,
            'clip': {'x': 0, 'y': 0, 'width': size['width'], 'height': size['height'], 'scale': 1},
        })
        if page.is_closed() or conversation_id(page.url) != target_id:
            raise RuntimeError('snapshot_page_changed_during_capture')
        image = base64.b64decode(data['data'], validate=True)
        if not image.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('snapshot_invalid_png')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image)
        return {'photo_path': str(output.resolve()), 'message': 'WebGPT full-page snapshot', 'reloaded': False}
    finally:
        session.detach()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cdp', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    # Stopping this private driver disconnects the client; never close the
    # shared Browser, Context, or Page owned by the main runtime.
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp, timeout=10000)
        result = capture(browser, args.url, Path(args.output))
    print(json.dumps(result))

if __name__ == '__main__':
    main()
