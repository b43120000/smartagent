"""Stable Web UI boundary for SmartAgent browser automation."""

from .contracts import (
    ArtifactObservation,
    ActivitySnapshot,
    AuthenticationState,
    ComposerSnapshot,
    ComposerValidation,
    CompatibilityReport,
    RequestScope,
    TurnRef,
    UIResponse,
    UISnapshot,
    normalize_ui_text,
    text_digest,
    ui_text_matches,
)
from .request_scope import RequestScopeTracker
from .factory import (
    WebUIProviderError,
    WebUIProviderNotImplemented,
    create_web_ui,
    create_web_ui_for_page,
    normalize_web_conversation_url,
    normalize_provider_name,
    provider_from_url,
    registered_providers,
)
from .provider_contract import WebUIProviderAdapter

__all__ = [
    "ActivitySnapshot",
    "ArtifactObservation",
    "AuthenticationState",
    "CompatibilityReport",
    "ComposerSnapshot",
    "ComposerValidation",
    "RequestScope",
    "RequestScopeTracker",
    "TurnRef",
    "UIResponse",
    "UISnapshot",
    "normalize_ui_text",
    "text_digest",
    "ui_text_matches",
    "WebUIProviderAdapter",
    "WebUIProviderError",
    "WebUIProviderNotImplemented",
    "create_web_ui",
    "create_web_ui_for_page",
    "normalize_web_conversation_url",
    "normalize_provider_name",
    "provider_from_url",
    "registered_providers",
]
