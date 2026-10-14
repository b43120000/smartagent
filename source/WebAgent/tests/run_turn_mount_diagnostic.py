"""Read-only live provider diagnostic; never sends, reloads or closes browser."""
import json
import argparse
from playwright.sync_api import sync_playwright
from agent_core.web_ui.providers.chatgpt.adapter import ChatGPTUIAdapter

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cdp', required=True)
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        pages = [page for context in browser.contexts for page in context.pages
            if page.url == args.url]
        if len(pages) != 1:
            raise RuntimeError('diagnostic_conversation_not_unique')
        adapter = ChatGPTUIAdapter(pages[0])
        print(json.dumps({'blocking_error':adapter.blocking_transport_error()},ensure_ascii=True))

if __name__ == '__main__':
    run()
