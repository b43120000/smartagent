"""Versioned Claude DOM profiles owned only by the Claude provider package."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeUIProfile:
    name: str
    composer_selector: str
    send_selector: str
    user_turn_selector: str
    user_content_selector: str
    assistant_turn_selector: str
    final_content_selector: str
    stop_selectors: tuple[str, ...]
    busy_selectors: tuple[str, ...]


CLAUDE_WEB_2026_PROFILE = ClaudeUIProfile(
    name="claude_web_2026",
    composer_selector=(
        "div.ProseMirror[contenteditable='true'], "
        "[contenteditable='true'][role='textbox']"
    ),
    send_selector=(
        "button[aria-label='Send message'], "
        "button[aria-label='Send Message'], "
        "button[aria-label*='Send message'], "
        "button[data-testid*='send']"
    ),
    user_turn_selector=(
        "[data-testid='user-message'], "
        "[data-testid*='user-message']"
    ),
    user_content_selector="p, .whitespace-pre-wrap",
    assistant_turn_selector=(
        "[data-testid='assistant-message'], "
        "[data-testid*='assistant-message'], "
        "[data-is-streaming]"
    ),
    final_content_selector=(
        ".font-claude-message, "
        "[data-testid='assistant-message'], "
        "[data-testid*='assistant-message']"
    ),
    stop_selectors=(
        "button[aria-label='Stop response']",
        "button[aria-label='Stop generating']",
        "button[aria-label*='Stop']",
        "button[data-testid*='stop']",
    ),
    busy_selectors=(
        "[aria-busy='true']",
        "[role='progressbar']",
        "[data-is-streaming='true']",
        "[data-state='loading']",
        "[data-state='generating']",
        "[data-state='thinking']",
        "[data-state='processing']",
        "[class*='loading']",
        "[class*='thinking']",
        "[class*='generating']",
    ),
)

CLAUDE_UI_PROFILES = (CLAUDE_WEB_2026_PROFILE,)


def profile_by_name(name: str) -> ClaudeUIProfile | None:
    builtin = next((profile for profile in CLAUDE_UI_PROFILES if profile.name == name), None)
    if builtin is not None:
        return builtin
    from ...profile_store import profile_values
    values = profile_values("claude", name=name)
    return ClaudeUIProfile(**values) if values else None


def active_profile() -> ClaudeUIProfile:
    from ...profile_store import profile_values
    values = profile_values("claude")
    return ClaudeUIProfile(**values) if values else CLAUDE_WEB_2026_PROFILE


__all__ = [
    "ClaudeUIProfile", "CLAUDE_WEB_2026_PROFILE", "CLAUDE_UI_PROFILES", "active_profile", "profile_by_name",
]
