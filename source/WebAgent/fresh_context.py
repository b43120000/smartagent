"""GAR L4: transport-verified fresh Planner bound to an existing conversation.

Default: WebGPT1 and WebGPT2 are distinct tabs in one BrowserContext.
Optional: WebGPT2 has its own configured Chrome/Edge profile and CDP context.
Both modes prepare the second conversation BEFORE L4 and verify its identity,
ownership and idle state without replaying original actions or changing routes.
The original response/outbox stays attached to WebGPT1.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_core.conversation_identity import (
    conversation_id, conversation_provider, same_conversation,
)
from agent_core.goal_anchored_recovery import FreshContextProof

def prepare_gar_browser_context(scraper):
    """Return selected WebGPT2 context; browser is kept alive by the original scraper.

    Never silently attach to the WebGPT1 context if a dedicated profile fails.
    The independently configured browser must be authenticated separately.
    """
    from agent_core.browser_preferences import browser_launch_selection
    first = browser_launch_selection(role="webgpt1")
    second = browser_launch_selection(role="webgpt2")
    original_context = getattr(scraper, "_browser", None)
    if original_context is None:
        raise RuntimeError("GAR_L4_ORIGINAL_BROWSER_CONTEXT_MISSING")
    if (first["browser"], first["instance"]) == (second["browser"], second["instance"]):
        return original_context
    existing = getattr(scraper, "_gar_second_context", None)
    if existing is not None:
        return existing
    playwright = getattr(scraper, "_pw", None)
    if playwright is None:
        raise RuntimeError("GAR_L4_SECOND_BROWSER_PLAYWRIGHT_UNAVAILABLE")
    from agent_core.web_ui.manual_browser import ensure_agent_browser
    endpoint, _ = ensure_agent_browser(playwright, role="webgpt2")
    if endpoint == str(getattr(scraper, "_active_cdp_endpoint", "") or ""):
        raise RuntimeError("GAR_L4_SECOND_BROWSER_ENDPOINT_NOT_DISTINCT")
    browser = playwright.chromium.connect_over_cdp(endpoint, timeout=60000)
    if not browser.contexts or browser.contexts[0] is original_context:
        raise RuntimeError("GAR_L4_SECOND_BROWSER_CONTEXT_UNAVAILABLE")
    # Hold the CDP browser across the entire task, including the delayed L4
    # review; stopping the original worker Playwright will detach it.
    scraper._gar_second_browser_connection = browser
    scraper._gar_second_context = browser.contexts[0]
    scraper._gar_second_endpoint = endpoint
    return browser.contexts[0]


def ensure_fresh_context_page(*, context, original_page, target_url: str,
                              page_lease_held: bool = False,
                              target_context=None) -> dict:
    """Prepare one verified second conversation in its configured context.

    No bootstrap, no task replay, no owner claim or original-page navigation.
    The L4 adapter independently verifies ownership and identity again.
    """
    from contextlib import nullcontext
    from agent_core.conversation_ownership import ConversationOwnershipRegistry
    from agent_core.paths import source_root
    from WebAgent.browser_bridge import (
        _conversation_ready, execution_page_lease, normalize_conversation_url,
    )
    from agent_core.shared_browser import shared_browser_enabled, mark_shared_page

    target = normalize_conversation_url(str(target_url or "").strip())
    original_url = str(getattr(original_page, "url", "") or "")
    expected_id = conversation_id(target)
    original_id = conversation_id(original_url)
    if not expected_id or not original_id:
        raise RuntimeError("GAR_L4_STARTUP_EXPLICIT_CONVERSATION_REQUIRED")
    if (same_conversation(original_url, target)
            or conversation_provider(target) != conversation_provider(original_url)):
        raise RuntimeError("GAR_L4_STARTUP_TARGET_NOT_DISTINCT")

    # The request-scoped RemoteAgent worker already holds this page lease;
    # only WebDirect startup needs to acquire it here.
    lease = (nullcontext() if page_lease_held else
             execution_page_lease(label="GAR L4 startup second-conversation tab"))
    with lease:
        if not getattr(context, "pages", None) or original_page not in context.pages:
            raise RuntimeError("GAR_L4_STARTUP_ORIGINAL_PAGE_NOT_ATTACHED")
        chosen_context = target_context if target_context is not None else context
        if not getattr(chosen_context, "pages", None) and not callable(
            getattr(chosen_context, "new_page", None)
        ):
            raise RuntimeError("GAR_L4_STARTUP_SECOND_CONTEXT_UNAVAILABLE")
        if chosen_context is not context and any(
            same_conversation(str(getattr(item, "url", "") or ""), target)
            for item in list(context.pages)
            if not bool(item.is_closed())
        ):
            raise RuntimeError("GAR_L4_STARTUP_TARGET_DUPLICATE_IN_ORIGINAL_CONTEXT")
        if bool(original_page.is_closed()):
            raise RuntimeError("GAR_L4_STARTUP_ORIGINAL_PAGE_CLOSED")
        matches = [
            page for page in list(chosen_context.pages)
            if not bool(page.is_closed())
            and same_conversation(str(getattr(page, "url", "") or ""), target)
        ]
        if len(matches) > 1 or (matches and matches[0] is original_page):
            raise RuntimeError("GAR_L4_STARTUP_TARGET_NOT_UNIQUE")
        if ConversationOwnershipRegistry(source_root()).references(target):
            raise RuntimeError("GAR_L4_STARTUP_TARGET_ALREADY_OWNED")
        created = not matches
        page = matches[0] if matches else None
        try:
            if created:
                # This creates only a TAB, not a browser profile or a new
                # conversation. Navigation is to the operator's existing /c/ ID.
                page = chosen_context.new_page()
                if shared_browser_enabled():
                    mark_shared_page(page)
                _conversation_ready(page, target)
            actual = str(getattr(page, "url", "") or "")
            if not same_conversation(actual, target):
                raise RuntimeError("GAR_L4_STARTUP_TARGET_IDENTITY_MISMATCH")
            if page is original_page or bool(page.is_closed()):
                raise RuntimeError("GAR_L4_STARTUP_TARGET_UNAVAILABLE")
            duplicates = [
                item for item in list(chosen_context.pages)
                if not bool(item.is_closed())
                and same_conversation(str(getattr(item, "url", "") or ""), target)
            ]
            if len(duplicates) != 1 or duplicates[0] is not page:
                raise RuntimeError("GAR_L4_STARTUP_TARGET_NOT_UNIQUE")
            # A newly created tab usually steals focus. Restore the original
            # WebCopilot page before accepting requests from its input bridge.
            original_page.bring_to_front()
        except BaseException:
            if created and page is not None:
                try:
                    page.close()  # Only the tab created by THIS startup.
                except Exception:
                    pass
            raise
        return {
            "created": created,
            "target_conversation_id": expected_id,
            "same_context": chosen_context is context,
            "bootstrap_sent": False,
        }



@dataclass
class FreshPlannerBinding:
    proof: FreshContextProof
    planner: Callable[[str, dict, list[str]], str]
    close: Callable[[], None]
    review: Callable[[str], str] | None = None


class ExistingConversationFreshAdapter:
    """Fail-closed, capability-checked adapter; no UI path inference."""

    def __init__(self, *, client, workspace: str | Path,
                 session_root: str | Path, display_name: str = "WebAgent",
                 target_url: str = ""):
        self.client = client
        self.workspace = str(workspace)
        self.session_root = Path(session_root)
        self.display_name = display_name
        self.target_url = str(target_url or
                              os.environ.get("SMARTAGENT_GAR_FRESH_CONTEXT_URL", "")).strip()
        self._used = False

    def prepare(self, *, request_id: str, task_id: str, task_epoch: str,
                owner: str) -> FreshPlannerBinding:
        if self._used:
            raise RuntimeError("GAR_L4_ADAPTER_ALREADY_USED")
        # A blank or unrecognized target is not a reason to invent a browser
        # tab or silently switch the source conversation.
        target = self.target_url
        original = self.client.scraper
        original_page = getattr(original, "_page", None)
        original_url = str(getattr(original_page, "url", "") or "")
        if not target or not conversation_id(target) or not conversation_id(original_url):
            raise RuntimeError("GAR_L4_EXPLICIT_CONVERSATION_REQUIRED")
        if (same_conversation(original_url, target)
                or conversation_provider(target) != conversation_provider(original_url)):
            raise RuntimeError("GAR_L4_CONVERSATION_NOT_DISTINCT")
        if not bool(getattr(original, "_execution_page_lease_owned", False)):
            raise RuntimeError("GAR_L4_EXCLUSIVE_PAGE_LEASE_NOT_ATTESTED")
        if bool(getattr(original, "_pending_submit_context", None)):
            raise RuntimeError("GAR_L4_PENDING_SUBMIT_UNKNOWN")
        try:
            old_generation_active = bool(original._is_generation_active())
        except Exception as exc:
            raise RuntimeError("GAR_L4_OLD_GENERATION_NOT_ATTESTED") from exc
        if old_generation_active:
            raise RuntimeError("GAR_L4_OLD_GENERATION_ACTIVE")
        original_context = getattr(original, "_browser", None)
        context = getattr(original, "_gar_second_context", None) or original_context
        if original_context is None or not any(
            item is original_page for item in getattr(original_context, "pages", ())
        ):
            raise RuntimeError("GAR_L4_ORIGINAL_CONTEXT_NOT_ATTESTED")
        pages = list(getattr(context, "pages", ()) or ())
        matching = [
            page for page in pages
            if not bool(page.is_closed())
            and same_conversation(str(getattr(page, "url", "") or ""), target)
        ]
        if len(matching) != 1 or matching[0] is original_page:
            raise RuntimeError("GAR_L4_TARGET_PAGE_NOT_UNIQUELY_OWNED")
        page = matching[0]
        from agent_core.conversation_ownership import ConversationOwnershipRegistry
        from agent_core.paths import source_root
        # Match WebLLMScraper._claim_conversation_owner's exact registry root.
        if ConversationOwnershipRegistry(source_root()).references(target):
            raise RuntimeError("GAR_L4_FRESH_CONTEXT_ALREADY_OWNED")
        # A borrowed WebCopilot tab can be used only while the same browser
        # profile and process lease remain stable. Never call adopt_page:
        # it rewrites the provider-global scraper and the original reply route.
        from agent_core.web_runtime import WebLLMScraper
        from WebAgent.browser_client import WebAgentBrowserClient
        from WebAgent.protocol import (
            WEBAGENT_PROTOCOL_BODY, WEBAGENT_PROTOCOL_NAME, WEBAGENT_PROTOCOL_VERSION,
        )
        from agent_core.session_protocol import SessionProtocol
        scraper = WebLLMScraper(service=conversation_provider(target))
        scraper._browser = context
        scraper._page = page
        scraper._attached_over_cdp = True
        scraper._active_cdp_endpoint = (
            getattr(original, "_gar_second_endpoint", None)
            or getattr(original, "_active_cdp_endpoint", None)
        )
        scraper._cdp_browser = (
            getattr(original, "_gar_second_browser_connection", None)
            or getattr(original, "_cdp_browser", None)
        )
        scraper._execution_page_lease_owned = True
        scraper._expected_execution_url = target
        scraper._rate_governor = self.client.governor
        # Observational only, scoped to the L4 Fresh Planner.
        scraper._gar_l4_readback_diagnostic = True
        try:
            if bool(scraper._is_generation_active()):
                raise RuntimeError("GAR_L4_TARGET_GENERATION_ACTIVE")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("GAR_L4_TARGET_IDLE_NOT_ATTESTED") from exc
        # Claim/release only this distinct conversation; never close the page.
        scraper._claim_conversation_owner(target)
        self._used = True
        closed = False

        def close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            scraper.release_conversation_owner()

        try:
            fresh_client = WebAgentBrowserClient(
                scraper, governor=self.client.governor,
                event_sink=self.client.event_sink,
                display_name=self.display_name + " L4",
            )
            session_id = "GAR-L4-" + uuid.uuid4().hex.upper()
            supervisor = os.environ.get(
                "SMARTAGENT_GAR_L4_SUPERVISOR_V1", "1"
            ).strip().lower() not in {"0", "false", "no", "off"}
            if supervisor:
                from agent_core.gar_l4_supervision import SUPERVISOR_PROTOCOL
            protocol = SessionProtocol(
                "gar_l4_supervisor" if supervisor else WEBAGENT_PROTOCOL_NAME,
                1 if supervisor else WEBAGENT_PROTOCOL_VERSION,
                SUPERVISOR_PROTOCOL if supervisor else WEBAGENT_PROTOCOL_BODY,
            )
            reply = fresh_client.ask(
                protocol.bootstrap_prompt(session_id=session_id),
                stage="GAR L4 獨立 Planner 協議初始化",
                protocol_expected=None,
            )
            ready = protocol.parse_protocol_ready(reply, session_id=session_id)
            current_url = str(getattr(page, "url", "") or "")
            if not ready or not same_conversation(current_url, target):
                raise RuntimeError("GAR_L4_PROTOCOL_IDENTITY_UNVERIFIED")
            if bool(getattr(scraper, "_pending_submit_context", None)):
                raise RuntimeError("GAR_L4_BOOTSTRAP_SUBMISSION_UNCERTAIN")
            if not bool(getattr(original, "_execution_page_lease_owned", False)):
                raise RuntimeError("GAR_L4_ORIGINAL_LEASE_LOST")
            proof = FreshContextProof(
                request_id=request_id, task_id=task_id, task_epoch=task_epoch,
                owner=owner,
                original_conversation_id=conversation_id(original_url),
                fresh_conversation_id=conversation_id(current_url),
                protocol_session_id=session_id,
                protocol_identity_verified=True,
                page_ownership_verified=True,
                previous_generation_inactive=True,
            )

            def assert_binding() -> None:
                if not same_conversation(str(getattr(page, "url", "") or ""), target):
                    raise RuntimeError("GAR_L4_PAGE_IDENTITY_CHANGED")
                if not bool(getattr(original, "_execution_page_lease_owned", False)):
                    raise RuntimeError("GAR_L4_ORIGINAL_LEASE_LOST")
                if bool(getattr(original, "_pending_submit_context", None)):
                    raise RuntimeError("GAR_L4_ORIGINAL_SUBMIT_UNKNOWN")

            def planner(prompt: str, expected: dict,
                        attachments: list[str]) -> str:
                if supervisor:
                    raise RuntimeError("GAR_L4_SUPERVISOR_NO_TOOL_PLANNER")
                assert_binding()
                return fresh_client.ask(
                    prompt,
                    stage="GAR L4 Fresh Planner 回合",
                    attachment_paths=attachments or None,
                    protocol_expected=expected,
                )

            def review(prompt: str) -> str:
                if not supervisor:
                    raise RuntimeError("GAR_L4_REVIEW_MODE_NOT_ENABLED")
                assert_binding()
                answer = fresh_client.ask(
                    prompt, stage="GAR L4 WebGPT2 read-only recovery review",
                    protocol_expected=None,
                )
                assert_binding()
                if bool(getattr(scraper, "_pending_submit_context", None)):
                    raise RuntimeError("GAR_L4_REVIEW_SUBMISSION_UNCERTAIN")
                return answer

            return FreshPlannerBinding(proof=proof, planner=planner,
                                       close=close, review=review if supervisor else None)
        except BaseException:
            close()
            raise


__all__ = ["ExistingConversationFreshAdapter", "FreshPlannerBinding",
           "ensure_fresh_context_page", "prepare_gar_browser_context"]
