"""Deterministic wiring checks; live Microsoft DOM validation is separate IT."""
import json
import tempfile
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch

from agent_core.web_ui.factory import create_web_ui, normalize_web_conversation_url
from agent_core.web_ui.provider_contract import WebUIProviderAdapter
from agent_core.web_ui.provider_registry import calibration_providers, registered_providers
from agent_core.web_ui.profile_store import validate_selector_payload, build_profile_document, publish_profile
from agent_core.web_ui.providers.m365.adapter import M365UIAdapter
from agent_core.web_ui.providers.m365.profiles import M365_BOOTSTRAP_PROFILE, active_profile
from agent_core.web_ui.providers.m365.composer import validate_composer_prompt
from agent_core.web_ui.contracts import ComposerSnapshot
from agent_core.conversation_identity import conversation_id, same_conversation
from agent_core.conversation_registry import ConversationRegistry
from agent_core.web_provider_routing import provider_for_url, web_model_key_for_url, endpoint_for_url
from agent_core.project_sync_receiver import browser_receiver_identity
from agent_core.project_sync_protocol import ProjectSyncProtocolError
from agent_core.web_runtime import WebLLMScraper, SERVICE_CONFIG, M365_CDP_ENDPOINT, CHATGPT_CDP_ENDPOINT
from validate_gemini_provider_support import Element, Locator

URL = "https://m365.cloud.microsoft/chat/conversation/f59f771b-070a-41ca-b80b-1d01c43f7d60?fromcode=test&es=Click"


class Page:
    url = URL

    def __init__(self):
        self.composer = Element(order=100)
        self.users = []
        self.assistants = []

    def query_selector_all(self, selector):
        p = M365_BOOTSTRAP_PROFILE
        if selector == p.composer_selector: return [self.composer]
        if selector == p.send_selector: return [Element()]
        if selector == p.user_turn_selector: return self.users
        if selector == p.assistant_turn_selector: return self.assistants
        if selector == "body": return [Element()]
        return []

    def locator(self, selector): return Locator(self.query_selector_all(selector))
    def evaluate(self, script, arg=None):
        if "compareDocumentPosition" in script: return arg[1].order > arg[0].order
        return None


def run():
    assert normalize_web_conversation_url("[M365](" + URL + ")") == URL
    assert provider_for_url(URL) == "m365"
    assert web_model_key_for_url(URL) == "web_m365"
    assert endpoint_for_url(URL) == "m365.cloud.microsoft"
    assert set(registered_providers()) == {"chatgpt", "claude", "gemini", "m365"}
    assert "m365" in calibration_providers()
    assert SERVICE_CONFIG["m365"]["profile_subdir"] == "m365"
    assert M365_CDP_ENDPOINT != CHATGPT_CDP_ENDPOINT
    assert same_conversation(URL, URL.split("?")[0] + "?es=Other")
    assert not same_conversation(URL, "https://claude.ai/chat/" + conversation_id(URL))
    page = Page()
    adapter = create_web_ui(provider="m365", page=page)
    assert type(adapter) is M365UIAdapter and isinstance(adapter, WebUIProviderAdapter)
    assert adapter.require_compatible().supported
    assert adapter.authentication_state().authenticated
    assert adapter.page_reachable()
    scope = adapter.capture_request("new request", conversation_id=conversation_id(URL))
    p = M365_BOOTSTRAP_PROFILE
    page.users.append(Element("new request", attrs={"data-message-id": "u1"}, order=1,
        children={p.user_content_selector: [Element("new request")]}))
    assert adapter.confirm_user_turn(scope)
    page.assistants.append(Element("answer", attrs={"data-message-id": "a1"}, order=2,
        children={p.final_content_selector: [Element('```smartagent_tool\n{"tool":"turn_commit","action_count":0}\n```')]}))
    assistant = adapter.latest_owned_assistant(scope)
    assert assistant and '"turn_commit"' in adapter.extract_final_text(assistant)
    assert not adapter.activity(assistant).generating
    login_page = Page()
    login_page.url = "https://login.microsoftonline.com/tenant/oauth2/authorize"
    login_page.composer.visible = False
    login_adapter = create_web_ui(provider="m365", page=login_page)
    assert login_adapter.authentication_state().status == "LOGIN_REQUIRED"
    assert not login_adapter.page_reachable()
    # Binding/profile storage preserves provider identity rather than selecting ChatGPT.
    assert adapter.attachment_dom_state([])["supported"] is False
    assert validate_composer_prompt("a\nb", ComposerSnapshot(primary_text="", semantic_text="a\nb")).matched
    assert not validate_composer_prompt("abc", ComposerSnapshot(primary_text="ab", semantic_text="ab")).matched
    scraper = type("Scraper", (), {"_page": page, "_expected_execution_url": URL})()
    assert browser_receiver_identity(scraper) == URL.split("?")[0]
    scraper._expected_execution_url = URL.replace("f59f", "aaaa")
    try:
        browser_receiver_identity(scraper)
    except ProjectSyncProtocolError as exc:
        assert "receiver_changed" in str(exc)
    else:
        raise AssertionError("wrong receiver was accepted")
    with tempfile.TemporaryDirectory(prefix="m365-wiring-") as temp:
        root = Path(temp)
        registry = ConversationRegistry(root / "registry.json")
        registry.upsert_binding(str(root), URL)
        assert registry.find_by_url(URL.split("?")[0])
        selectors = asdict(p)
        selectors.pop("name")
        assert validate_selector_payload("m365", selectors)
        doc = build_profile_document("m365", selectors, source_url=URL,
            planner_url="https://chatgpt.com/c/planner", validation={"passed": True, "fixture_test": True})
        with patch("agent_core.web_ui.profile_store.web_ui_profile_root", return_value=root / "profiles"):
            publish_profile(doc)
            assert active_profile().name == doc["name"]
        with patch("agent_core.web_runtime.PROFILE_DIR", root / "browser"):
            runtime = WebLLMScraper("m365")
            runtime._page = page
            assert type(runtime._web_ui_adapter()) is M365UIAdapter
            runtime._browser = object()
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return b'{"webSocketDebuggerUrl":"ws://127.0.0.1:1275/devtools/browser/test"}'
            with patch("agent_core.web_runtime.urllib.request.urlopen", return_value=Response()) as opened:
                assert runtime.get_cdp_endpoint() == M365_CDP_ENDPOINT
                assert opened.call_args.args[0] == M365_CDP_ENDPOINT + "/json/version"
    print("M365_PROVIDER_WIRING_OK (live DOM and attachments NOT_TESTED)")


if __name__ == "__main__":
    run()
