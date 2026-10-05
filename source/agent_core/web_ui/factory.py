"""Fail-closed construction of provider-owned web_ui adapters."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .provider_contract import WebUIProviderAdapter
from .provider_registry import (
    create_provider_adapter,
    known_providers,
    normalize_provider_name,
    provider_from_url as _registry_provider_from_url,
    registered_providers,
)


class WebUIProviderError(RuntimeError):
    pass


class WebUIProviderNotImplemented(WebUIProviderError):
    pass


def provider_from_url(url: str) -> str:
    """Resolve one known provider from a browser URL; never guess."""
    try:
        return _registry_provider_from_url(url)
    except ValueError as exc:
        raise WebUIProviderError(str(exc)) from exc


def normalize_web_conversation_url(raw: str) -> str:
    """Normalize one supported Web conversation URL without changing its provider/path."""
    value = str(raw or "").strip().strip('"')
    if not value:
        raise ValueError("Web conversation URL must not be empty")
    markdown = re.fullmatch(r"\[[^\]]*\]\((https?://[^\s)]+)\)", value)
    if markdown:
        value = markdown.group(1)
    elif value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Web conversation URL must use http(s)")
    provider_from_url(value)
    return value


def create_web_ui_for_page(page, *, provider: str = "", profile_name: str = "") -> WebUIProviderAdapter:
    resolved = normalize_provider_name(provider) if provider else provider_from_url(
        str(getattr(page, "url", "") or "")
    )
    return create_web_ui(provider=resolved, page=page, profile_name=profile_name)


def create_web_ui(*, provider: str, page, profile_name: str = "") -> WebUIProviderAdapter:
    """Return one provider adapter; never fall back to another provider."""
    name = normalize_provider_name(provider)
    if name not in known_providers():
        raise WebUIProviderError(f"WEB_UI_PROVIDER_UNKNOWN: {name or '(empty)'}")
    try:
        adapter = create_provider_adapter(name, page, profile_name=profile_name)
    except (ImportError, AttributeError) as exc:
        raise WebUIProviderNotImplemented(f"WEB_UI_PROVIDER_NOT_IMPLEMENTED: {name}") from exc
    if not isinstance(adapter, WebUIProviderAdapter):
        raise WebUIProviderError(f"WEB_UI_PROVIDER_CONTRACT_MISMATCH: {name}")
    return adapter


__all__ = [
    "WebUIProviderError", "WebUIProviderNotImplemented", "create_web_ui",
    "create_web_ui_for_page", "normalize_provider_name", "provider_from_url",
    "normalize_web_conversation_url", "registered_providers",
]
