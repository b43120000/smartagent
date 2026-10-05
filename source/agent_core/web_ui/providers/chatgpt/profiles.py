"""Versioned ChatGPT DOM profiles owned by the ChatGPT provider package.

Raw ChatGPT selectors are owned here.  A request pins one profile; selectors
from different renderer generations are never unioned for freshness decisions.
"""
from __future__ import annotations

from dataclasses import dataclass


CHATGPT_COMPOSER_SELECTOR = (
    "#prompt-textarea, "
    "div.ProseMirror[contenteditable='true'][role='textbox']"
)
CHATGPT_SEND_SELECTOR = (
    "button[data-testid='send-button'], "
    "form button[type='submit'][aria-label*='Send'], "
    "form button[type='submit'][aria-label*='傳送']"
)

# Compatibility exports for migration only.  web_ui never uses these unions to
# prove request ownership.
CHATGPT_USER_TURN_SELECTOR = (
    "[data-message-author-role='user'], "
    "[data-content-search-unit-key$=':user']"
)
CHATGPT_ASSISTANT_TURN_SELECTOR = (
    "[data-message-author-role='assistant'], "
    "[data-content-search-unit-key$=':assistant']"
)
CHATGPT_RESPONSE_SELECTOR = (
    ".markdown, "
    "[data-message-author-role='assistant'] .markdown, "
    "[data-markdown-text-style='assistant-message']"
)


@dataclass(frozen=True)
class ChatGPTUIProfile:
    name: str
    composer_selector: str
    send_selector: str
    user_turn_selector: str
    user_content_selector: str
    assistant_turn_selector: str
    final_content_selector: str
    stop_selectors: tuple[str, ...]
    busy_selectors: tuple[str, ...]


CHATGPT_STOP_SELECTORS = (
    "button[data-testid='stop-button']",
    "button[data-testid*='stop']",
    "button[aria-label='Stop streaming']",
    "button[aria-label='Stop responding']",
    "button[aria-label*='Stop']",
    "button[aria-label*='停止']",
    "form button[aria-label*='停止']",
    "form button:has(rect)",
)

_BUSY_SELECTORS = (
    "[aria-busy='true']",
    "[role='progressbar']",
    "[data-state='loading']",
    "[data-state='generating']",
    "[data-state='processing']",
    "[data-state='thinking']",
    "[data-state='working']",
    "[data-state='creating']",
    "[data-state='preparing']",
    "[data-testid*='loading']",
    "[data-testid*='generat']",
    "[class*='loading']",
    "[class*='generating']",
    "[class*='thinking']",
    "[class*='processing']",
    "[class*='skeleton']",
    "[class*='shimmer']",
)

LEGACY_ROLE_PROFILE = ChatGPTUIProfile(
    name="chatgpt_legacy_role",
    composer_selector=CHATGPT_COMPOSER_SELECTOR,
    send_selector=CHATGPT_SEND_SELECTOR,
    user_turn_selector="[data-message-author-role='user']",
    user_content_selector=".text-size-chat.whitespace-pre-wrap, .whitespace-pre-wrap",
    assistant_turn_selector="[data-message-author-role='assistant']",
    final_content_selector=".markdown",
    stop_selectors=CHATGPT_STOP_SELECTORS,
    busy_selectors=_BUSY_SELECTORS,
)

SEARCH_UNIT_2026_PROFILE = ChatGPTUIProfile(
    name="chatgpt_search_unit_2026",
    composer_selector=CHATGPT_COMPOSER_SELECTOR,
    send_selector=CHATGPT_SEND_SELECTOR,
    user_turn_selector="[data-content-search-unit-key$=':user']",
    user_content_selector=".text-size-chat.whitespace-pre-wrap, .whitespace-pre-wrap",
    assistant_turn_selector="[data-content-search-unit-key$=':assistant']",
    final_content_selector=".markdown, [data-markdown-text-style='assistant-message']",
    stop_selectors=CHATGPT_STOP_SELECTORS,
    busy_selectors=_BUSY_SELECTORS,
)

CHATGPT_UI_PROFILES = (SEARCH_UNIT_2026_PROFILE, LEGACY_ROLE_PROFILE)


def profile_by_name(name: str) -> ChatGPTUIProfile | None:
    builtin = next((profile for profile in CHATGPT_UI_PROFILES if profile.name == name), None)
    if builtin is not None:
        return builtin
    from ...profile_store import profile_values
    values = profile_values("chatgpt", name=name)
    return ChatGPTUIProfile(**values) if values else None


def active_profile() -> ChatGPTUIProfile:
    from ...profile_store import profile_values
    values = profile_values("chatgpt")
    return ChatGPTUIProfile(**values) if values else SEARCH_UNIT_2026_PROFILE
