"""Opt-in live CDP startup check; never submits content or closes existing tabs."""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from agent_core.web_runtime import WebLLMScraper


def run(endpoint):
    result = {"test": "attached_chatgpt_startup", "messages_submitted": 0}
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=6000)
        context = browser.contexts[0]
        originals = list(context.pages)
        target = next((p.url for p in originals
                       if p.url.startswith("https://chatgpt.com/") and "/c/" in p.url), "")
        if not target:
            raise RuntimeError("Open an existing ChatGPT conversation in this browser first")
        page = context.new_page()
        try:
            scraper = WebLLMScraper("chatgpt")
            scraper.cfg = dict(scraper.cfg, url=target)
            scraper._page = page
            started = time.monotonic()
            scraper._start_attached_chatgpt_conversation(timeout_ms=30000)
            result.update(status="PASS", elapsed_seconds=round(time.monotonic() - started, 2),
                          target_match=scraper._attached_chatgpt_target_matches(),
                          composer_visible=scraper._web_ui_adapter().visible_composer().is_visible())
        except Exception as exc:
            result.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc)[:500])
        finally:
            page.close()
            result["original_pages_preserved"] = all(not p.is_closed() for p in originals)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:12801")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.endpoint)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
