"""Claude implementation of the provider-neutral WebUI adapter contract."""
from __future__ import annotations

import hashlib
from ...base_adapter import BaseWebUIAdapter
from .composer import read_composer_snapshot, validate_composer_prompt
from ...contracts import CompatibilityReport, RequestScope, TurnRef, text_digest
from .compatibility import probe_profile
from .profiles import ClaudeUIProfile, active_profile, profile_by_name
from .bridge import BRIDGE_SCRIPT, BRIDGE_VERSION
from . import attachments


class ClaudeUIAdapter(BaseWebUIAdapter):
    """Claude DOM boundary.

    The inherited request-scope, turn ownership, media and artifact algorithms are
    selector-neutral; all provider-specific selectors and controls are overridden
    by this package.
    """

    provider_name = "claude"
    authenticated_session_selectors = (
        "button[aria-label*='account' i]",
        "button[aria-label*='profile' i]",
        "[data-testid*='account']",
    )
    login_required_selectors = (
        "a[href*='/login']", "input[type='email']", "input[type='password']",
    )
    login_url_tokens = ("/login", "/oauth")

    def __init__(self, page, *, profile_name: str = ""):
        self.page = page
        requested = str(profile_name or "").strip()
        if requested:
            profile = profile_by_name(requested)
            if profile is None:
                raise RuntimeError(f"WEB_UI_PROFILE_UNKNOWN: {requested}")
            self.profile = profile
        else:
            self.profile = active_profile()
        self._compatibility_report: CompatibilityReport | None = None

    def probe_compatibility(self, *, force: bool = False) -> CompatibilityReport:
        if self._compatibility_report is not None and not force:
            return self._compatibility_report
        report = probe_profile(self.page, self.profile)
        self._compatibility_report = report
        return report

    def require_compatible(self) -> CompatibilityReport:
        report = self.probe_compatibility()
        if not report.supported:
            raise RuntimeError(
                "WEB_UI_PROFILE_UNSUPPORTED: " + ",".join(report.reasons or ("unknown",))
            )
        return report

    def _active_profile(self) -> ClaudeUIProfile:
        self.require_compatible()
        assert self.profile is not None
        return self.profile

    @staticmethod
    def _safe_attr(element, name: str) -> str:
        try:
            return str(element.get_attribute(name) or "")
        except Exception:
            return ""

    def _turn_ref(self, element, role: str, ordinal: int) -> TurnRef:
        attrs = tuple(
            (name, value)
            for name in ("data-testid", "id", "data-message-id", "data-is-streaming")
            if (value := self._safe_attr(element, name))
        )
        raw_text = self._turn_text(element, role)
        structural_source = "\\n".join(f"{name}={value}" for name, value in attrs)
        if not structural_source:
            structural_source = f"{role}:ordinal:{ordinal}"
        structural_id = hashlib.sha256(
            f"{role}\\n{structural_source}".encode("utf-8", errors="replace")
        ).hexdigest()
        return TurnRef(
            role=role, ordinal=ordinal, structural_id=structural_id,
            content_digest=text_digest(raw_text), raw_text=raw_text,
            stable_attributes=attrs, element=element,
        )

    @staticmethod
    def _is_relevant_assistant_image(info: dict) -> bool:
        if not bool(info.get("visible")):
            return False
        semantic = " ".join(str(info.get(key) or "") for key in (
            "alt", "aria_label", "testid", "class_name",
        )).lower()
        generation_marker = any(marker in semantic for marker in (
            "generated", "generating", "image generation", "generated image",
        ))
        natural_width = int(info.get("naturalWidth") or 0)
        natural_height = int(info.get("naturalHeight") or 0)
        rendered_width = float(info.get("renderedWidth") or 0)
        rendered_height = float(info.get("renderedHeight") or 0)
        substantial = (natural_width >= 64 and natural_height >= 64) or (
            rendered_width >= 96 and rendered_height >= 96
        )
        return bool(generation_marker or substantial)


    def composer_snapshot(self):
        return read_composer_snapshot(self.visible_composer())

    def validate_composer(self, prompt: str):
        return validate_composer_prompt(prompt, self.composer_snapshot())

    def confirm_user_turn(self, scope: RequestScope):
        if self.profile is None or self.profile.name != scope.profile_name:
            self.profile = profile_by_name(scope.profile_name)
            self._compatibility_report = None
        if self.profile is None:
            return None
        candidate = self._confirmed_user_turn_candidate(scope, self.turns("user"))
        if candidate is None:
            return None
        scope.user_turn = candidate
        return scope.user_turn

    def install_input_bridge(self, *, reset_legacy: bool = False) -> None:
        if reset_legacy:
            state = self.page.evaluate("""() => ({
              installed: !!window.__webAgentDirectBridgeInstalled,
              version: Number(window.__webAgentDirectBridgeVersion || 0)
            })""")
            if state.get("installed") and int(state.get("version") or 0) < BRIDGE_VERSION:
                self.page.reload(wait_until="domcontentloaded", timeout=60000)
        self.page.context.add_init_script(f"({BRIDGE_SCRIPT})()")
        if not self.page.evaluate(BRIDGE_SCRIPT):
            raise RuntimeError("WEB_UI_INPUT_BRIDGE_INSTALL_FAILED")

    def input_bridge_installed(self) -> bool:
        try:
            return bool(self.page.evaluate(
                "() => !!window.__webAgentDirectBridgeInstalled && "
                f"Number(window.__webAgentDirectBridgeVersion || 0) === {BRIDGE_VERSION}"
            ))
        except Exception:
            return False

    def attachment_file_inputs(self) -> tuple:
        return tuple(attachments.file_inputs(self.page))

    def attachment_button(self):
        return attachments.attach_button(self.page)

    def attachment_menu_items(self) -> tuple:
        return attachments.attach_menu_items(self.page)

    def attachment_dom_state(self, expected_names: list[str]) -> dict:
        return attachments.attachment_dom_state(self.page, expected_names)

    def composer_attachment_count(self) -> int:
        return attachments.composer_attachment_count(self.page)

    def clear_one_attachment(self) -> bool:
        return attachments.clear_one_attachment(self.page)

    def dismiss_rate_limit_dialog(self) -> bool:
        result = self.dismiss_blocking_dialog()
        return bool(result.get("dismissed") and result.get("kind") == "rate_limit")

    def conversation_display_name(self) -> str:
        for selector in (
            '[data-testid*="conversation-title"]',
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
                    if value and len(value) <= 160 and value.lower() not in {"claude", "new chat"}:
                        return value
            except Exception:
                continue
        try:
            title = (self.page.title() or "").strip()
        except Exception:
            title = ""
        for suffix in (" - Claude", " | Claude"):
            if title.endswith(suffix):
                title = title[:-len(suffix)].strip()
        return "" if title.lower() in {"", "claude", "new chat"} else title[:160]

    def conversation_link(self, conversation_id: str):
        try:
            links = self.page.locator(f'a[href*="/chat/{conversation_id}"]')
            for index in range(links.count()):
                link = links.nth(index)
                if link.is_visible():
                    return link
        except Exception:
            pass
        return None

    def disconnect_signature_visible(self) -> bool:
        try:
            body = str(self.page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            return False
        return any(token in body for token in (
            "connection error", "reconnecting", "something went wrong",
        ))

    def dismiss_blocking_dialog(self) -> dict:
        try:
            return self.page.evaluate(r"""() => {
                const visible = el => {
                    if (!el || !el.isConnected) return false;
                    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
                };
                const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible);
                const rateTokens = ['too many requests', 'rate limit', 'reached your limit'];
                const closeTokens = ['ok', 'okay', 'got it', 'close', 'dismiss'];
                for (const root of dialogs) {
                    const text = (root.innerText || root.textContent || '').trim().toLowerCase();
                    if (!rateTokens.some(token => text.includes(token))) continue;
                    for (const button of root.querySelectorAll('button, [role="button"]')) {
                        const label = `${button.innerText || button.textContent || ''} ${button.getAttribute('aria-label') || ''}`
                            .trim().toLowerCase();
                        if (visible(button) && closeTokens.some(token => label.includes(token))) {
                            button.click();
                            return {dismissed:true, kind:'rate_limit', detail:text};
                        }
                    }
                }
                return {dismissed:false};
            }""") or {}
        except Exception:
            return {}


__all__ = ["ClaudeUIAdapter"]
