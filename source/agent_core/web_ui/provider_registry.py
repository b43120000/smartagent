"""Single source of truth for supported Web UI providers.

Provider-specific modules are imported lazily so URL routing and profile
validation never need to import Playwright-facing adapter code.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import import_module
from urllib.parse import urlparse

from .provider_contract import WebUIProviderAdapter


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    hosts: tuple[str, ...]
    aliases: tuple[str, ...]
    adapter_module: str
    adapter_class: str
    calibration: bool = True
    profile_store: bool = True
    attachments: bool = False

    def create(self, page, *, profile_name: str = "") -> WebUIProviderAdapter:
        module = import_module(self.adapter_module, package=__package__)
        adapter_type = getattr(module, self.adapter_class)
        return adapter_type(page, profile_name=profile_name)


_SPECS = (
    ProviderSpec(
        name="m365",
        hosts=("m365.cloud.microsoft",),
        aliases=("microsoft_365", "m365_copilot"),
        adapter_module=".providers.m365",
        adapter_class="M365UIAdapter",
        attachments=False,
    ),
    ProviderSpec(
        name="chatgpt",
        hosts=("chatgpt.com", "chat.openai.com"),
        aliases=("chat_gpt",),
        adapter_module=".providers.chatgpt",
        adapter_class="ChatGPTUIAdapter",
        attachments=True,
    ),
    ProviderSpec(
        name="gemini",
        hosts=("gemini.google.com",),
        aliases=("google_gemini",),
        adapter_module=".providers.gemini",
        adapter_class="GeminiUIAdapter",
        attachments=False,
    ),
    ProviderSpec(
        name="claude",
        hosts=("claude.ai",),
        aliases=("anthropic_claude",),
        adapter_module=".providers.claude",
        adapter_class="ClaudeUIAdapter",
        attachments=True,
    ),
)

_BY_NAME = {spec.name: spec for spec in _SPECS}
_ALIASES = {
    alias: spec.name
    for spec in _SPECS
    for alias in spec.aliases
}


def normalize_provider_name(provider: str) -> str:
    value = str(provider or "").strip().lower().replace("-", "_")
    return _ALIASES.get(value, value)


def provider_spec(provider: str) -> ProviderSpec | None:
    name = normalize_provider_name(provider)
    if is_other_provider(name):
        return ProviderSpec(name, (), (), '.providers.other', 'OtherUIAdapter', calibration=False, profile_store=False)
    return _BY_NAME.get(name)


def provider_from_url(url: str) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except Exception:
        host = ""
    for spec in _SPECS:
        if any(host == suffix or host.endswith("." + suffix) for suffix in spec.hosts):
            return spec.name
    return other_provider_from_url(url)


def is_other_provider(provider: str) -> bool:
    return bool(re.fullmatch(r"other_[a-f0-9]{64}", str(provider)))


def other_provider_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('WEB_UI_PROVIDER_URL_INVALID')
    host = parsed.hostname.lower().rstrip('.')
    if ':' in host:
        host = f'[{host}]'
    port = parsed.port
    origin = f"{parsed.scheme}://{host}" + (f":{port}" if port and port != {'http':80,'https':443}[parsed.scheme] else '')
    return 'other_' + hashlib.sha256(origin.encode('utf-8')).hexdigest()


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(_BY_NAME))


def registered_providers() -> tuple[str, ...]:
    return tuple(sorted(spec.name for spec in _SPECS if spec.adapter_module))


def calibration_providers() -> tuple[str, ...]:
    return tuple(sorted(spec.name for spec in _SPECS if spec.calibration))


def profile_store_providers() -> tuple[str, ...]:
    return tuple(sorted(spec.name for spec in _SPECS if spec.profile_store))


def create_provider_adapter(provider: str, page, *, profile_name: str = "") -> WebUIProviderAdapter:
    if is_other_provider(provider) and provider_from_url(page.url) != provider:
        raise ValueError('manual_other_page_origin_changed')
    spec = provider_spec(provider)
    if spec is None:
        raise KeyError(normalize_provider_name(provider))
    return spec.create(page, profile_name=profile_name)


__all__ = [
    "ProviderSpec", "calibration_providers", "create_provider_adapter",
    "known_providers", "normalize_provider_name", "profile_store_providers",
    "provider_from_url", "provider_spec", "registered_providers", "is_other_provider",
]
