"""Single source of truth for supported Web UI providers.

Provider-specific modules are imported lazily so URL routing and profile
validation never need to import Playwright-facing adapter code.
"""
from __future__ import annotations

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
    return _BY_NAME.get(normalize_provider_name(provider))


def provider_from_url(url: str) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except Exception:
        host = ""
    for spec in _SPECS:
        if any(host == suffix or host.endswith("." + suffix) for suffix in spec.hosts):
            return spec.name
    raise ValueError(f"WEB_UI_PROVIDER_URL_UNKNOWN: {host or '(empty)'}")


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(_BY_NAME))


def registered_providers() -> tuple[str, ...]:
    return tuple(sorted(spec.name for spec in _SPECS if spec.adapter_module))


def calibration_providers() -> tuple[str, ...]:
    return tuple(sorted(spec.name for spec in _SPECS if spec.calibration))


def profile_store_providers() -> tuple[str, ...]:
    return tuple(sorted(spec.name for spec in _SPECS if spec.profile_store))


def create_provider_adapter(provider: str, page, *, profile_name: str = "") -> WebUIProviderAdapter:
    spec = provider_spec(provider)
    if spec is None:
        raise KeyError(normalize_provider_name(provider))
    return spec.create(page, profile_name=profile_name)


__all__ = [
    "ProviderSpec", "calibration_providers", "create_provider_adapter",
    "known_providers", "normalize_provider_name", "profile_store_providers",
    "provider_from_url", "provider_spec", "registered_providers",
]
