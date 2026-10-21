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
from agent_core.shared_browser import (shared_browser_enabled, shared_browser_endpoint,
    connect_shared_browser, mark_shared_page, is_shared_page, same_provider_url)

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
    # URL validity and a provider's canonical conversation ID are separate.
    # Other origins already use a deterministic URL hash. Known entry pages
    # may acquire their real ID only after navigation or human selection.
    return normalize_web_conversation_url(url).rstrip("/")


def _conversation_ready(page, target: str, *, timeout_ms: int = 60000) -> None:
    """Navigate without depending on ChatGPT project redirects completing load."""
    target_id = conversation_id(target)
    target_provider = provider_for_url(target)
    current_id = conversation_id(str(getattr(page, "url", "") or ""))
    if not target_id:
        print('[WebAgent] URL is a site entry. Select the intended existing conversation '
              'in this new tab; waiting for its conversation ID before session initialization.', flush=True)
    if not target_id or current_id != target_id:
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
        current_id = conversation_id(current)
        if current_id and (not target_id or current_id == target_id):
            try:
                if provider_for_url(current) != target_provider:
                    raise ValueError('conversation_redirect_provider_mismatch')
                composer = _visible_composer(page)
                if composer is not None:
                    return
            except Exception:
                pass
        page.wait_for_timeout(200)
    if not target_id:
        raise RuntimeError('WEB_CONVERSATION_ID_PENDING: select an existing conversation '
                           'and use its full URL; no session was initialized and no message was submitted.')
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
        if target_id and conversation_id(str(getattr(candidate, "url", "") or "")) == target_id
        and provider_for_url(str(candidate.url)) == provider_for_url(target)
        and (not shared_browser_enabled() or (is_shared_page(candidate) and same_provider_url(candidate.url, target)))
    ]
    page = matching[0] if matching else None
    # Conversation identity is unique inside the RemoteAgent profile. Collapse
    # historical duplicates before a second worker can trigger another request.
    for duplicate in ([] if shared_browser_enabled() else matching[1:]):
        try:
            duplicate.close()
        except Exception:
            pass
    if page is None:
        # The caller holds the common cross-process page lease, so this check
        # and create operation is atomic across Local/Web/Remote interfaces.
        page = context.new_page()
        if shared_browser_enabled():
            mark_shared_page(page)
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
                and (not shared_browser_enabled() or (is_shared_page(candidate) and same_provider_url(candidate.url, target_url)))
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
    if provider not in web_runtime.SERVICE_CONFIG:
        if not provider.startswith("other_"):
            raise ValueError(f"Unsupported WebGPT provider: {provider}")
        web_runtime.SERVICE_CONFIG[provider] = {
            "url": target,
            "new_chat_url": target,
            "send_by_enter": True,
            "profile_subdir": provider,
        }
    web_runtime.SERVICE_CONFIG[provider]["url"] = target
    use_shared = shared_browser_enabled()
    endpoint = shared_browser_endpoint() if use_shared else web_runtime.CHATGPT_CDP_ENDPOINT
    if not conversation_id(target) and not use_shared and not _cdp_available(endpoint):
        # A manager cannot bind an identity-less URL. Launch the standard
        # authenticated browser first; resolve the real ID before adoption.
        from playwright.sync_api import sync_playwright
        from agent_core.web_ui.manual_browser import ensure_agent_browser
        with sync_playwright() as starter:
            endpoint, _ = ensure_agent_browser(starter)
    if use_shared or _cdp_available(endpoint):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = connect_shared_browser(pw, endpoint)[0] if use_shared else pw.chromium.connect_over_cdp(endpoint)
        except BaseException:
            pw.stop()
            raise
        if not browser.contexts:
            pw.stop()
            raise RuntimeError("既有 CDP browser 沒有可用的 BrowserContext")
        context = browser.contexts[0]
        try:
            page = (
                attach_remote_agent_execution_page(context, target)
                if reuse_remote_agent_page
                else open_target_conversation(context, target)
            )
        except BaseException:
            pw.stop()
            raise
        web_runtime.SERVICE_CONFIG[provider]['url'] = str(page.url)
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
    # All managed browser sessions now use CDP too. Adoption must never turn
    # a borrowed shared context into an owned context that close() can destroy.
    scraper._attached_over_cdp = True
    scraper._cdp_browser = context.browser
    scraper._active_cdp_endpoint = web_runtime.CHATGPT_CDP_ENDPOINT
    if attached_pw is not None:
        scraper._owns_playwright = True
    if shared_browser_enabled():
        scraper._shared_personal_browser = True
        scraper._active_cdp_endpoint = shared_browser_endpoint()
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
