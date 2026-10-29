"""GAR L4: transport-verified fresh Planner context in the SAME browser profile.

No new browser, tab, login, implicit chat navigation, or old-request replay.
The operator must explicitly prepare a *different* existing conversation tab
and pin its exact URL using SMARTAGENT_GAR_FRESH_CONTEXT_URL. This adapter
only attaches to that page while the original worker owns the page lease.

The original response/outbox stays attached to the original request route.
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


@dataclass
class FreshPlannerBinding:
    proof: FreshContextProof
    planner: Callable[[str, dict, list[str]], str]
    close: Callable[[], None]


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
        context = getattr(original, "_browser", None)
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
        if ConversationOwnershipRegistry(self.session_root).references(target):
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
        scraper._execution_page_lease_owned = True
        scraper._expected_execution_url = target
        scraper._rate_governor = self.client.governor
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
            protocol = SessionProtocol(
                WEBAGENT_PROTOCOL_NAME,
                WEBAGENT_PROTOCOL_VERSION,
                WEBAGENT_PROTOCOL_BODY,
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

            def planner(prompt: str, expected: dict,
                        attachments: list[str]) -> str:
                # A copied response from the source page cannot satisfy the
                # new page's request-scope / matching V9 ACK.
                if not same_conversation(str(getattr(page, "url", "") or ""), target):
                    raise RuntimeError("GAR_L4_PAGE_IDENTITY_CHANGED")
                return fresh_client.ask(
                    prompt,
                    stage="GAR L4 Fresh Planner 回合",
                    attachment_paths=attachments or None,
                    protocol_expected=expected,
                )
            return FreshPlannerBinding(proof=proof, planner=planner, close=close)
        except BaseException:
            close()
            raise


__all__ = ["ExistingConversationFreshAdapter", "FreshPlannerBinding"]
