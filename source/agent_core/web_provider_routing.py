"""Provider-neutral routing helpers for Web conversation bindings."""
from __future__ import annotations

from .web_ui.factory import normalize_web_conversation_url, provider_from_url

WEB_MODEL_KEYS = {
    "chatgpt": "web_chatgpt",
    "gemini": "web_gemini",
    "claude": "web_claude",
}

PROVIDER_ENDPOINTS = {
    "chatgpt": "chatgpt.com",
    "gemini": "gemini.google.com",
    "claude": "claude.ai",
}


def provider_for_url(url: str) -> str:
    """Return the known provider for one validated Web conversation URL."""
    return provider_from_url(normalize_web_conversation_url(url))


def web_model_key_for_url(url: str) -> str:
    provider = provider_for_url(url)
    try:
        return WEB_MODEL_KEYS[provider]
    except KeyError as exc:
        raise ValueError(f"WEB_PROVIDER_MODEL_UNAVAILABLE: {provider}") from exc


def endpoint_for_url(url: str) -> str:
    provider = provider_for_url(url)
    try:
        return PROVIDER_ENDPOINTS[provider]
    except KeyError as exc:
        raise ValueError(f"WEB_PROVIDER_ENDPOINT_UNAVAILABLE: {provider}") from exc


__all__ = [
    "WEB_MODEL_KEYS", "PROVIDER_ENDPOINTS", "provider_for_url",
    "web_model_key_for_url", "endpoint_for_url",
]
