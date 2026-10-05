#!/usr/bin/env python3
"""Deterministic Gemini Web UI provider and contract acceptance checks."""
from __future__ import annotations

from pathlib import Path
import tempfile

from agent_core.conversation_registry import ConversationRegistry
from agent_core.conversation_identity import conversation_id, conversation_provider
from agent_core.web_provider_routing import endpoint_for_url, provider_for_url, web_model_key_for_url
from agent_core.web_runtime import SERVICE_CONFIG
from agent_core.web_ui import WebUIProviderAdapter, create_web_ui, provider_from_url, registered_providers
from agent_core.web_ui.providers.gemini import GeminiUIAdapter
from agent_core.web_ui.providers.gemini.composer import validate_composer_prompt
from agent_core.web_ui.providers.gemini.profiles import GEMINI_WEB_2026_PROFILE
from agent_core.web_ui.contracts import ComposerSnapshot


GEMINI_URL = "https://gemini.google.com/u/1/app/4d7689a3b59b9885?dest=manage"


class Element:
    def __init__(self, text="", *, attrs=None, order=0, children=None, visible=True):
        self.text = text
        self.attrs = dict(attrs or {})
        self.order = order
        self.children = dict(children or {})
        self.visible = visible

    def inner_text(self): return self.text
    def text_content(self): return self.text
    def get_attribute(self, name): return self.attrs.get(name)
    def query_selector_all(self, selector): return list(self.children.get(selector, ()))
    def is_visible(self): return self.visible
    def evaluate(self, script, arg=None):
        if "compareDocumentPosition" in script:
            return self.order > int(getattr(arg, "order", -1))
        if "matches(selector)" in script:
            return bool(self.attrs.get("matches_final"))
        return None


class Locator:
    def __init__(self, values): self.values = values
    def count(self): return len(self.values)
    def nth(self, index): return self.values[index]
    @property
    def last(self): return self.values[-1]


class Page:
    url = GEMINI_URL

    def __init__(self):
        p = GEMINI_WEB_2026_PROFILE
        self.composer = Element(order=100)
        self.send = Element(order=101)
        self.users = [Element("old request", attrs={"data-query-id": "q1"}, order=1,
                              children={p.user_content_selector: [Element("old request")]})]
        self.assistants = [Element("old answer", attrs={"data-response-id": "r1"}, order=2,
                                   children={p.final_content_selector: [Element("old answer")]})]

    def query_selector_all(self, selector):
        p = GEMINI_WEB_2026_PROFILE
        if selector == "body": return [Element(order=0)]
        if selector == p.composer_selector: return [self.composer]
        if selector == p.send_selector: return [self.send]
        if selector == p.user_turn_selector: return self.users
        if selector == p.assistant_turn_selector: return self.assistants
        return []

    def locator(self, selector): return Locator(self.query_selector_all(selector))
    def evaluate(self, script, arg=None):
        if "compareDocumentPosition" in script and isinstance(arg, list):
            return arg[1].order > arg[0].order
        if "location.hostname" in script: return "gemini.google.com"
        if "location.pathname" in script: return "/u/1/app/4d7689a3b59b9885"
        return None


def run() -> dict:
    assert provider_from_url(GEMINI_URL) == "gemini"
    assert provider_for_url(GEMINI_URL) == "gemini"
    assert conversation_provider(GEMINI_URL) == "gemini"
    assert conversation_id(GEMINI_URL) == "4d7689a3b59b9885"
    assert web_model_key_for_url(GEMINI_URL) == "web_gemini"
    assert endpoint_for_url(GEMINI_URL) == "gemini.google.com"
    assert "gemini" in registered_providers()
    assert SERVICE_CONFIG["gemini"]["profile_subdir"] == "gemini"

    page = Page()
    adapter = create_web_ui(provider="gemini", page=page)
    assert isinstance(adapter, GeminiUIAdapter)
    assert isinstance(adapter, WebUIProviderAdapter)
    assert adapter.probe_compatibility().supported
    assert adapter.page_reachable()
    auth = adapter.authentication_state()
    assert auth.authenticated and auth.status == "AUTHENTICATED"
    login_page = Page()
    login_page.url = "https://accounts.google.com/v3/signin/identifier"
    login_page.composer.visible = False
    login_auth = create_web_ui(provider="gemini", page=login_page).authentication_state()
    assert not login_auth.authenticated and login_auth.status == "LOGIN_REQUIRED"
    initial = adapter.snapshot()
    assert initial.profile_name == "gemini_web_2026"
    assert len(initial.user_turns) == 1 and len(initial.assistant_turns) == 1
    scope = adapter.capture_request("new request", conversation_id="4d7689a3b59b9885")
    p = GEMINI_WEB_2026_PROFILE
    page.users.append(Element("newrequest", attrs={"data-query-id": "q2"}, order=3,
                              children={p.user_content_selector: [Element("newrequest")]}) )
    user = adapter.confirm_user_turn(scope)
    page.assistants.append(Element("new answer", attrs={"data-response-id": "r2"}, order=4,
                                   children={p.final_content_selector: [Element("new answer")]}) )
    assistant = adapter.latest_owned_assistant(scope)
    assert user and user.ordinal == 2
    assert assistant and adapter.extract_final_text(assistant) == "new answer"
    activity = adapter.activity(assistant)
    assert not activity.generating and activity.busy_count == 0

    exact = validate_composer_prompt(
        "alpha\nbeta",
        ComposerSnapshot(primary_text="alpha beta", semantic_text="alpha\nbeta"),
    )
    missing = validate_composer_prompt(
        "alpha beta gamma",
        ComposerSnapshot(primary_text="alpha beta", semantic_text="alpha beta"),
    )
    assert exact.matched and exact.matched_view in {"primary", "semantic"}
    assert not missing.matched
    assert adapter.attachment_dom_state([])["reason"] == "GEMINI_ATTACHMENTS_UNSUPPORTED"

    with tempfile.TemporaryDirectory(prefix="gemini-binding-") as temp:
        workspace = Path(temp) / "workspace"
        workspace.mkdir()
        registry = ConversationRegistry(Path(temp) / "conversations.json")
        record, created = registry.upsert_binding(str(workspace), GEMINI_URL)
        assert created and record["gpt_url"] == GEMINI_URL
        matched = registry.find_by_url(
            "https://gemini.google.com/u/1/app/4d7689a3b59b9885?dest=other"
        )
        assert matched and matched["workspace"] == str(workspace)

    result = {
        "gemini_url_routing": True,
        "gemini_factory_contract": True,
        "gemini_request_ownership": True,
        "gemini_final_text": True,
        "gemini_standard_state_contracts": True,
        "gemini_composer_integrity": True,
        "gemini_attachment_fail_closed": True,
        "gemini_authentication_state": True,
        "gemini_workspace_binding": True,
    }
    print("GEMINI_PROVIDER_SUPPORT_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
