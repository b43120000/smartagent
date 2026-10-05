#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT browser attachment and trusted-human composer bridge."""
from __future__ import annotations

from contextlib import contextmanager
import os
import re
import time
import urllib.request
from pathlib import Path

from agent_core.conversation_identity import conversation_id
from agent_core.web_provider_routing import provider_for_url
from agent_core.web_ui.factory import normalize_web_conversation_url
from agent_core.paths import remote_execution_page_lock_path
from agent_core.web_ui import create_web_ui_for_page

CHATGPT_HOME = "https://chatgpt.com/"
EXECUTION_PAGE_LOCK = remote_execution_page_lock_path()
REMOTE_AGENT_EXECUTION_PAGE_MARKER = "SMARTAGENT_REMOTE_AGENT0_V1"

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


def _page_is_closed(page) -> bool:
    try:
        check = getattr(page, "is_closed", None)
        return bool(check()) if callable(check) else bool(getattr(page, "closed", False))
    except Exception:
        return True


def _visible_composer(page):
    """Return the composer through the provider-neutral web_ui boundary."""
    return create_web_ui_for_page(page).visible_composer()


def select_chatgpt_page(context):
    pages = [page for page in list(getattr(context, "pages", []) or []) if not _page_is_closed(page)]
    preferred = [page for page in pages if str(getattr(page, "url", "") or "").startswith("https://chatgpt.com/")]
    candidates = preferred or pages
    return max(candidates, key=_page_score) if candidates else context.new_page()


@contextmanager
def execution_page_lease(
    *,
    timeout_sec: float = 120.0,
    label: str = "RemoteAgent execution page",
    marker_path: str | Path = EXECUTION_PAGE_LOCK,
):
    """Serialize Agent0 navigation and request-scoped Worker page ownership."""
    from agent_core.process_file_lock import exclusive_process_lock

    with exclusive_process_lock(
        marker_path,
        timeout_sec=timeout_sec,
        label=label,
        legacy_kind="remote-execution-page-lease-v1",
    ):
        yield


def normalize_conversation_url(url: str) -> str:
    value = normalize_web_conversation_url(url).rstrip("/")
    if not conversation_id(value):
        raise ValueError(f"Web conversation URL has no conversation identity: {value}")
    return value


def _conversation_ready(page, target: str, *, timeout_ms: int = 60000) -> None:
    """Navigate without depending on ChatGPT project redirects completing load."""
    target_id = conversation_id(target)
    current_id = conversation_id(str(getattr(page, "url", "") or ""))
    if current_id != target_id:
        try:
            page.evaluate("target => { window.location.assign(target); }", target)
        except Exception:
            # The execution context can be destroyed immediately after assign.
            # URL/composer readiness below is the source of truth.
            pass
    deadline = time.monotonic() + max(1.0, float(timeout_ms) / 1000.0)
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError("remote_execution_page_closed_during_navigation")
        current = str(getattr(page, "url", "") or "")
        if conversation_id(current) == target_id:
            try:
                composer = _visible_composer(page)
                if composer is not None:
                    return
            except Exception:
                pass
        page.wait_for_timeout(200)
    raise RuntimeError(
        f"remote_execution_conversation_not_ready: target={target_id} "
        f"current={conversation_id(str(getattr(page, 'url', '') or ''))}"
    )


def _open_target_conversation_unlocked(context, target_url: str):
    target = normalize_conversation_url(target_url)
    target_id = conversation_id(target)
    pages = [
        candidate for candidate in list(getattr(context, "pages", []) or [])
        if not _page_is_closed(candidate)
    ]
    matching = [
        candidate for candidate in pages
        if conversation_id(str(getattr(candidate, "url", "") or "")) == target_id
    ]
    page = matching[0] if matching else None
    # Conversation identity is unique inside the RemoteAgent profile. Collapse
    # historical duplicates before a second worker can trigger another request.
    for duplicate in matching[1:]:
        try:
            duplicate.close()
        except Exception:
            pass
    if page is None:
        # The caller holds the common cross-process page lease, so this check
        # and create operation is atomic across Local/Web/Remote interfaces.
        page = context.new_page()
        try:
            _conversation_ready(page, target)
        except BaseException:
            # Never strand the transient about:blank target when navigation or
            # composer readiness fails.
            try:
                page.close()
            except Exception:
                pass
            raise
    # /c/<id> is the stable conversation identity.  Project slugs, query
    # strings and redirects are only URL aliases; reloading an already-matching
    # page can interrupt an in-flight composer or get stuck in ChatGPT routing.
    try:
        page.bring_to_front()
    except Exception:
        pass
    return page


def open_target_conversation(context, target_url: str):
    """Reuse one Page per canonical /c/<id>, serialized across all interfaces."""
    with execution_page_lease(
        timeout_sec=120.0,
        label="common WebGPT conversation page",
    ):
        return _open_target_conversation_unlocked(context, target_url)


def attach_remote_agent_execution_page(context, target_url: str = ""):
    """Return Agent0's persistent execution page without creating or navigating a tab.

    ``window.name`` is only a fast ownership hint.  ChatGPT navigation and the
    shared WebAgent runtime may replace it with the canonical conversation
    marker while keeping the same live page.  In that case recover only by the
    exact requested ``/c/<id>`` identity; never select an arbitrary ChatGPT tab.
    """
    pages = [
        candidate for candidate in list(getattr(context, "pages", []) or [])
        if not _page_is_closed(candidate)
    ]
    marked = []
    for candidate in pages:
        try:
            marker = str(candidate.evaluate("() => window.name") or "")
        except Exception:
            continue
        if marker == REMOTE_AGENT_EXECUTION_PAGE_MARKER:
            marked.append(candidate)
    if not marked:
        target_id = conversation_id(str(target_url or ""))
        if target_id:
            marked = [
                candidate for candidate in pages
                if conversation_id(str(getattr(candidate, "url", "") or ""))
                == target_id
            ]
        if not marked:
            raise RuntimeError("remote_agent_execution_page_unavailable")

    page = marked[0]
    current = str(getattr(page, "url", "") or "")
    if not conversation_id(current):
        raise RuntimeError(
            f"remote_agent_execution_page_not_ready: current={current or 'EMPTY'}"
        )
    try:
        ready = _visible_composer(page) is not None
    except Exception:
        ready = False
    if not ready:
        raise RuntimeError("remote_agent_execution_page_composer_unavailable")
    try:
        page.bring_to_front()
    except Exception:
        pass
    return page


def open_or_attach_browser(target_url: str, *, reuse_remote_agent_page: bool = False):
    from agent_core import web_runtime

    target = normalize_conversation_url(target_url)
    provider = provider_for_url(target)
    endpoint = web_runtime.CHATGPT_CDP_ENDPOINT if provider == "chatgpt" else ""
    if provider == "chatgpt" and _cdp_available(endpoint):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(endpoint)
        if not browser.contexts:
            pw.stop()
            raise RuntimeError("既有 CDP browser 沒有可用的 BrowserContext")
        context = browser.contexts[0]
        page = (
            attach_remote_agent_execution_page(context, target)
            if reuse_remote_agent_page
            else open_target_conversation(context, target)
        )
        mode = "REUSE_REMOTE_AGENT_PAGE" if reuse_remote_agent_page else "REUSE_EXISTING_CDP"
        return page, context, pw, mode
    web_runtime.SERVICE_CONFIG[provider]["url"] = target
    scraper = web_runtime.get_manager().get_or_create(provider)
    if scraper._page is None or scraper._browser is None:
        raise RuntimeError("ChatGPT browser 啟動後沒有可用頁面")
    page = (
        attach_remote_agent_execution_page(scraper._browser, target)
        if reuse_remote_agent_page
        else open_target_conversation(scraper._browser, target)
    )
    scraper._page = page
    return page, scraper._browser, None, "OWN_PERSISTENT_BROWSER"


def adopt_page(page, context, attached_pw=None):
    from agent_core import web_runtime

    manager = web_runtime.get_manager()
    provider = provider_for_url(str(getattr(page, "url", "") or ""))
    scraper = manager._scrapers.get(provider) or web_runtime.WebLLMScraper(service=provider)
    scraper._pw = attached_pw or getattr(scraper, "_pw", None)
    scraper._browser = context
    scraper._page = page
    scraper._claim_conversation_owner(str(getattr(page, "url", "") or ""))
    manager._scrapers[provider] = scraper
    return scraper


class BrowserInputBridge:
    def __init__(self, page, *, provider: str = ""):
        self.page = page
        self.web_ui = create_web_ui_for_page(page, provider=provider)

    def install(self, *, reset_legacy: bool = False) -> None:
        self.web_ui.install_input_bridge(reset_legacy=reset_legacy)

    def installed(self) -> bool:
        return self.web_ui.input_bridge_installed()

    def pop(self) -> dict | None:
        return self.web_ui.pop_input_bridge()
