#!/usr/bin/env python3
"""Provider-aware Web conversation identity helpers shared by all interfaces."""
from __future__ import annotations

import re

from .web_ui.factory import (
    WebUIProviderError,
    normalize_web_conversation_url,
    provider_from_url,
)


_CHATGPT_RE = re.compile(r"/c/([^/?#]+)", re.IGNORECASE)
_CLAUDE_RE = re.compile(r"/chat/([^/?#]+)", re.IGNORECASE)
_GEMINI_RE = re.compile(r"/app/([^/?#]+)", re.IGNORECASE)
_M365_RE = re.compile(r"/chat/conversation/([^/?#]+)", re.IGNORECASE)


def conversation_provider(url: str) -> str:
    try:
        return provider_from_url(normalize_web_conversation_url(str(url or "")))
    except (WebUIProviderError, ValueError, TypeError):
        return ""


def conversation_id(url: str) -> str:
    value = str(url or "")
    provider = conversation_provider(value)
    matcher = {
        "chatgpt": _CHATGPT_RE,
        "claude": _CLAUDE_RE,
        "gemini": _GEMINI_RE,
        "m365": _M365_RE,
    }.get(provider)
    if matcher is None:
        if provider.startswith("other_"):
            from hashlib import sha256
            try:
                normalized = normalize_web_conversation_url(value).rstrip("/")
            except (ValueError, WebUIProviderError, TypeError):
                return ""
            return sha256((provider + "\n" + normalized).encode("utf-8")).hexdigest()
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


def conversation_target_matches(actual: str, requested: str) -> bool:
    """Match a resolved page to an exact conversation or provider entry URL.

    Entry URLs have no conversation ID.  They may resolve once to an identified
    conversation for the same provider; callers must then pin that resolved URL.
    """
    actual_provider = conversation_provider(actual)
    requested_provider = conversation_provider(requested)
    if not actual_provider or actual_provider != requested_provider:
        return False
    actual_id = conversation_id(actual)
    requested_id = conversation_id(requested)
    return bool(actual_id and (not requested_id or actual_id == requested_id))


__all__ = [
    "conversation_provider", "conversation_id", "conversation_target_matches",
    "same_conversation",
]
