"""ChatGPT implementation of the provider-neutral Web UI adapter."""
from __future__ import annotations

import re
import uuid

from ...base_adapter import BaseWebUIAdapter
from . import attachments
from .bridge import BRIDGE_SCRIPT, BRIDGE_VERSION
from .compatibility import probe_profile
from .composer import read_composer_snapshot, validate_composer_prompt
from .profiles import (
    LEGACY_ROLE_PROFILE,
    SEARCH_UNIT_2026_PROFILE,
    active_profile,
    profile_by_name,
)


class ChatGPTUIAdapter(BaseWebUIAdapter):
    provider_name = "chatgpt"
    authenticated_session_selectors = (
        "button[data-testid='profile-button']",
        "button[aria-label*='profile' i]",
        "button[aria-label*='account' i]",
    )
    login_required_selectors = (
        "a[href*='/auth/login']", "button[data-testid='login-button']",
    )
    login_url_tokens = ("/auth/login", "/auth/signup")
    bridge_script = BRIDGE_SCRIPT
    bridge_version = BRIDGE_VERSION
    attachments = attachments
    stable_turn_attributes = (
        "data-message-id", "data-testid", "id",
        "data-content-search-unit-key",
        "data-chatgpt-search-message-ids",
        "data-chatgpt-selection-message-id",
    )

    @classmethod
    def load_profile_by_name(cls, name: str):
        return profile_by_name(name)

    @classmethod
    def load_active_profile(cls):
        return active_profile()

    def probe_provider_profile(self, profile):
        return probe_profile(self.page, profile)

    def read_provider_composer(self, composer):
        return read_composer_snapshot(composer)

    def validate_provider_composer(self, prompt: str, snapshot):
        return validate_composer_prompt(prompt, snapshot)

    def conversation_unit_id(self, turn) -> str:
        """Pair search-unit user/assistant nodes within the current snapshot.

        ``fallback-turn-N`` is intentionally not treated as durable identity:
        the renderer may renumber it after every response.  Its prefix is still
        the authoritative user/assistant grouping key while reading one DOM
        snapshot.
        """
        attrs = dict(getattr(turn, "stable_attributes", ()) or ())
        key = str(attrs.get("data-content-search-unit-key", "") or "").strip()
        match = re.match(
            r"^(fallback-turn-[^:]+):\d+:(?:user|assistant)$",
            key,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).casefold()
        generic = re.match(r"^(.*):(?:user|assistant)$", key, flags=re.IGNORECASE)
        return generic.group(1).casefold() if generic else ""

    def durable_turn_id(self, turn) -> str:
        """Use native message identity without volatile search-unit position."""
        attrs = dict(getattr(turn, "stable_attributes", ()) or ())
        for name in (
            "data-message-id",
            "data-chatgpt-selection-message-id",
            "data-chatgpt-search-message-ids",
            "id",
        ):
            value = str(attrs.get(name, "") or "").strip()
            if value:
                return f"{name}:{value}"
        return super().durable_turn_id(turn)

    def prepare_transport_prompt(self, prompt: str) -> str:
        return '[WEBUI_SUBMIT_ID]\nWUS-' + uuid.uuid4().hex.upper() + '\n[/WEBUI_SUBMIT_ID]\n' + prompt

    def assistant_is_fresh(self, turn, scope) -> bool:
        attrs = dict(turn.stable_attributes)
        # Only recycled fallback keys may use a changed-content proof. Native
        # message IDs remain immutable. A unique current submit anchor is required.
        native = any(attrs.get(name) for name in ('data-message-id',
            'data-chatgpt-selection-message-id', 'data-chatgpt-search-message-ids', 'id'))
        if native or not str(attrs.get('data-content-search-unit-key','')).startswith('fallback-turn-'):
            return super().assistant_is_fresh(turn, scope)
        if not any(marker.startswith('WUS-') for marker in scope.request_markers):
            return super().assistant_is_fresh(turn, scope)
        if not scope.user_turn:
            return False
        from ...base_adapter import _request_markers
        if not set(scope.request_markers).intersection(_request_markers(scope.user_turn.raw_text)):
            return False
        unit = self.conversation_unit_id(scope.user_turn)
        if not unit or self.conversation_unit_id(turn) != unit:
            return False
        baseline_digests = {digest for _identity, digest in scope.baseline_assistant_content}
        return bool(turn.content_digest not in baseline_digests and turn.raw_text.strip())

    def recover_turn_mounts(self) -> bool:
        # Use only the pinned profile. Scroll the latest known user into view
        # to remount its conversation unit; no reload, alternate selector or send.
        users = self.turn_elements('user')
        if not users:
            return False
        try:
            users[-1].scroll_into_view_if_needed(timeout=1500)
            return True
        except Exception:
            return False

    def turn_mount_diagnostic(self) -> dict:
        try:
            return self.page.evaluate(r"""() => ({units:Array.from(document.querySelectorAll('[data-content-search-unit-key]')).slice(-14).map(e=>({key:e.getAttribute('data-content-search-unit-key'),message:e.getAttribute('data-message-id'),tag:e.tagName,text_len:(e.innerText||'').length,ready_marker_present:/\[AGENT_(SESSION|PROTOCOL)_READY\]/.test(e.innerText||'')}))})""")
        except Exception as exc:
            return {'error_type':type(exc).__name__}

    def blocking_transport_error(self) -> dict:
        """Only a visible provider error panel, not quoted user/model content."""
        try:
            return self.page.evaluate("""() => {
                const texts = ['你已達到上限。請稍後再試一次。', "You've reached the limit. Please try again later."];
                const visible = e => !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
                for (const e of document.querySelectorAll('div,p,span')) {
                    const text=(e.innerText||'').trim();
                    const full = /^(You've reached the maximum length for this conversation|You have reached the maximum length for this conversation)/i.test(text)
                        || /^.{0,12}(這段|此|這個)?對話.{0,12}(長度上限|已滿)/.test(text);
                    if(e.children.length || !visible(e) || (!texts.includes(text) && !full)) continue;
                    if(e.closest('[data-content-search-unit-key], [data-message-author-role], #prompt-textarea, pre, code, [contenteditable=true]')) continue;
                    let panel=e;
                    for(let i=0;panel && i<5;i++,panel=panel.parentElement) {
                        if(panel.tagName==='BODY' || panel.tagName==='MAIN') break;
                        const controls=full ? ['New chat','Start a new chat','新聊天','開始新聊天','開啟新對話','重試','Retry'] : ['重試','Retry'];
                        if(Array.from(panel.querySelectorAll('button,a')).some(b=>visible(b) && controls.includes((b.innerText||'').trim())))
                            return {reason:full?'PROVIDER_CONVERSATION_FULL':'PROVIDER_RATE_LIMITED',message:text.slice(0,300),retry_control_visible:true};
                    }
                }
                return {};
            }""") or {}
        except Exception:
            return {}

    def analysis_complete_visible(self, assistant=None) -> bool:
        """Observe ChatGPT's collapsed analysis label without treating it as final output."""
        if assistant is None:
            return False
        if hasattr(assistant, "raw_text"):
            text = str(getattr(assistant, "raw_text", "") or "")
        else:
            try:
                text = str(assistant.inner_text() or "")
            except Exception:
                text = ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return any(line.strip() == "已分析" for line in text.split("\n"))

    def assistant_wait_fallbacks(
        self, markers: tuple[str, ...], *, limit: int = 3
    ) -> dict:
        """Probe provider-owned assistant selectors for diagnostic marker evidence."""
        profiles = (
            ("selected_profile", self.profile),
            ("legacy_role", LEGACY_ROLE_PROFILE),
            ("search_unit", SEARCH_UNIT_2026_PROFILE),
        )
        result = {}
        seen_selectors = set()
        sample_limit = max(1, int(limit))
        marker_tokens = tuple(str(marker) for marker in markers if str(marker))
        for name, profile in profiles:
            selector = str(getattr(profile, "assistant_turn_selector", "") or "")
            if not selector or selector in seen_selectors:
                continue
            seen_selectors.add(selector)
            try:
                elements = list(self.page.query_selector_all(selector))
                marker_found = False
                for element in elements[-sample_limit:]:
                    try:
                        text = str(element.inner_text() or "")
                    except Exception:
                        try:
                            text = str(element.text_content() or "")
                        except Exception:
                            continue
                    if any(marker in text for marker in marker_tokens):
                        marker_found = True
                        break
                result[name] = {
                    "count": len(elements),
                    "ready_marker_in_last_three": marker_found,
                }
            except Exception as exc:
                result[name] = {"error_type": type(exc).__name__}
        return result

    @staticmethod
    def _is_relevant_assistant_image(info: dict) -> bool:
        if not bool(info.get("visible")):
            return False
        semantic = " ".join(str(info.get(key) or "") for key in (
            "alt", "aria_label", "testid", "class_name",
        )).lower()
        marked = any(marker in semantic for marker in (
            "generated", "generating", "imagegen", "image-gen", "dall-e", "dalle",
            "產生", "生成",
        ))
        natural_width = int(info.get("naturalWidth") or 0)
        natural_height = int(info.get("naturalHeight") or 0)
        rendered_width = float(info.get("renderedWidth") or 0)
        rendered_height = float(info.get("renderedHeight") or 0)
        substantial = (
            natural_width >= 64 and natural_height >= 64
        ) or (
            rendered_width >= 96 and rendered_height >= 96
        )
        return bool(marked or substantial)

    def disconnect_signature_visible(self) -> bool:
        try:
            body = str(self.page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            return False
        return any(token in body for token in (
            "connection interrupted", "reconnecting", "連線中斷",
        ))

    def dismiss_rate_limit_dialog(self) -> bool:
        try:
            modal = self.page.locator('[data-testid="modal-conversation-history-rate-limit"]')
            if not modal.count() or not modal.is_visible():
                return False
            buttons = modal.locator("button")
            if buttons.count():
                buttons.last.click(timeout=10000)
                modal.wait_for(state="hidden", timeout=10000)
                return True
        except Exception:
            pass
        return False

    def conversation_display_name(self) -> str:
        for selector in (
            '[data-testid="conversation-title"]',
            'nav a[aria-current="page"]',
            'aside a[aria-current="page"]',
            'h1',
        ):
            try:
                locator = self.page.locator(selector)
                for index in range(locator.count() - 1, -1, -1):
                    item = locator.nth(index)
                    if not item.is_visible():
                        continue
                    value = (item.inner_text() or "").strip()
                    if value and len(value) <= 160 and value.lower() not in {"chatgpt", "new chat"}:
                        return value
            except Exception:
                continue
        try:
            title = (self.page.title() or "").strip()
        except Exception:
            title = ""
        for suffix in (" - ChatGPT", " | ChatGPT", " — ChatGPT"):
            if title.endswith(suffix):
                title = title[:-len(suffix)].strip()
        return "" if title.lower() in {"", "chatgpt", "new chat"} else title[:160]

    def conversation_link(self, conversation_id: str):
        links = self.page.locator(f'a[href*="/c/{conversation_id}"]')
        for index in range(links.count()):
            link = links.nth(index)
            if link.is_visible():
                return link
        return None

    def dismiss_blocking_dialog(self) -> dict:
        try:
            return self.page.evaluate("""() => {
                const kinds = [
                    {kind: 'rate_limit', phrases: ['太多要求', 'too many requests']},
                    {kind: 'duplicate_attachment', phrases: [
                        '你已上傳此檔案', '已上傳此檔案', '嘗試上傳新的內容',
                        "you've already uploaded this file", 'already uploaded this file',
                        'try uploading new content'
                    ]}
                ];
                const buttonNames = ['確定', '確認', '知道了', 'got it', 'ok', 'okay'];
                const visible = el => {
                    if (!el || !el.isConnected) return false;
                    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
                };
                const label = el => `${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''}`
                    .trim().toLowerCase();
                const roots = [...new Set(document.querySelectorAll(
                    '[role="dialog"], [data-testid*="modal"], [class*="modal"]'
                ))].filter(visible);
                for (const root of roots) {
                    const text = (root.innerText || root.textContent || '').toLowerCase();
                    const match = kinds.find(item => item.phrases.some(token => text.includes(token)));
                    if (!match) continue;
                    for (const button of root.querySelectorAll('button, [role="button"]')) {
                        if (visible(button) && buttonNames.some(name => label(button).includes(name))) {
                            button.click();
                            return {dismissed: true, kind: match.kind,
                                detail: (root.innerText || root.textContent || text).trim()};
                        }
                    }
                }
                return {dismissed: false};
            }""") or {}
        except Exception:
            return {}


__all__ = ["ChatGPTUIAdapter"]
