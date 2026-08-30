#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT browser attachment and trusted-human composer bridge."""
from __future__ import annotations

import re
import urllib.request

CHATGPT_HOME = "https://chatgpt.com/"

BRIDGE_SCRIPT = r"""
() => {
  if (window.__webAgentDirectBridgeInstalled) return true;
  window.__webAgentDirectBridgeInstalled = true;
  window.__webAgentDirectQueue = window.__webAgentDirectQueue || [];
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const composer = () => {
    const xs = [document.querySelector('#prompt-textarea'), document.querySelector('textarea[data-id="root"]'), document.querySelector('div[contenteditable="true"][data-lexical-editor="true"]'), document.querySelector('form div[contenteditable="true"]')];
    return xs.find(visible) || xs.find(Boolean) || null;
  };
  const textOf = el => ((el && (el.innerText || el.value || el.textContent)) || '').trim();
  const clear = el => {
    el.focus();
    if ('value' in el) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      if (setter) setter.call(el, ''); else el.value = '';
    } else {
      const range = document.createRange(); range.selectNodeContents(el);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      document.execCommand('delete'); selection.removeAllRanges();
      if (textOf(el)) el.innerHTML = '';
    }
    el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward', data:null}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
  };
  const capture = event => {
    if (!event.isTrusted) return false;
    const box = composer(), text = textOf(box);
    if (!box || !text) return false;
    event.preventDefault(); event.stopImmediatePropagation(); clear(box);
    window.__webAgentDirectQueue.push({text, captured_at:Date.now()});
    return true;
  };
  document.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    const box = composer();
    if (box && (event.target === box || box.contains(event.target))) capture(event);
  }, true);
  document.addEventListener('click', event => {
    const button = event.target?.closest?.('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="傳送"]');
    if (button) capture(event);
  }, true);
  return true;
}
"""


def conversation_id(url: str) -> str:
    match = re.search(r"/c/([0-9A-Za-z-]+)", str(url or ""))
    return match.group(1) if match else ""


def _cdp_available(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def _page_score(page) -> tuple[int, int]:
    url = str(getattr(page, "url", "") or "")
    score = 10 if url.startswith("https://chatgpt.com/") else 0
    score += 10 if conversation_id(url) else 0
    try:
        state = page.evaluate("() => ({focus:document.hasFocus(), visibility:document.visibilityState, worker:window.name||''})")
        score += 100 if state.get("focus") else 0
        score += 20 if state.get("visibility") == "visible" else 0
        score -= 1000 if str(state.get("worker", "")).startswith("SMARTAGENT_REMOTE_WORKER:") else 0
    except Exception:
        pass
    return score, len(url)


def select_chatgpt_page(context):
    pages = [page for page in list(getattr(context, "pages", []) or []) if not page.is_closed()]
    preferred = [page for page in pages if str(getattr(page, "url", "") or "").startswith("https://chatgpt.com/")]
    candidates = preferred or pages
    return max(candidates, key=_page_score) if candidates else context.new_page()


def normalize_conversation_url(url: str) -> str:
    value = str(url or "").strip().strip('"').rstrip("/")
    if not value.startswith("https://chatgpt.com/") or not conversation_id(value):
        raise ValueError(f"不是有效的 ChatGPT 對話連結（必須包含 /c/...）: {value}")
    return value


def open_target_conversation(context, target_url: str):
    target = normalize_conversation_url(target_url)
    target_id = conversation_id(target)
    page = None
    for candidate in list(getattr(context, "pages", []) or []):
        if not candidate.is_closed() and conversation_id(str(getattr(candidate, "url", "") or "")) == target_id:
            page = candidate
            break
    if page is None:
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
    elif str(getattr(page, "url", "") or "").rstrip("/") != target:
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
    try:
        page.bring_to_front()
    except Exception:
        pass
    return page


def open_or_attach_browser(target_url: str):
    from agent_core import web_runtime

    target = normalize_conversation_url(target_url)
    endpoint = web_runtime.CHATGPT_CDP_ENDPOINT
    if _cdp_available(endpoint):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(endpoint)
        if not browser.contexts:
            pw.stop()
            raise RuntimeError("既有 CDP browser 沒有可用的 BrowserContext")
        context = browser.contexts[0]
        return open_target_conversation(context, target), context, pw, "REUSE_EXISTING_CDP"
    web_runtime.SERVICE_CONFIG["chatgpt"]["url"] = target
    scraper = web_runtime.get_manager().get_or_create("chatgpt")
    if scraper._page is None or scraper._browser is None:
        raise RuntimeError("ChatGPT browser 啟動後沒有可用頁面")
    page = open_target_conversation(scraper._browser, target)
    scraper._page = page
    return page, scraper._browser, None, "OWN_PERSISTENT_BROWSER"


def adopt_page(page, context, attached_pw=None):
    from agent_core import web_runtime

    manager = web_runtime.get_manager()
    scraper = manager._scrapers.get("chatgpt") or web_runtime.WebLLMScraper(service="chatgpt")
    scraper._pw = attached_pw or getattr(scraper, "_pw", None)
    scraper._browser = context
    scraper._page = page
    manager._scrapers["chatgpt"] = scraper
    return scraper


class BrowserInputBridge:
    def __init__(self, page):
        self.page = page

    def install(self) -> None:
        self.page.context.add_init_script(f"({BRIDGE_SCRIPT})()")
        if not self.page.evaluate(BRIDGE_SCRIPT):
            raise RuntimeError("無法安裝 WebAgent composer bridge")

    def installed(self) -> bool:
        try:
            return bool(self.page.evaluate("() => !!window.__webAgentDirectBridgeInstalled"))
        except Exception:
            return False

    def pop(self) -> dict | None:
        value = self.page.evaluate("""() => {
          const q = window.__webAgentDirectQueue || [];
          return q.length ? q.shift() : null;
        }""")
        return dict(value) if isinstance(value, dict) else None
