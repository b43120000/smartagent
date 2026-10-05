#!/usr/bin/env python3
"""Focused regression checks for the provider-neutral web_ui cut point."""
from __future__ import annotations

from pathlib import Path

from agent_core.web_ui import (
    WebUIProviderAdapter,
    WebUIProviderNotImplemented,
    create_web_ui,
    provider_from_url,
    registered_providers,
)
from agent_core.web_ui.adapter import ChatGPTUIAdapter as LegacyChatGPTUIAdapter
from agent_core.web_ui.base_adapter import BaseWebUIAdapter
from agent_core.web_ui.providers.chatgpt import ChatGPTUIAdapter
from agent_core.web_ui.providers.claude import ClaudeUIAdapter
from agent_core.web_ui.providers.gemini import GeminiUIAdapter


class Page:
    pass


def run() -> dict:
    page = Page()
    adapter = create_web_ui(provider="chatgpt", page=page)
    assert isinstance(adapter, WebUIProviderAdapter)
    assert adapter.provider_name == "chatgpt"
    assert adapter.page is page
    assert ChatGPTUIAdapter is LegacyChatGPTUIAdapter
    assert isinstance(create_web_ui(provider="claude", page=page), ClaudeUIAdapter)
    assert isinstance(create_web_ui(provider="gemini", page=page), GeminiUIAdapter)
    assert issubclass(ChatGPTUIAdapter, BaseWebUIAdapter)
    assert issubclass(ClaudeUIAdapter, BaseWebUIAdapter)
    assert issubclass(GeminiUIAdapter, BaseWebUIAdapter)
    assert not issubclass(GeminiUIAdapter, ChatGPTUIAdapter)
    assert not issubclass(ClaudeUIAdapter, ChatGPTUIAdapter)
    assert registered_providers() == ("chatgpt", "claude", "gemini")
    assert provider_from_url("https://chatgpt.com/c/abc") == "chatgpt"
    assert provider_from_url("https://gemini.google.com/app/abc") == "gemini"
    assert provider_from_url("https://claude.ai/chat/abc") == "claude"

    try:
        create_web_ui(provider="unknown-provider", page=page)
    except Exception as exc:
        assert "WEB_UI_PROVIDER_UNKNOWN" in str(exc)
    else:
        raise AssertionError("unknown providers must fail closed")

    root = Path(__file__).resolve().parents[3]
    runtime_source = (root / "source" / "agent_core" / "web_runtime.py").read_text(encoding="utf-8")
    remote_source = (root / "source" / "RemoteAgent" / "remote_runtime.py").read_text(encoding="utf-8")
    boundary_consumers = [
        root / "source" / "agent_core" / "web_runtime.py",
        root / "source" / "WebAgent" / "browser_bridge.py",
        root / "source" / "RemoteAgent" / "remote_runtime.py",
        root / "source" / "RemoteAgent" / "browser_control_worker.py",
        root / "source" / "RemoteAgent" / "webgpt_transport.py",
    ]
    forbidden_dom = (
        "[data-message-author-role", "[data-content-search-unit-key",
        "#prompt-textarea", "data-testid='send-button'",
        'data-testid="send-button"', "data-testid='stop-button'",
        'data-testid="stop-button"', "rich-textarea", "model-response",
    )
    for consumer in boundary_consumers:
        source = consumer.read_text(encoding="utf-8")
        assert not any(fragment in source for fragment in forbidden_dom), consumer
    assert "CHATGPT_COMPOSER_SELECTOR = (" not in runtime_source
    assert "snapshot = self._capture_request_turn_state(prompt)" in runtime_source
    assert "self._web_ui_adapter().reconcile_user_turn(scope)" in runtime_source
    assert "self._web_ui_adapter().latest_owned_assistant(scope)" in runtime_source
    assert "from agent_core.web_runtime import (" not in remote_source
    assert "create_web_ui_for_page(self._page,provider=provider)" in remote_source
    assert "provider=\"chatgpt\"" not in remote_source
    base_source = (root / "source" / "agent_core" / "web_ui" / "base_adapter.py").read_text(
        encoding="utf-8"
    ).lower()
    assert not any(name in base_source for name in ("chatgpt", "gemini", "claude"))

    result = {
        "provider_contract": True,
        "chatgpt_factory": True,
        "claude_factory": True,
        "gemini_factory": True,
        "providers_share_neutral_base_only": True,
        "url_provider_resolution": True,
        "legacy_import_compatibility": True,
        "unknown_providers_fail_closed": True,
        "remote_runtime_uses_web_ui": True,
        "web_runtime_uses_request_scope": True,
        "selector_ownership_moved": True,
        "consumer_dom_leak_scan": True,
    }
    print("WEB_UI_PROVIDER_BOUNDARY_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
