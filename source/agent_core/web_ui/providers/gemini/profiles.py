"""Versioned Gemini DOM profiles owned only by the Gemini provider package."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GeminiUIProfile:
    name: str
    composer_selector: str
    send_selector: str
    user_turn_selector: str
    user_content_selector: str
    assistant_turn_selector: str
    final_content_selector: str
    stop_selectors: tuple[str, ...]
    busy_selectors: tuple[str, ...]


GEMINI_WEB_2026_PROFILE = GeminiUIProfile(
    name="gemini_web_2026",
    composer_selector=(
        "rich-textarea .ql-editor[contenteditable='true'], "
        "div.ql-editor[contenteditable='true'][role='textbox'], "
        "[contenteditable='true'][role='textbox'][aria-label*='prompt' i]"
    ),
    send_selector=(
        "button.send-button, "
        "button[aria-label*='Send message' i], "
        "button[data-test-id='send-button'], "
        "button[data-testid='send-button']"
    ),
    user_turn_selector=(
        "user-query, "
        "[data-test-id='user-query'], "
        "[data-testid='user-query']"
    ),
    user_content_selector=(
        ".query-text, .user-query-content, "
        "[data-test-id='user-query-text'], [data-testid='user-query-text']"
    ),
    assistant_turn_selector=(
        "model-response, "
        "[data-test-id='model-response'], "
        "[data-testid='model-response']"
    ),
    final_content_selector=(
        "message-content, .model-response-text, .markdown, "
        "[data-test-id='response-content'], [data-testid='response-content']"
    ),
    stop_selectors=(
        "button[aria-label*='Stop response' i]",
        "button[aria-label*='Stop generating' i]",
        "button.stop-button",
        "button[data-test-id='stop-button']",
        "button[data-testid='stop-button']",
    ),
    busy_selectors=(
        "[aria-busy='true']",
        "[role='progressbar']",
        "mat-progress-spinner",
        "[data-state='loading']",
        "[data-state='generating']",
        "[data-state='thinking']",
        "[class*='loading']",
        "[class*='generating']",
        "[class*='thinking']",
    ),
)

GEMINI_UI_PROFILES = (GEMINI_WEB_2026_PROFILE,)


def profile_by_name(name: str) -> GeminiUIProfile | None:
    builtin = next((profile for profile in GEMINI_UI_PROFILES if profile.name == name), None)
    if builtin is not None:
        return builtin
    from ...profile_store import profile_values
    values = profile_values("gemini", name=name)
    return GeminiUIProfile(**values) if values else None


def active_profile() -> GeminiUIProfile:
    from ...profile_store import profile_values
    values = profile_values("gemini")
    return GeminiUIProfile(**values) if values else GEMINI_WEB_2026_PROFILE


__all__ = [
    "GeminiUIProfile", "GEMINI_WEB_2026_PROFILE", "GEMINI_UI_PROFILES",
    "active_profile", "profile_by_name",
]
