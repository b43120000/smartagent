#!/usr/bin/env python3
"""Provider-aware Web conversation identity helpers shared by all interfaces."""
from __future__ import annotations

import re

from .web_ui.factory import WebUIProviderError, provider_from_url


_CHATGPT_RE = re.compile(r"/c/([^/?#]+)", re.IGNORECASE)
_CLAUDE_RE = re.compile(r"/chat/([^/?#]+)", re.IGNORECASE)
_GEMINI_RE = re.compile(r"/app/([^/?#]+)", re.IGNORECASE)


def conversation_provider(url: str) -> str:
    try:
        return provider_from_url(str(url or ""))
    except (WebUIProviderError, ValueError, TypeError):
        return ""


def conversation_id(url: str) -> str:
    value = str(url or "")
    provider = conversation_provider(value)
    matcher = {
        "chatgpt": _CHATGPT_RE,
        "claude": _CLAUDE_RE,
        "gemini": _GEMINI_RE,
    }.get(provider)
    if matcher is None:
        return ""
    match = matcher.search(value)
    return match.group(1).lower() if match else ""


def same_conversation(left: str, right: str) -> bool:
    left_provider = conversation_provider(left)
    right_provider = conversation_provider(right)
    if not left_provider or left_provider != right_provider:
        return False
    left_id = conversation_id(left)
    right_id = conversation_id(right)
    return bool(left_id and right_id and left_id == right_id)


__all__ = ["conversation_provider", "conversation_id", "same_conversation"]
