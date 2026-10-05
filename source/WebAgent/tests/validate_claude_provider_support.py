#!/usr/bin/env python3
"""Focused static/runtime acceptance for Claude Web conversation support."""
from __future__ import annotations

from pathlib import Path

from agent_core.conversation_identity import conversation_id, conversation_provider, same_conversation
from agent_core.web_provider_routing import endpoint_for_url, provider_for_url, web_model_key_for_url
from agent_core.web_runtime import SERVICE_CONFIG
from agent_core.web_ui import (
    WebUIProviderAdapter,
    WebUIProviderNotImplemented,
    create_web_ui,
    normalize_web_conversation_url,
    provider_from_url,
    registered_providers,
)
from agent_core.web_ui.providers.claude import ClaudeUIAdapter


CLAUDE_URL = "https://claude.ai/chat/e364c2b5-b42e-4f0b-9507-c06c5fb67186"


class Page:
    url = CLAUDE_URL


def run() -> dict:
    assert normalize_web_conversation_url(CLAUDE_URL) == CLAUDE_URL
    assert provider_from_url(CLAUDE_URL) == "claude"
    assert provider_for_url(CLAUDE_URL) == "claude"
    assert conversation_provider(CLAUDE_URL) == "claude"
    assert conversation_id(CLAUDE_URL) == "e364c2b5-b42e-4f0b-9507-c06c5fb67186"
    assert same_conversation(
        CLAUDE_URL,
        CLAUDE_URL + "?from=acceptance",
    )
    assert web_model_key_for_url(CLAUDE_URL) == "web_claude"
    assert endpoint_for_url(CLAUDE_URL) == "claude.ai"

    assert registered_providers() == ("chatgpt", "claude", "gemini")
    adapter = create_web_ui(provider="claude", page=Page())
    assert isinstance(adapter, ClaudeUIAdapter)
    assert isinstance(adapter, WebUIProviderAdapter)
    assert adapter.provider_name == "claude"
    assert adapter.page.url == CLAUDE_URL

    claude_cfg = SERVICE_CONFIG["claude"]
    assert claude_cfg["profile_subdir"] == "claude"
    assert claude_cfg["new_chat_url"] == "https://claude.ai/new"

    root = Path(__file__).resolve().parents[3]
    generic_consumers = (
        root / "source" / "agent_core" / "web_runtime.py",
        root / "source" / "WebAgent" / "browser_bridge.py",
        root / "source" / "RemoteAgent" / "remote_agent.py",
        root / "source" / "web_copilot.py",
    )
    claude_dom_fragments = (
        "font-claude-message",
        "data-is-streaming",
        "button[aria-label='Send message']",
        "div.ProseMirror[contenteditable='true']",
        "[data-testid='user-message']",
        "[data-testid='assistant-message']",
    )
    for consumer in generic_consumers:
        source = consumer.read_text(encoding="utf-8")
        assert not any(fragment in source for fragment in claude_dom_fragments), consumer

    result = {
        "claude_url_normalization": True,
        "claude_conversation_identity": True,
        "claude_provider_routing": True,
        "claude_factory_contract": True,
        "gemini_registered_separately": "gemini" in registered_providers(),
        "claude_service_config": True,
        "claude_selector_boundary": True,
    }
    print("CLAUDE_PROVIDER_SUPPORT_OK")
    print(result)
    return result


if __name__ == "__main__":
    run()
